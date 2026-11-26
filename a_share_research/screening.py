"""Monthly candidate batches: deterministic whole-market screen → linked research → one model
comparison → automatic draft freeze (owner selection if unavailable) → forward simulation against total-return benchmarks.

Design: docs/screening-experiment.md. Runs in the worker only (TuShare and model credentials).
"""

import hashlib
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import NoReturn

import anyio
import pandas as pd
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from a_share_research import report as report_body
from a_share_research import market, db, model_profile, runner_process, service, sources, stock_codes
from a_share_research.db import SHANGHAI
from a_share_research.market import ScreenError
from a_share_research.batches import POLL_COLUMNS, horizons_complete, load, group_return, returns, small_rule
from a_share_research.reading import research_state
from a_share_research.service import freeze_batch as freeze
from a_share_research.material import METRICS
from a_share_research.prompts import prompts_sha256, render_prompt
from a_share_research.research_mcp import ERROR_KINDS, _receipt
# Keep the existing import surface for callers and frozen backtest experiments.
from a_share_research.screening_factors import (
    RULE, _months_after, announced_by, annual_rows,
    indicator_check, factors, market_filters, windfall, percentiles, score, pick, rank_candidates,
)

__all__ = [
    "announced_by",
    "annual_rows",
    "group_return",
    "percentiles",
    "pick",
    "score",
]

log = logging.getLogger(__name__)

STANDARD_AUDIT = "标准无保留意见"
REUSE_DAYS = 7  # a passed report this recent is reused instead of researching again
QUESTION = render_prompt("screening_question.j2")
MARK_HOUR = 17  # daily bars are complete well before this (Shanghai time)


# ---------- storage ----------

def _save(conn, batch_id: int, **fields) -> None:
    fields = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v for k, v in fields.items()}
    db.update_batch(conn, batch_id, updated_at=db.now(), **fields)


# ---------- data ----------

_query = market.retrying_query


def _compact(d: date) -> str:
    return d.strftime("%Y%m%d")


def latest_session(pro) -> str:
    """Latest trading day whose daily_basic cross-section is already published."""
    today = datetime.now(SHANGHAI).date()
    for day in reversed(market.trade_days(pro, _compact(today - timedelta(days=20)), _compact(today))):
        if not _query(pro, "daily_basic", trade_date=day, fields="ts_code").empty:
            return day
    raise ScreenError("近 20 天没有可用的每日指标")


INDICATOR_FIELDS = "ts_code,ann_date,end_date,roe_dt,profit_dedt,update_flag"
INCOME_FIELDS = "ts_code,ann_date,f_ann_date,end_date,report_type,n_income,update_flag"
CASHFLOW_FIELDS = "ts_code,ann_date,f_ann_date,end_date,report_type,n_cashflow_act,update_flag"


def evaluate(base: dict, ind: pd.DataFrame, inc: pd.DataFrame, cf: pd.DataFrame, audit, as_of: str) -> dict:
    """One company's status and factors as of `as_of`; `audit` is called only when the opinion matters."""
    result = base | factors(ind, inc, cf, as_of)
    if result["status"] == "excluded":
        return result
    audit = audit()
    audit = audit[(audit["end_date"].astype(str) == f"{result['years'][-1]}1231") & (audit["ann_date"].astype(str) <= as_of)]
    opinion = str(audit.sort_values("ann_date")["audit_result"].iloc[-1]) if not audit.empty else None
    result["audit"] = opinion
    if opinion is None:
        return result | {"status": "gap", "reason": "; ".join(filter(None, [result.get("reason"), "最新年度审计意见缺失"]))}
    if opinion != STANDARD_AUDIT:
        return result | {"status": "excluded", "reason": f"审计意见：{opinion}"}
    if result["status"] == "ok" and (result["pe"] is None or result["pe"] <= 0):
        return result | {"status": "gap", "reason": "市盈率TTM非正或缺失，不参与估值排名"}
    return result


def momentum(pro, as_of: str, codes: list[str], rule: dict = RULE) -> dict:
    """Adjusted return over the last `momentum_days` sessions; None when either end has no bar."""
    days = rule.get("momentum_days", 60)  # screen-v1 batches predate the recorded momentum hint
    start = _compact(datetime.strptime(as_of, "%Y%m%d").date() - timedelta(days=days * 3))
    first = market.trade_days(pro, start, as_of)[-(days + 1)]
    ends = {}
    for day in (first, as_of):
        close = _query(pro, "daily", trade_date=day, fields="ts_code,close").set_index("ts_code")["close"]
        factor = _query(pro, "adj_factor", trade_date=day).set_index("ts_code")["adj_factor"]
        ends[day] = {c: float(close[c]) * float(factor[c]) for c in codes
                     if c in close.index and c in factor.index and _positive(close[c]) and _positive(factor[c])}
    out = {}
    for code in codes:
        value = ends[as_of][code] / ends[first][code] - 1 if code in ends[first] and code in ends[as_of] else None
        out[code] = {"return_60d": value, "drawdown": value is not None and value < rule.get("drawdown_flag", -0.3),
                     "momentum_from": first}
    return out


def screen(as_of: str | None = None, workers: int = 4, rule: dict = RULE) -> dict:
    """Whole-market screen on the latest published session; returns universe, ranked rows and Q20/Q5."""
    if rule.get("version") not in {"screen-v1", "screen-v1.1", "screen-v1.2", "screen-v1.3"} or rule.get("industry") != "SW2021 L1":
        raise ScreenError("不支持的初筛规则版本或行业分类")
    if rule["version"] in {"screen-v1.1", "screen-v1.2", "screen-v1.3"} and "windfall_flag" not in rule:
        raise ScreenError("screen-v1.1 缺少利润跳升提示规则")
    if rule["version"] in {"screen-v1.2", "screen-v1.3"} and not isinstance(rule.get("small"), dict):
        raise ScreenError(f"{rule['version']} 缺少小市值组规则")
    pro = market.client()
    as_of = as_of or latest_session(pro)
    start = _compact(datetime.strptime(as_of, "%Y%m%d").date() - timedelta(days=60))
    days = [d for d in market.trade_days(pro, start, as_of)][-rule["amount_days"]:]
    basic = _query(pro, "stock_basic", list_status="L", fields="ts_code,name,industry,market,list_date")
    valuation = _query(pro, "daily_basic", trade_date=as_of, fields="ts_code,close,pe_ttm,total_mv")
    suspended = set(_query(pro, "suspend_d", trade_date=as_of, suspend_type="S")["ts_code"])
    members = _query(pro, "index_member_all", is_new="Y")
    sw = members.drop_duplicates("ts_code").set_index("ts_code")["l1_name"]
    amount = pd.concat([_query(pro, "daily", trade_date=d, fields="ts_code,amount") for d in days])
    avg_amount = amount.groupby("ts_code")["amount"].sum() / len(days)  # 千元; a suspended day counts as 0

    frame = basic.merge(valuation, on="ts_code", how="left")
    frame["industry_sw"] = frame["ts_code"].map(sw)
    frame["avg_amount"] = frame["ts_code"].map(avg_amount).fillna(0)
    # Both pools use the same market snapshot but have independent eligibility and ranks.
    pools = [(rule, *market_filters(frame, suspended, as_of, rule))]
    if rule["version"] in {"screen-v1.2", "screen-v1.3"}:
        small = small_rule(rule)
        pools.append((small, *market_filters(frame, suspended, as_of, small)))
    eligible = pd.concat([pool[1] for pool in pools]).drop_duplicates("ts_code")
    since = f"{int(as_of[:4]) - 5}0101"

    def company(row) -> dict:
        code = row.ts_code
        base = {"code": code, "name": row.name, "industry": row.industry_sw,
                "pe": float(row.pe_ttm) if pd.notna(row.pe_ttm) else None,
                "total_mv_yi": round(float(row.total_mv) / 1e4, 2), "avg_amount_yi": round(float(row.avg_amount) / 1e5, 2)}
        try:
            ind = _query(pro, "fina_indicator", ts_code=code, start_date=since, end_date=as_of, fields=INDICATOR_FIELDS)
            if ind.empty:
                return base | {"status": "gap", "reason": "没有财务指标"}
            pre = indicator_check(ind, as_of)
            if pre["status"] != "ok":  # skip the other calls for ineligible companies
                return base | pre
            inc = _query(pro, "income", ts_code=code, start_date=since, end_date=as_of, fields=INCOME_FIELDS)
            cf = _query(pro, "cashflow", ts_code=code, start_date=since, end_date=as_of, fields=CASHFLOW_FIELDS)
            return evaluate(base, ind, inc, cf, lambda: _query(pro, "fina_audit", ts_code=code,
                                                               start_date=since, end_date=as_of), as_of)
        except ScreenError as exc:
            return base | {"status": "gap", "reason": f"取数失败：{exc}"}

    started = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        companies = list(pool.map(company, eligible.itertuples(index=False)))
    results = []
    for pool_rule, pool_frame, funnel, no_industry in pools:
        codes = set(pool_frame["ts_code"])
        members = [c for c in companies if c["code"] in codes]
        ranked, candidates, mechanical = rank_candidates(members, pool_rule)
        counts = {s: sum(c["status"] == s for c in members) for s in ("ok", "excluded", "gap")}
        results.append({
            "universe": {"funnel": funnel | {f"factor_{k}": v for k, v in counts.items()},
                         "no_industry": no_industry, "benchmark_days": days,
                         "markets": pool_frame["market"].value_counts().to_dict(),
                         "rows": ranked + [c for c in members if c["status"] != "ok"]},
            "q20": candidates, "q5": mechanical,
        })
        log.info("screen %s %s–%s亿: %s companies in %.0fs, %s", as_of,
                 pool_rule["min_total_mv_yi"], pool_rule.get("max_total_mv_yi", "∞"),
                 len(members), time.monotonic() - started, counts)
    candidates = [r for result in results for r in result["q20"]]
    try:
        moves = momentum(pro, as_of, [r["code"] for r in candidates], rule)
    except ScreenError:  # a missing hint must not fail the screen
        log.exception("momentum for %s failed", as_of)
        moves = {r["code"]: {"return_60d": None, "drawdown": False} for r in candidates}
    for row in candidates:
        row |= moves[row["code"]] | {"windfall": windfall(row, rule)}
    result = {"as_of": as_of} | results[0]
    if len(results) > 1:
        result["universe"]["small"] = results[1]["universe"]
        result |= {"s10": results[1]["q20"], "sq3": results[1]["q5"]}
    return result


# ---------- research and selection ----------

def reusable_report(conn, code: str, since: str):
    """The latest formal report of `code` since `since` that a batch may reuse.

    Corrections are excluded: they may omit valuation and stance. Holdings-aware reports are excluded so
    selection and ranking compare blind judgments only; a blind follow-up cannot descend from one (409).
    """
    code = stock_codes.normalize_key(code)
    return conn.execute(
        "SELECT r.id, r.run_id FROM reports r JOIN runs u ON u.id = r.run_id"
        f" WHERE r.company_key = ? AND r.created_at >= ? AND {db.BLIND_FORMAL_REPORT}"
        # A continuation's answer is about its earlier report, not the batch question.
        " AND json_type(r.body, '$.prior') IS NOT 'object' ORDER BY r.id DESC LIMIT 1",
        (code, since),
    ).fetchone()


def link_research(conn, batch_id: int, q20: list[dict]) -> list[dict]:
    """Reuse a recent passed report or submit research for each Q20 company."""
    since = (datetime.now(SHANGHAI) - timedelta(days=REUSE_DAYS)).isoformat(timespec="seconds")
    for row in q20:
        recent = reusable_report(conn, row["code"], since)
        if recent:
            row |= {"run_id": recent["run_id"], "report_id": recent["id"], "reused": True}
        else:
            row["run_id"] = service.submit(conn, stock_codes.normalize_key(row["code"]), f"batch-{batch_id}-{row['code']}",
                                           name=row["name"], question=QUESTION)
    return q20


def _report_digest(conn, report_id: int | None) -> dict | None:
    if report_id is None:
        return None
    row = conn.execute("SELECT passed, body FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row["passed"]:
        return None
    body = report_body.load(row["body"])
    texts = lambda key: [item["text"] for item in body.get(key, []) if isinstance(item, dict)]
    table = body.get("table", [])
    dates = sorted({p for r in table if r["id"].startswith("T.") for p in r["values"]})
    periods = set()
    if dates:
        latest = dates[-1]
        annual = [p for p in dates if p.endswith("12-31")]
        periods = {latest, f"{int(latest[:4]) - 1}{latest[4:]}", *annual[-1:]}
    metrics = []
    for r in table:
        if r["id"].startswith("C."):
            metrics.append(r)  # Keep exact calculation inputs, periods and units.
        elif r["id"].startswith("T."):
            values = {p: v for p, v in r["values"].items() if p in periods}
            if values:
                metrics.append({k: r[k] for k in ("id", "label", "unit")} | {"values": values})
    present = {r["id"]: r["values"] for r in metrics}
    return {"verdict": body.get("verdict"), "summary": body.get("summary"), "answer": texts("answer"),
            "risks": texts("risks"), "financial": texts("financial"), "business": texts("business"),
            "checkpoints": texts("checkpoints"), "gaps": body.get("gaps", []), "metrics": metrics,
            "missing_metrics": {f"T.{m[0]}": sorted(periods - present.get(f"T.{m[0]}", {}).keys())
                                for m in METRICS if periods - present.get(f"T.{m[0]}", {}).keys()}}


def selectable(report: dict | None) -> bool:
    """A reviewed report whose verdict is not 暂不值得."""
    return bool(report) and report.get("verdict") != "暂不值得"


def selection_prompt(candidates: list[dict], rule: dict) -> dict[str, str]:
    """The cacheable system prompt holds selection instructions; the user prompt holds parameters and candidates."""
    flag = rule.get("windfall_flag", {"min_growth": 0.5, "max_pe": 10})  # legacy comparison hint
    return {"system_prompt": render_prompt("selection_system.j2"),
            "prompt": render_prompt("selection.j2", picks=rule["picks"], picks_per_industry=rule["picks_per_industry"],
                                    min_growth=f"{flag['min_growth']:.0%}", max_pe=flag["max_pe"],
                                    candidates=json.dumps(candidates, ensure_ascii=False, indent=1))}


PICK_FIELDS = ("code", "return_source", "why_over_higher_ranked", "invalidation")


def _strings(items, fields: tuple[str, ...]) -> bool:
    return isinstance(items, list) and all(
        isinstance(i, dict) and all(isinstance(i.get(f), str) and i[f].strip() for f in fields) for i in items)


def selection_problems(draft: dict | None, candidates: list[dict], rule: dict) -> list[str]:
    """Shape and rule checks; the page renders every field, so each must be a non-empty string."""
    if draft is None or not _strings(draft.get("picks"), PICK_FIELDS):
        return [f"picks 必须是对象列表，每项含非空字符串字段 {'、'.join(PICK_FIELDS)}"]
    problems, counts = [], {}
    if not _strings(draft.get("rejected", []), ("code", "reason")):
        problems.append("rejected 每项必须含非空字符串 code 与 reason")
    if not isinstance(draft.get("concentration", ""), str):
        problems.append("concentration 必须是字符串")
    if not (isinstance(draft.get("gaps", []), list) and all(isinstance(g, str) for g in draft.get("gaps", []))):
        problems.append("gaps 必须是字符串列表")
    eligible = {c["code"]: c for c in candidates if selectable(c.get("report"))}
    codes = [p["code"] for p in draft["picks"]]
    if len(set(codes)) != len(codes):
        problems.append("picks 代码不能重复")
    if len(codes) > rule["picks"]:
        problems.append(f"最多选 {rule['picks']} 家")
    for code in codes:
        if code not in eligible:
            problems.append(f"{code} 不是研究通过的候选")
            continue
        industry = eligible[code]["industry"]
        counts[industry] = counts.get(industry, 0) + 1
    problems += [f"{k} 选了 {v} 家，超过 {rule['picks_per_industry']} 家" for k, v in counts.items()
                 if v > rule["picks_per_industry"]]
    return problems


def rankable(candidates: list[dict]) -> list[str]:
    """Every candidate with a reviewed report is ranked, 暂不值得 included: avoiding losers is what is tested."""
    return [c["code"] for c in candidates if c.get("report")]


def ranking_prompt(candidates: list[dict], rule: dict) -> dict[str, str]:
    pool = [c for c in candidates if c.get("report")]
    return {"system_prompt": render_prompt("ranking_system.j2"),
            "prompt": render_prompt("ranking.j2", count=len(pool), benchmark=next(iter(rule["benchmarks"].values())),
                                    candidates=json.dumps(pool, ensure_ascii=False, indent=1))}


def ranking_problems(draft: dict | None, candidates: list[dict]) -> list[str]:
    codes = rankable(candidates)
    if draft is None or not isinstance(draft.get("order"), list) or not all(isinstance(c, str) for c in draft["order"]):
        return ["order 必须是候选代码字符串数组"]
    problems = []
    order = draft["order"]
    if len(set(order)) != len(order):
        problems.append("order 中代码不能重复")
    if missing := [c for c in codes if c not in order]:
        problems.append(f"order 缺少：{'、'.join(missing)}")
    if extra := [c for c in order if c not in codes]:
        problems.append(f"order 含不在名单中的代码：{'、'.join(extra)}")
    if not isinstance(draft.get("basis"), str) or not draft["basis"].strip():
        problems.append("basis 必须是非空字符串")
    return problems


def candidate_digests(conn, batch: dict) -> list[dict]:
    return [{"code": r["code"], "name": r["name"], "industry": r["industry"], "rank": i + 1,
             "score": round(r["score"], 3), "roe_dt_3y": round(r["roe"], 2), "cash_ratio_3y": round(r["cash"], 2),
             "dedt_profit_cagr_3y": round(r["growth"], 4), "pe_ttm": round(r["pe"], 2),
             "small_group": r["small_group"], "return_60d": None if r.get("return_60d") is None else round(r["return_60d"], 4),
             "drawdown_over_30pct": r.get("drawdown"), "profit_jump_low_pe": bool(r.get("windfall")),
             "report": _report_digest(conn, r.get("report_id"))}
            for i, r in enumerate(batch["q20"])]


# ---------- model comparison over the runner's stdio MCP ----------

def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_TEXT = {"type": "string"}
COMPARISONS = ("q20_select", "q20_rank", "s10_select")
COMPARISON_TOOLS = [
    types.Tool(name="next_comparison", description="runner-only：领取下一项比较任务；仅供 runner 使用。",
               input_schema=_object({})),
    types.Tool(name="submit_selection", description=render_prompt("submit_selection_tool.j2").strip(),
               input_schema=_object({
                   "picks": {"type": "array", "items": _object({field: _TEXT for field in PICK_FIELDS})},
                   "rejected": {"type": "array", "items": _object({"code": _TEXT, "reason": _TEXT})},
                   "concentration": {"type": "string"},
                   "gaps": {"type": "array", "items": {"type": "string"}}})),
    types.Tool(name="submit_ranking", description=render_prompt("submit_ranking_tool.j2").strip(),
               input_schema=_object({"order": {"type": "array", "items": _TEXT}, "basis": _TEXT})),
    types.Tool(name="record_call", description="runner-only：记录一次实际模型请求；仅供 runner 使用。",
               input_schema=_object({
                   "step": {"enum": list(COMPARISONS)}, "messages": {"type": "array"}, "tools": {"type": "array"},
                   "response": {"type": ["object", "string", "null"]}, "usage": {"type": ["object", "null"]},
                   "seconds": {"type": "number", "minimum": 0}, "error_kind": {"enum": [None, *ERROR_KINDS]},
                   "identity": {"type": "object"}, "stop_reason": {"type": ["string", "null"]}})),
]
COMPARISON_VALIDATORS = {tool.name: Draft202012Validator(tool.input_schema) for tool in COMPARISON_TOOLS}
SUBMIT_TOOL = {"q20_select": "submit_selection", "q20_rank": "submit_ranking", "s10_select": "submit_selection"}
_OUTPUT = {"q20_select": "draft", "q20_rank": "order", "s10_select": "draft"}
NO_SUBMISSION = "未调用提交工具"
BUDGET_SPENT = "自动月批模型调用已达整批上限"


class _BatchChanged(Exception):
    """Another writer won the batch while a comparison was running."""


class ComparisonSession:
    """One batch's model comparisons, served to a single runner over stdio MCP.

    The runner takes one task at a time, so every paid attempt and each task's result are saved before the
    next comparison starts. A save that finds the batch taken by another writer ends the session at once.
    """

    def __init__(self, conn, batch: dict, large_selectable: bool, small: bool):
        self.conn, self.batch, self.large_selectable, self.small = conn, batch, large_selectable, small
        self.selection = batch.get("selection") or {}
        self.small_selection = batch.get("small_selection") or {}
        self.judgment = (self.selection.get("judgment") or (batch.get("marks") or {}).get("judgment")
                         or model_profile.judgment_identity())
        limits = model_profile.PROFILE.limits
        self.attempt_limit = limits.comparison_attempts_per_task
        # Each attempt is bounded by the runner's model timer; allow for startup and the pipe transfer.
        self.deadline = time.monotonic() + len(COMPARISONS) * self.attempt_limit * (limits.model_call_seconds + 30)
        self.budget = None
        if str(batch.get("request_id") or "").startswith("monthly:"):
            research_calls = conn.execute("SELECT count(*) FROM calls c JOIN runs r ON r.id = c.run_id"
                                          " WHERE r.batch_id = ?", (batch["id"],)).fetchone()[0]
            saved = [self.selection, self.selection.get("ranking") or {}, self.small_selection]
            self.budget = (limits.monthly_batch_model_calls - research_calls
                           - sum(len(task.get("attempts", [])) for task in saved))
        self.task: dict | None = None
        self.failure: Exception | None = None
        self.last_error_kind: str | None = None
        self._fatal: Exception | None = None
        self._step: str | None = "q20_select"
        self._groups: dict[str, tuple[list[dict], dict]] = {}
        self._prompts_sha256 = prompts_sha256()
        self._server = self._build_server()

    def ready(self) -> bool:
        """Save every result that needs no model call; True when a comparison waits for the runner."""
        self._advance()
        return self.task is not None

    def abandon_ranking(self, exc: Exception) -> bool:
        """Record a ranking cut off by a runner failure; ranking is scored separately from the saved watchlist."""
        if self.task is None or self.task["name"] != "q20_rank":
            return False
        log.exception("batch %s ranking failed", self.batch["id"])
        result = self.task["result"]
        self.task = None
        self._save("q20_rank", result | {"order": None, "error": str(exc)})
        return True

    def _group(self, name: str) -> tuple[list[dict], dict]:
        group = "s10" if name == "s10_select" else "q20"
        if group not in self._groups:
            self._groups[group] = ((candidate_digests(self.conn, self.batch | {"q20": self.batch["s10"]}),
                                    small_rule(self.batch["rule"])) if group == "s10"
                                   else (candidate_digests(self.conn, self.batch), self.batch["rule"]))
        return self._groups[group]

    def _saved(self, name: str) -> dict:
        if name == "q20_rank":
            return self.selection.get("ranking") or {}
        return self.small_selection if name == "s10_select" else self.selection

    def _save(self, name: str, result: dict) -> None:
        if name == "q20_rank":
            self.selection = self.selection | {"ranking": result}
        elif name == "q20_select":
            self.selection = result
        else:
            self.small_selection = result
        field, value = (("small_selection", self.small_selection) if name == "s10_select"
                        else ("selection", self.selection))
        row = self.conn.execute(f"UPDATE batches SET {field}=?, updated_at=? WHERE id=? AND status='selecting' RETURNING id",
                                (json.dumps(value, ensure_ascii=False), db.now(), self.batch["id"])).fetchone()
        if row is None:
            raise _BatchChanged

    def _pending(self) -> str | None:
        """Save the steps that need no model call and name the next comparison, if any."""
        if self._step == "q20_select":
            if not self.large_selectable:
                self._save("q20_select", {"judgment": self.judgment, "attempts": [], "draft": {
                    "picks": [], "rejected": [], "concentration": "", "gaps": ["大市值组没有研究通过的候选，全部席位留现金"]}})
                self._step = "s10_select"
            elif "draft" in self.selection and not self.selection.get("error_kind"):
                self._step = "q20_rank"
            else:
                return "q20_select"
        if self._step == "q20_rank":
            ranking = self.selection.get("ranking")
            if "ranking" in self.selection and (ranking is None or "order" not in ranking):
                return "q20_rank"
            self._step = "s10_select"
        if self._step == "s10_select":
            if self.small and not isinstance(self.small_selection.get("draft"), dict):
                if any(selectable(c["report"]) for c in self._group("s10_select")[0]):
                    return "s10_select"
                self._save("s10_select", {"judgment": self.judgment, "attempts": [], "draft": {
                    "picks": [], "rejected": [], "concentration": "", "gaps": ["小市值组没有研究通过且可选的候选，全部席位留现金"]}})
            self._step = None
        return None

    def _advance(self) -> None:
        while self.failure is None and self.task is None and (name := self._pending()):
            # A retried task keeps the attempts already paid for; the transcript starts afresh.
            attempts = list(self._saved(name).get("attempts", []))
            self.task = {"name": name, "result": {"judgment": self.judgment, "attempts": attempts}, "left": None}
            if len(attempts) >= self.attempt_limit:
                self._conclude()
            elif self.budget is not None and self.budget <= 0:
                if name == "q20_rank":
                    self._conclude(BUDGET_SPENT)
                else:
                    self.task, self.failure = None, ScreenError(BUDGET_SPENT)

    def _conclude(self, error: str | None = None) -> None:
        """End the current task without an accepted submission and save why."""
        name, result = self.task["name"], self.task["result"]
        self.task = None
        last = result["attempts"][-1] if result["attempts"] else {}
        kind = None if error else last.get("error_kind")
        error = error or kind or "；".join(last.get("problems") or [NO_SUBMISSION])
        final = result | {_OUTPUT[name]: None, "error": error} | ({"error_kind": kind} if kind else {})
        if name == "q20_select" and not kind:
            final["ranking"] = None
        self._save(name, final)
        # Ranking failure never discards the watchlist; a selection without a draft fails the batch.
        if name == "s10_select" or (name == "q20_select" and kind):
            self.failure = ScreenError(error)

    def _next(self) -> dict:
        if self.task is not None and self.task["left"] is not None:
            self._conclude()
        self._advance()
        if self.task is None:
            return {"status": "done"}
        name = self.task["name"]
        candidates, rule = self._group(name)
        prompts = ranking_prompt(candidates, rule) if name == "q20_rank" else selection_prompt(candidates, rule)
        log.info("batch %s comparison %s: %s candidates, %s prompt characters", self.batch["id"], name,
                 len(candidates), len(prompts["system_prompt"]) + len(prompts["prompt"]))
        left = self.attempt_limit - len(self.task["result"]["attempts"])
        self.task["left"] = left if self.budget is None else min(left, self.budget)
        return {"status": "task", "batch_id": self.batch["id"], "task": name, "tool": SUBMIT_TOOL[name],
                "max_attempts": self.task["left"], **prompts, "profile_sha256": model_profile.PROFILE_SHA256,
                "remaining_ms": max(0, int((self.deadline - time.monotonic()) * 1000))}

    def _record(self, args: dict) -> dict:
        task = self.task
        if task is None or task["left"] is None or args["step"] != task["name"]:
            raise ValueError("调用审计与当前比较任务不符")
        if task["left"] <= 0:
            raise ValueError("比较尝试超过本项上限")
        if self.budget is not None:
            if self.budget <= 0:
                raise ScreenError(BUDGET_SPENT)
            self.budget -= 1
        task["left"] -= 1
        prompt = json.dumps({"messages": args["messages"], "tools": args["tools"]}, ensure_ascii=False)
        identity = args["identity"] | {"stop_reason": args["stop_reason"],
                                       "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                                       "prompts_sha256": self._prompts_sha256}
        detail = {"identity": identity, "usage": args["usage"], "seconds": args["seconds"]}
        if kind := args["error_kind"]:
            detail["error_kind"] = kind
        else:
            detail["problems"] = [NO_SUBMISSION]  # replaced once this attempt's submission is checked
        task["result"]["attempts"].append(detail)
        self.last_error_kind = kind
        # Persist each paid attempt at once: a later transport failure must not lose it.
        self._save(task["name"], task["result"])
        return {"attempt": len(task["result"]["attempts"])}

    def _submit(self, tool: str, args: dict) -> types.CallToolResult:
        task = self.task
        if task is None or task["left"] is None or SUBMIT_TOOL[task["name"]] != tool or not task["result"]["attempts"]:
            raise ValueError("提交与当前比较任务不符")
        name, result = task["name"], task["result"]
        attempt = result["attempts"][-1]
        candidates, rule = self._group(name)
        if problem := next(COMPARISON_VALIDATORS[tool].iter_errors(args), None):
            problems = [f"{problem.json_path}: {problem.message}"]
        elif name == "q20_rank":
            problems = ranking_problems(args, candidates)
        else:
            problems = selection_problems(args, candidates, rule)
        attempt["problems"] = problems
        if problems:
            self._save(name, result)
            return _receipt({"error": "；".join(problems)}, error=True)
        self.task = None
        if name == "q20_rank":
            self._save(name, result | {"order": args["order"], "basis": args["basis"]})
        else:
            final = result | {"draft": args, "prompt_sha256": attempt["identity"]["prompt_sha256"]}
            self._save(name, final | ({"ranking": None} if name == "q20_select" else {}))
        return _receipt({"status": "accepted"})

    async def _abort(self, exc: Exception) -> NoReturn:
        self.failure = self._fatal = exc
        self._scope.cancel()
        await anyio.sleep_forever()

    def _build_server(self) -> Server:
        catalog = {tool.name: tool for tool in COMPARISON_TOOLS}

        async def list_tools(ctx, params):
            return types.ListToolsResult(tools=list(catalog.values()))

        async def call_tool(ctx, params):
            name, args = params.name, params.arguments or {}
            if name not in catalog:
                raise MCPError(types.INVALID_PARAMS, f"工具 {name} 不可用；请用 tools/list 查询本次工具集")
            try:
                if name in ("submit_selection", "submit_ranking"):
                    return self._submit(name, args)
                if problem := next(COMPARISON_VALIDATORS[name].iter_errors(args), None):
                    raise ValueError(f"{name} 参数无效：{problem.json_path}")
                return _receipt(self._record(args) if name == "record_call" else self._next())
            except (_BatchChanged, ScreenError) as exc:
                await self._abort(exc)
            except Exception:
                log.exception("batch %s comparison failed in %s", self.batch["id"], name)
                await self._abort(RuntimeError("批次比较服务执行失败，请核对比较记录"))

        return Server("a-share-batch", on_list_tools=list_tools, on_call_tool=call_tool,
                      get_tool_input_schema=lambda name: catalog[name].input_schema if name in catalog else None)

    async def serve(self, read_stream, write_stream):
        """Serve MCP until the runner's EOF or a fatal error, which the worker then raises."""
        with anyio.CancelScope() as self._scope:
            await self._server.run(read_stream, write_stream, self._server.create_initialization_options())
        if self._fatal is not None:
            raise self._fatal


def _compare_groups(conn, batch, large_selectable, small):
    session = ComparisonSession(conn, batch, large_selectable, small)
    while session.failure is None and session.ready():
        if session.judgment != model_profile.judgment_identity():
            raise ScreenError("批次开始时冻结的判断配置已变更，无法继续比较")
        try:
            runner_process.run_batch(session, batch_id=batch["id"])
            if session.failure is None and session.ready():
                raise runner_process.RunnerError("pipeline_error", "批次 runner 未完成全部比较即退出")
        except _BatchChanged:
            raise
        except Exception as exc:
            if session.failure is not None or not session.abandon_ranking(exc):
                raise
    if session.failure is not None:
        raise session.failure
    return session.selection


# ---------- simulation ----------

def _positive(value) -> bool:
    return value is not None and pd.notna(value) and math.isfinite(float(value)) and float(value) > 0


def entry_codes(batch: dict) -> list[str]:
    codes = [r["code"] for r in batch["q20"]] + batch["l5"]["codes"]
    if batch["rule"]["version"] in {"screen-v1.2", "screen-v1.3"}:
        codes += (batch.get("sq3") or []) + (batch.get("sl3") or {}).get("codes", [])
    return list(dict.fromkeys(codes))


def entry_prices(pro, day: str, codes: list[str]) -> dict:
    """Entry-day opens and adjustment factors; a seat stays cash when suspended or limit-up at the open.

    Missing data that is not a confirmed suspension means the day is not fully published: raise.
    """
    daily = _query(pro, "daily", trade_date=day, fields="ts_code,open,close").set_index("ts_code")
    factor = _query(pro, "adj_factor", trade_date=day).set_index("ts_code")["adj_factor"]
    limit = _query(pro, "stk_limit", trade_date=day).set_index("ts_code")["up_limit"]
    suspensions = _query(pro, "suspend_d", trade_date=day, suspend_type="S")
    suspended = set()
    opening = datetime.strptime("09:30", "%H:%M").time()
    for row in suspensions[suspensions["ts_code"].isin(codes)].itertuples(index=False):
        timing = getattr(row, "suspend_timing", None)
        if timing is None or pd.isna(timing) or timing == "":  # no interval means a full-day suspension
            suspended.add(row.ts_code)
            continue
        try:
            start, end = [datetime.strptime(t, "%H:%M").time() for t in timing.split("-")]
            if start >= end:
                raise ValueError("invalid interval")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScreenError(f"{day} {row.ts_code} 停牌时段无效：{timing}") from exc
        if start <= opening < end:
            suspended.add(row.ts_code)
    prices = {}
    for code in codes:
        if code in suspended:
            prices[code] = {"filled": False, "reason": "入场日停牌"}
            continue
        if code not in daily.index or not _positive(daily.at[code, "open"]):
            raise ScreenError(f"{day} {code} 开盘价未发布")
        if code not in limit.index or not _positive(limit[code]) or code not in factor.index or not _positive(factor[code]):
            raise ScreenError(f"{day} {code} 涨停价或复权因子未发布")
        if daily.at[code, "open"] >= limit[code]:
            prices[code] = {"filled": False, "reason": "入场日开盘涨停，买不进"}
        else:
            prices[code] = {"filled": True, "open": float(daily.at[code, "open"]), "adj": float(factor[code])}
    return prices


PRICE_INDEX = {"H00906.CSI": "000906.SH", "H00300.CSI": "000300.SH", "H00852.CSI": "000852.SH"}


def benchmark_entry_point(previous_close: float, price_open: float, price_previous_close: float) -> float:
    """Total-return entry point from the corresponding price index's opening ratio."""
    if not all(_positive(v) for v in (previous_close, price_open, price_previous_close)):
        raise ScreenError("指数开盘计算点位无效")
    value = float(previous_close) * float(price_open) / float(price_previous_close)
    if not _positive(value):
        raise ScreenError("指数计算点位无效")
    return value


def benchmark_prices(pro, start: str, end: str, field: str, benchmarks: dict) -> dict:
    """Total-return index level: latest close in [start, end], or the entry-day open.

    Total-return indexes publish no open; the open is the previous close scaled by the price
    index's open / previous close on that day (dividends do not move the index intraday).
    """
    out = {}
    for code in benchmarks:
        frame = _query(pro, "index_daily", ts_code=code, start_date=start, end_date=end).sort_values("trade_date")
        if field == "open" and (frame.empty or str(frame["trade_date"].iloc[-1]) != end):
            # Total-return rows publish after the price index; the entry-day pre_close equals the
            # previous session's close (no exception in either benchmark since 2015), so use that.
            prev = [d for d in market.trade_days(pro, _compact(datetime.strptime(end, "%Y%m%d") - timedelta(days=20)), end)
                    if d < end][-1:]
            frame = _query(pro, "index_daily", ts_code=code, start_date=prev[0], end_date=prev[0]) if prev else frame.iloc[0:0]
            if frame.empty or str(frame["trade_date"].iloc[-1]) != prev[0]:
                raise ScreenError(f"{code} {end} 及前一交易日的行情未发布")
            frame = frame.assign(trade_date=end, pre_close=frame["close"])
        if frame.empty or str(frame["trade_date"].iloc[-1]) != end:
            raise ScreenError(f"{code} {end} 的行情未发布")
        last = frame.iloc[-1]
        if last["ts_code"] != code or not _positive(last["close"]):
            raise ScreenError(f"{code} {end} 的指数代码或收盘点位无效")
        value = float(last["close"])
        if field == "open":
            price = _query(pro, "index_daily", ts_code=PRICE_INDEX[code], start_date=end, end_date=end)
            if len(price) != 1 or str(price.iloc[0]["trade_date"]) != end or price.iloc[0]["ts_code"] != PRICE_INDEX[code]:
                raise ScreenError(f"{PRICE_INDEX[code]} {end} 的行情未发布或日期/代码不符")
            price = price.iloc[0]
            if not all(_positive(v) for v in (last["pre_close"], price["open"], price["pre_close"])):
                raise ScreenError(f"{code} {end} 的开盘计算点位无效")
            value = benchmark_entry_point(last["pre_close"], price["open"], price["pre_close"])
        if not _positive(value):
            raise ScreenError(f"{code} {end} 的计算点位无效")
        out[code] = {"date": str(last["trade_date"]), "value": value}
    return out


def mark_on(pro, batch: dict, day: str, previous: dict | None = None) -> dict:
    """Close × factor on `day` for every filled seat; a suspended code keeps its last valid close."""
    codes = [c for c, p in batch["entry"]["prices"].items() if p.get("filled")]
    daily = _query(pro, "daily", trade_date=day, fields="ts_code,close").set_index("ts_code")["close"]
    factor = _query(pro, "adj_factor", trade_date=day).set_index("ts_code")["adj_factor"]
    if daily.empty or factor.empty:
        raise ScreenError(f"{day} 日线或复权因子尚未发布")
    missing = [c for c in codes if c not in daily.index]
    suspended = set(_query(pro, "suspend_d", trade_date=day, suspend_type="S")["ts_code"]) if missing else set()
    if unconfirmed := [c for c in missing if c not in suspended]:
        raise ScreenError(f"{day} {'、'.join(unconfirmed)} 行情未发布且无停牌记录")
    prices = {}
    for code in codes:
        if code in daily.index:
            if code not in factor.index or not _positive(daily[code]) or not _positive(factor[code]):
                raise ScreenError(f"{day} {code} 收盘价或复权因子无效")
            prices[code] = {"close": float(daily[code]), "adj": float(factor[code]), "date": day}
        elif previous and code in previous["prices"]:
            prices[code] = previous["prices"][code]
        else:  # suspended on `day`: last valid close since entry
            bars = _query(pro, "daily", ts_code=code, start_date=batch["entry"]["date"], end_date=day,
                          fields="ts_code,trade_date,close").sort_values("trade_date")
            bars = bars[bars["close"].map(_positive)]
            if bars.empty:
                raise ScreenError(f"{code} 入场后没有有效收盘价")
            last = bars.iloc[-1]
            adj = _query(pro, "adj_factor", ts_code=code, trade_date=last["trade_date"])["adj_factor"]
            if adj.empty or not _positive(adj.iloc[0]):
                raise ScreenError(f"{code} {last['trade_date']} 复权因子缺失")
            prices[code] = {"close": float(last["close"]), "adj": float(adj.iloc[0]), "date": str(last["trade_date"])}
    return {"date": day, "prices": prices,
            "benchmarks": benchmark_prices(pro, batch["entry"]["date"], day, "close", batch["rule"]["benchmarks"])}


def benchmark_day(pro, start: str, end: str, benchmarks: dict) -> str | None:
    """Latest session in [start, end] on which every benchmark close is published (they lag the stocks)."""
    days = None
    for code in benchmarks:
        frame = _query(pro, "index_daily", ts_code=code, start_date=start, end_date=end)
        published = set(frame["trade_date"].astype(str)) if not frame.empty else set()
        days = published if days is None else days & published
    return max(days) if days else None


def update_marks(pro, batch: dict, day: str) -> dict:
    """Latest mark plus each horizon valued on its own first trading day on or after the target date."""
    marks = batch.get("marks") or {}
    latest = mark_on(pro, batch, day, marks.get("latest"))
    marks = marks | {"latest": latest}
    for months in batch["rule"]["horizons_months"]:
        key, target = f"{months}m", _months_after(batch["entry"]["date"], months)
        if key in marks.get("horizons", {}) or day < target:
            continue
        hday = next(d for d in market.trade_days(pro, target, day))  # day itself is a trading day >= target
        mark = latest if hday == day else mark_on(pro, batch, hday)
        marks = marks | {"horizons": (marks.get("horizons") or {}) | {key: returns(batch | {"marks": {"latest": mark}})}}
    return marks


# ---------- worker ----------

RETRY_MINUTES = 30


def _waiting(batch: dict, now: datetime) -> bool:
    """A failed fetch or comparison is retried only after RETRY_MINUTES."""
    attempted = (batch.get("marks") or {}).get("attempted")
    return bool(attempted) and now - datetime.fromisoformat(attempted) < timedelta(minutes=RETRY_MINUTES)


def _draft_picks(selection: dict | None) -> list[str]:
    return [p["code"] for p in ((selection or {}).get("draft") or {}).get("picks", [])]


def _cash_large_draft(batch: dict, selection: dict | None) -> bool:
    """Only completed dual-pool drafts without selectable large candidates freeze empty L5."""
    return (batch["rule"]["version"] in {"screen-v1.2", "screen-v1.3"}
            and not any(r.get("verdict") and r["verdict"] != "暂不值得" for r in batch["q20"])
            and isinstance((selection or {}).get("draft"), dict)
            and isinstance((batch.get("small_selection") or {}).get("draft"), dict))


def _auto_freeze(conn, batch_id: int, selection: dict, expected_status: str = "selecting") -> bool:
    """Save and freeze a draft with picks or no selectable large candidates atomically.

    The owner chose (2026-10-25) to freeze the model's draft as soon as it exists. A draft that
    fails freeze validation stays proposed with the reason, for the owner to pick from.
    """
    picks = _draft_picks(selection)
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None or row["status"] != expected_status:
        return False
    batch = load(row)
    # Prepare the shared calendar even for an empty large draft awaiting an owner decision.
    # No report_rc backfill is performed; freeze records whatever is cached at that time.
    coverage = sources.batch_coverage(batch) if batch["rule"]["version"] in {"screen-v1.2", "screen-v1.3"} else None
    with db.transaction(conn):
        row = conn.execute("SELECT status, selection FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None or row["status"] != expected_status:
            return False
        if expected_status == "proposed" and json.loads(row["selection"] or "null") != selection:
            return False
        _save(conn, batch_id, status="proposed", selection=selection, error=None, marks=None)
        if coverage is not None:
            _save(conn, batch_id, coverage=coverage)
        if picks or _cash_large_draft(batch, selection):
            try:
                freeze(conn, batch_id, picks, "", by="auto", coverage=coverage)
                log.info("batch %s frozen with the model draft", batch_id)
            except service.RequestError as exc:
                log.warning("batch %s draft could not be frozen: %s", batch_id, exc)
                _save(conn, batch_id, status="proposed", selection=selection | {"freeze_error": str(exc)},
                      error=f"草案未能自动冻结：{exc}", marks=None)
    return True


def recover(conn) -> None:
    """Interrupted automatic monthly work never restarts paid comparison; manual behavior is unchanged."""
    conn.execute("UPDATE batches SET status = 'failed', step = NULL, error = '初筛中断（服务重启），请重新开始',"
                 " updated_at = ? WHERE status = 'screening'", (db.now(),))
    conn.execute("UPDATE batches SET status = 'failed', step = NULL, error = '自动月批比较中断，不自动重跑',"
                 " updated_at = ? WHERE status = 'selecting' AND request_id LIKE 'monthly:%'", (db.now(),))
    conn.execute("UPDATE batches SET status = 'researching', step = NULL WHERE status = 'selecting'")


def _marks_through(pro, batch: dict, day: str, stamp: str) -> dict:
    """Marks up to the latest session with published benchmarks; until that reaches `day`, retry later."""
    marked = benchmark_day(pro, batch["entry"]["date"], day, batch["rule"]["benchmarks"])
    marks = batch.get("marks") or {}
    # A saved mark for that session already carries every horizon due by then; do not revalue it.
    if marked and marked != (marks.get("latest") or {}).get("date"):
        marks = update_marks(pro, batch, marked)
    if marked == day:
        return marks | {"checked": stamp, "attempted": None}
    return marks | {"attempted": db.now()}


def tick(conn) -> bool:
    """Advance at most one batch by one step; returns True when it did work."""
    rows = conn.execute(f"SELECT {', '.join(POLL_COLUMNS)} FROM batches WHERE status IN ('queued', 'researching', 'proposed', 'frozen', 'tracking')"
                        " ORDER BY id").fetchall()
    now = datetime.now(SHANGHAI)
    for r in rows:
        batch = load(r)
        try:
            if batch["status"] == "queued":
                request_id = str(batch.get("request_id") or "")
                if request_id.startswith("monthly:") and request_id != f"monthly:{now:%Y-%m}":
                    raise ScreenError("自动月批已错过发起月份，不使用后来数据补跑")
                started = conn.execute("UPDATE batches SET status = 'screening', step = 'screen', marks = ?, updated_at = ?"
                                       " WHERE id = ? AND status = 'queued' RETURNING id",
                                       (json.dumps({"judgment": model_profile.judgment_identity()}), db.now(), batch["id"])).fetchone()
                if started is None:
                    return False
                result = screen(rule=batch["rule"])
                if not result["q20"]:
                    raise ScreenError("没有合格候选")
                # Research submissions and the batch's links commit together.
                with db.transaction(conn):
                    if conn.execute("SELECT 1 FROM batches WHERE id = ? AND status = 'screening'",
                                    (batch["id"],)).fetchone() is None:
                        return False
                    q20 = link_research(conn, batch["id"], result["q20"])
                    small = {key: result[key] for key in ("s10", "sq3") if key in result}
                    _save(conn, batch["id"], status="researching", step=None, as_of=result["as_of"],
                          universe=result["universe"], q20=q20, q5=result["q5"], **small)
                return True
            if batch["status"] == "proposed":
                # A draft with picks normally freezes in the same transaction that saved it; this
                # catches one left behind by an older version, including an all-cash L5.
                # Empty drafts with selectable large candidates still wait for the owner.
                if (batch["selection"] or {}).get("freeze_error"):
                    continue
                if not _draft_picks(batch["selection"]) and not _cash_large_draft(batch, batch["selection"]):
                    continue
                return _auto_freeze(conn, batch["id"], batch["selection"], expected_status="proposed")
            if batch["status"] == "researching":
                finished, q20 = research_state(conn, batch["q20"])
                if not finished or _waiting(batch, now):
                    continue
                small = batch["rule"]["version"] in {"screen-v1.2", "screen-v1.3"}
                s10 = batch.get("s10") or []
                if small:
                    if any(not c.get("run_id") for c in s10):
                        # Queue small companies only after Q20 finishes, retaining researching
                        # throughout so deployment's idle check still sees the batch as busy.
                        with db.transaction(conn):
                            s10 = link_research(conn, batch["id"], s10)
                            _save(conn, batch["id"], q20=q20, s10=s10)
                        return True
                    small_finished, s10 = research_state(conn, s10)
                    if not small_finished:
                        continue
                large_selectable = any(r["verdict"] and r["verdict"] != "暂不值得" for r in q20)
                if not large_selectable and not (small and any(r["verdict"] and r["verdict"] != "暂不值得" for r in s10)):
                    raise ScreenError("没有研究通过的候选，无法比较；可重新开始本月批次")
                # Stage the start identity in marks until a comparison actually exists. Readers
                # treat a nonempty selection during research as an already returned model draft.
                selection = batch.get("selection") or {"judgment": (batch.get("marks") or {}).get("judgment")
                                                       or model_profile.judgment_identity()}
                batch.update(q20=q20, s10=s10, selection=selection)
                started = conn.execute("UPDATE batches SET status='selecting', q20=?, s10=?, selection=?, updated_at=?"
                                       " WHERE id=? AND status='researching' AND q20=? RETURNING id",
                                       (json.dumps(q20, ensure_ascii=False), json.dumps(s10, ensure_ascii=False) if small else r["s10"],
                                        json.dumps(selection, ensure_ascii=False), db.now(), batch["id"], r["q20"])).fetchone()
                if started is None:
                    return False
                try:
                    selection = _compare_groups(conn, batch, large_selectable, small)
                except _BatchChanged:
                    return False
                except Exception as exc:  # the researched candidates stay; retry the comparison later
                    log.exception("batch %s comparison failed", batch["id"])
                    failures = ((batch["selection"] or {}).get("failures") or 0) + 1
                    if str(batch.get("request_id") or "").startswith("monthly:") or failures >= 3:
                        raise ScreenError(f"跨公司比较连续 {failures} 次失败：{exc}") from exc
                    saved_selection = json.loads(conn.execute("SELECT selection FROM batches WHERE id = ?",
                                                              (batch["id"],)).fetchone()[0] or "{}")
                    updated = conn.execute("UPDATE batches SET status = 'researching', error = ?, selection = ?,"
                                           " marks = ?, updated_at = ? WHERE id = ? AND status = 'selecting' RETURNING id",
                                           (f"跨公司比较失败，稍后重试：{exc}", json.dumps(saved_selection | {"failures": failures}),
                                            json.dumps({"attempted": db.now()}), db.now(), batch["id"])).fetchone()
                    return updated is not None
                return _auto_freeze(conn, batch["id"], selection)
            stamp = now.strftime("%Y%m%d")
            if now.hour < MARK_HOUR or (batch.get("marks") or {}).get("checked") == stamp or _waiting(batch, now):
                continue
            pro = market.client()
            if batch["status"] == "frozen":
                frozen = datetime.fromisoformat(batch["frozen_at"]).strftime("%Y%m%d")
                days = [d for d in market.trade_days(pro, frozen, stamp) if d > frozen]
                if not days:
                    _save(conn, batch["id"], marks=(batch.get("marks") or {}) | {"checked": stamp, "attempted": None})
                    continue
                codes = entry_codes(batch)
                entry = {"date": days[0], "prices": entry_prices(pro, days[0], codes),
                         "benchmarks": benchmark_prices(pro, days[0], days[0], "open", batch["rule"]["benchmarks"])}
                batch = batch | {"entry": entry}
                marks = _marks_through(pro, batch, days[-1], stamp)
                _save(conn, batch["id"], status="closed" if horizons_complete(batch["rule"], marks) else "tracking",
                      entry=entry, error=None, marks=marks)
                return True
            day = market.trade_days(pro, batch["entry"]["date"], stamp)[-1]
            marks = _marks_through(pro, batch, day, stamp)
            _save(conn, batch["id"], status="closed" if horizons_complete(batch["rule"], marks) else "tracking",
                  error=None, marks=marks)
            return True
        except Exception as exc:
            if not isinstance(exc, ScreenError):
                log.exception("batch %s failed", batch["id"])
            if batch["status"] in ("queued", "researching"):
                active = "screening" if batch["status"] == "queued" else "selecting"
                updated = conn.execute("UPDATE batches SET status = 'failed', step = NULL, error = ?, updated_at = ?"
                                       " WHERE id = ? AND status IN (?, ?) RETURNING id",
                                       (str(exc) or type(exc).__name__, db.now(), batch["id"],
                                        batch["status"], active)).fetchone()
            else:  # frozen/tracking keep going; retried after RETRY_MINUTES
                marks = (batch.get("marks") or {}) | {"attempted": db.now()}
                updated = conn.execute("UPDATE batches SET error = ?, marks = ?, updated_at = ?"
                                       " WHERE id = ? AND status = ? AND marks IS ? AND selection IS ? RETURNING id",
                                       (str(exc) or type(exc).__name__, json.dumps(marks, ensure_ascii=False), db.now(),
                                        batch["id"], batch["status"], r["marks"], r["selection"])).fetchone()
            return updated is not None
    return False


def main() -> None:
    """Command-line screen without saving a batch: prints the funnel and Q20."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = screen()
    print(json.dumps({"as_of": result["as_of"], "funnel": result["universe"]["funnel"],
                      "q20": [{k: r.get(k) for k in ("code", "name", "industry", "score", "roe", "cash", "growth", "pe")}
                              for r in result["q20"]], "q5": result["q5"]}, ensure_ascii=False, indent=1,
                     default=lambda v: None if isinstance(v, float) and math.isnan(v) else v))


if __name__ == "__main__":
    main()
