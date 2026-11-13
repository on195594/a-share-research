"""Judgment validation: V0 excess return after each report, V1 checkpoint forecast scoring, V4 stance
scoring, V5 agenda status (read-only, no model calls).

Scores the verdicts already frozen in passed reports against the CSI 800 total-return index, from the
close of the first full session after the report finished. Only the aggregate is meaningful; one
stock's outcome says nothing about the research. See docs/judgment-validation.md.
"""
import argparse
import json
import logging
import sqlite3
import statistics
from contextlib import closing
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from a_share_research import market, agenda, forecasts, report as report_body, screening, stance as stances
from a_share_research.db import SHANGHAI

log = logging.getLogger(__name__)

BENCHMARK = "H00906.CSI"  # 中证800全收益
HORIZONS = (20, 60, 120)  # trading days after entry
LEFT_TAIL = -0.30
SESSION_OPEN = time(9, 30)


# ---------- pure functions ----------


def judgment_contract(body: dict) -> int | None:
    return (body.get("judgment") or {}).get("contract")


def filter_contract(rows: list[dict], contract: int | None) -> list[dict]:
    """Apply before any first-report dedup, so another contract's earlier report never takes this contract's slot."""
    return rows if contract is None else [r for r in rows if r.get("contract") == (contract or None)]


def contract_counts(rows: list[dict]) -> list[dict]:
    """Count distinct input reports (not checkpoint/observation rows), or batches."""
    groups = {}
    for row in rows:
        key = row.get("contract")
        identity = ("batch", row["batch"]) if "batch" in row else ("report", row.get("report", row.get("id")))
        groups.setdefault(key, set()).add(identity)
    return [{"contract": key, "label": "未记录" if key is None else str(key), "count": len(groups[key])}
            for key in sorted(groups, key=lambda k: -1 if k is None else k)]


def contract_line(rows: list[dict]) -> str:
    return "按判断契约的入样数：" + ("，".join(f"{g['label']} {g['count']}" for g in contract_counts(rows)) or "无")


def source(request: dict) -> str | None:
    """'batch' for screening deep dives, 'active' for owner/agent research, None for feedback checks."""
    if request.get("feedback"):
        return None
    return "batch" if request.get("question") == screening.QUESTION else "active"


def entry_day(completed: datetime, trade_days: list[str]) -> str | None:
    """First session whose open is at or after the report's completion (a full session of trading on it)."""
    for day in trade_days:
        if datetime.combine(date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}"), SESSION_OPEN, SHANGHAI) >= completed:
            return day
    return None


def first_per_month(reports: list[dict]) -> list[dict]:
    """Keep each (source, code, natural month)'s earliest report so repeated research is not counted twice."""
    seen, kept = set(), []
    for report in sorted(reports, key=lambda r: r["completed"]):
        key = (report["source"], report["code"], report["completed"].strftime("%Y-%m"))
        if key not in seen:
            seen.add(key)
            kept.append(report)
    return kept


def value_on(series: dict[str, float], day: str) -> float | None:
    """Latest value on or before `day` (a suspended stock keeps its last close)."""
    days = [d for d in series if d <= day]
    return series[max(days)] if days else None


def excess(stock: dict[str, float], bench: dict[str, float], start: str, end: str, *,
           stock_start: float | None = None, benchmark_start: float | None = None) -> dict | None:
    """Simple stock and benchmark returns from the `start` close to the `end` close, and their difference."""
    s0, s1 = stock.get(start) if stock_start is None else stock_start, value_on(stock, end)
    b0, b1 = bench.get(start) if benchmark_start is None else benchmark_start, bench.get(end)
    if s0 is None or s1 is None or b0 is None or b1 is None:
        return None
    stock_ret, bench_ret = s1 / s0 - 1, b1 / b0 - 1
    return {"stock": stock_ret, "benchmark": bench_ret, "excess": stock_ret - bench_ret}


def horizon_dates(trade_days: list[str], entry: str, last: str) -> dict[str, str]:
    """End date of each matured horizon plus 'latest'; a horizon not yet reached by `last` is omitted."""
    after = [d for d in trade_days if entry <= d <= last]
    out = {f"{h}d": after[h] for h in HORIZONS if len(after) > h}
    if len(after) > 1:
        out["latest"] = after[-1]
    return out


def summarize(rows: list[dict], key: str) -> list[dict]:
    """Per (source, `key` group, horizon): count, mean/median excess, share beating the index, left-tail share."""
    groups: dict[tuple, list[float]] = {}
    for row in rows:
        for horizon, result in row["returns"].items():
            groups.setdefault((row["source"], row[key], horizon), []).append(result["excess"])
    out = []
    for (src, group, horizon), values in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]), kv[0][2])):
        out.append({"source": src, key: group, "horizon": horizon, "n": len(values),
                    "mean": statistics.fmean(values), "median": statistics.median(values),
                    "beat": sum(v > 0 for v in values) / len(values),
                    "left_tail": sum(v <= LEFT_TAIL for v in values) / len(values)})
    return out


def summarize_forecasts(scored: list[dict]) -> dict:
    """Calibration and naive-baseline share over first forecasts only; updates are counted, not scored twice.

    Record counts are not independent judgments: companies, target periods and updates are reported alongside.
    """
    first = [s for s in scored if s["first"]]
    done = [s for s in first if s["result"]["status"] == "scored"]
    by_confidence = {}
    for level in forecasts.CONFIDENCE:
        group = [s for s in done if s["forecast"]["confidence"] == level]
        if group:
            by_confidence[level] = {"n": len(group), "hit": sum(s["result"]["hit"] for s in group) / len(group)}
    compared = [s for s in done if s["result"]["beat_baseline"] is not None]
    clean_compared = [s for s in compared if _item_baseline_note(s) is None]
    return {"total": len(first), "updates": len(scored) - len(first), "scored": len(done), "pending": sum(s["result"]["status"] == "pending" for s in first),
            "unavailable": sum(s["result"]["status"] == "unavailable" for s in first),
            "unchecked": sum(s["result"]["status"] == "unchecked" for s in first),
            "companies": len({s["code"] for s in first}), "periods": len({s["forecast"]["period"] for s in first}),
            "restated": sum(bool(s["result"].get("restated")) for s in done),
            "by_confidence": by_confidence,
            "beat_baseline": sum(s["result"]["beat_baseline"] for s in compared) / len(compared) if compared else None,
            "compared": len(compared),
            "distorted_compared": len(compared) - len(clean_compared),
            "clean_beat_baseline": sum(s["result"]["beat_baseline"] for s in clean_compared) / len(clean_compared) if clean_compared else None,
            "clean_compared": len(clean_compared)}


def model_roles(body: dict) -> dict:
    """Frozen configuration only, never infer historical roles from today's environment."""
    judgment = body.get("judgment") or {}
    return {role: {key: (judgment.get(role) or {}).get(key) for key in ("provider", "model", "reasoning")}
            for role in ("author", "reviewer")}


def model_label(role: dict) -> str:
    return " / ".join(str(role.get(k) or "未记录") for k in ("provider", "model", "reasoning"))


def _item_baseline_note(item: dict) -> str | None:
    if "diagnostics" in item:
        return item["diagnostics"].get("baseline_note")
    written = item.get("written")
    base = item.get("base")
    if not written or not base:
        return None
    frozen = {p: Decimal(str(v)) for p, v in (written.get("exact_values") or written.get("values") or {}).items()}
    return baseline_distortion(item["forecast"], base, written, frozen)


def baseline_distortion(forecast: dict, base: str, written: dict, frozen: dict[str, Decimal]) -> str | None:
    """Flag cumulative-flow naive baselines distorted by sign flips, negative bases, or tiny denominators."""
    metric, unit = forecast.get("metric"), written.get("unit", "")
    if unit != "亿元" or metric in forecasts.BALANCES:
        return None
    period = forecast.get("period", "")
    prior_base, prior_period = f"{int(base[:4]) - 1}{base[4:]}", f"{int(period[:4]) - 1}{period[4:]}"
    if base not in frozen or prior_base not in frozen or prior_period not in frozen or frozen[prior_base] == 0:
        return None
    vb, vpb = frozen[base], frozen[prior_base]
    if vb * vpb < 0:
        return "基期与上年同期异号，比例外推失真"
    if vpb < 0:
        return "上年同期基期为负，比例外推失真"
    if abs(vb / vpb) >= 5:
        return "基期同比变动≥5倍，比例外推可能失真"
    return None


def forecast_diagnostics(item: dict) -> dict:
    """Descriptive measures, not a new score. Percentage differences are percentage points; original value units are retained."""
    forecast, written, result = item["forecast"], item["written"], item["result"]
    low, high = Decimal(str(forecast["low"])), Decimal(str(forecast["high"]))
    width, midpoint = high - low, (high + low) / 2
    frozen = {p: Decimal(str(v)) for p, v in (written.get("exact_values") or written.get("values") or {}).items()}
    naive = forecasts.baseline(forecast["metric"], written.get("unit", ""), forecast["period"], item["base"], frozen)
    # A zero denominator has no relative interpretation, not zero error or width.
    ratio = lambda n, d: float(n / abs(d)) if d is not None and d != 0 else None
    out = {"width": float(width), "unit": written.get("unit", ""),
           "difference_unit": "百分点" if written.get("unit") == "%" else written.get("unit", ""),
           "baseline": float(naive) if naive is not None else None,
           "baseline_note": baseline_distortion(forecast, item["base"], written, frozen),
           "width_to_baseline": ratio(width, naive), "width_to_actual": None,
           "midpoint_absolute_error": None, "baseline_absolute_error": None}
    if result["status"] == "scored":
        actual = Decimal(str(result["actual"]))
        out.update(width_to_actual=ratio(width, actual), midpoint_absolute_error=float(abs(midpoint - actual)),
                   baseline_absolute_error=float(abs(naive - actual)) if naive is not None else None)
    return out


def _group_identity(item: dict) -> tuple[int | None, str, str]:
    roles = item.get("models") or model_roles({})
    return (item.get("contract"), model_label(roles["author"]), model_label(roles["reviewer"]))


def forecast_review(scored: list[dict]) -> dict:
    """Use the existing first-sample flags before grouping; never give a rerun a new scoring slot."""
    items = [dict(item, diagnostics=forecast_diagnostics(item)) for item in scored]
    groups = {}
    shadowed = {}
    for item in items:
        key = _group_identity(item)
        (groups if item["first"] else shadowed).setdefault(key, []).append(item)
    cohorts = []
    # Stable identity order, not a model performance ranking.
    for (contract, author, reviewer), group in sorted(groups.items(), key=lambda pair: str(pair[0])):
        strata = {}
        for item in group:
            key = (item["forecast"]["confidence"], item["forecast"]["metric"], item["diagnostics"]["unit"])
            strata.setdefault(key, []).append(item)
        width_groups = []
        for (confidence, metric, unit), rows in sorted(strata.items(), key=lambda pair: str(pair[0])):
            widths = [i["diagnostics"]["width_to_baseline"] for i in rows
                      if i["diagnostics"]["width_to_baseline"] is not None]
            clean_widths = [i["diagnostics"]["width_to_baseline"] for i in rows
                            if i["diagnostics"]["width_to_baseline"] is not None
                            and i["diagnostics"]["baseline_note"] is None]
            distorted = sum(1 for i in rows if i["diagnostics"]["baseline_note"] is not None)
            width_groups.append({"confidence": confidence, "metric": metric, "unit": unit,
                                 **summarize_forecasts(rows), "width_compared": len(widths),
                                 "median_width_to_baseline": statistics.median(widths) if widths else None,
                                 "distorted_baselines": distorted,
                                 "clean_width_compared": len(clean_widths),
                                 "clean_median_width_to_baseline": statistics.median(clean_widths) if clean_widths else None})
        summary = summarize_forecasts(group)
        cohorts.append({"contract": contract, "author": author, "reviewer": reviewer,
                        "reports": len({i["report"] for i in group}), **summary,
                        "width_groups": width_groups})
    shadowed_cohorts = []
    for (contract, author, reviewer), rows in sorted(shadowed.items(), key=lambda pair: str(pair[0])):
        if (contract, author, reviewer) in groups:
            continue
        shadowed_cohorts.append({"contract": contract, "author": author, "reviewer": reviewer,
                                 "reports": len({i["report"] for i in rows}),
                                 "companies": len({i["code"] for i in rows}),
                                 "updates": len(rows)})
    first_by_contract = {}
    for i in items:
        if i["first"] and i.get("contract") is not None:
            first_by_contract[i["contract"]] = first_by_contract.get(i["contract"], 0) + 1
    contract_independent_first = []
    for c in sorted({i.get("contract") for i in items if i.get("contract") is not None}):
        indep_rows = mark_first([dict(i) for i in items if i.get("contract") == c])
        indep_summary = summarize_forecasts(indep_rows)
        if indep_summary["total"] > first_by_contract.get(c, 0):
            contract_independent_first.append({"contract": c, **indep_summary})
    first_items = [i for i in items if i["first"]]
    confidence_coverage = [
        {"confidence": level,
         "total": sum(1 for i in first_items if i["forecast"]["confidence"] == level),
         "scored": sum(1 for i in first_items if i["forecast"]["confidence"] == level and i["result"]["status"] == "scored"),
         "pending": sum(1 for i in first_items if i["forecast"]["confidence"] == level and i["result"]["status"] == "pending"),
         "unchecked": sum(1 for i in first_items if i["forecast"]["confidence"] == level and i["result"]["status"] == "unchecked"),
         "unavailable": sum(1 for i in first_items if i["forecast"]["confidence"] == level and i["result"]["status"] == "unavailable")}
        for level in forecasts.CONFIDENCE
    ]
    return {"items": items, "summary": summarize_forecasts(items), "model_groups": cohorts,
            "shadowed_model_groups": shadowed_cohorts, "contract_independent_first": contract_independent_first,
            "confidence_coverage": confidence_coverage,
            "generated_at": datetime.now(SHANGHAI).isoformat(),
            "scope": "研究预测；非模拟组合收益，非真实账户表现。模型分组仅描述冻结配置，不排名、不作因果归因。"}


def frozen_forecasts(items: list[dict], today: date | None = None) -> list[dict]:
    """No market/model calls: ended periods are unchecked, not scored or presumed unavailable."""
    today = today or datetime.now(SHANGHAI).date()
    return [dict(item, result={"status": "unchecked" if period_ended(item["forecast"]["period"], today) else "pending",
                               "reason": "只读冻结记录，未查询兑现数据"}) for item in items]


def write_forecast_html(review: dict, path: str) -> None:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(loader=FileSystemLoader(Path(__file__).with_name("templates")), autoescape=select_autoescape())
    env.filters["review_pct"] = lambda v: f"{v:.1%}" if v is not None else "—"
    Path(path).write_text(env.get_template("forecast_review.html").render(review=review), encoding="utf-8")


def mark_first(items: list[dict]) -> list[dict]:
    """A forecast's identity is company × metric × target period; the earliest report holds the first one."""
    seen = set()
    for item in sorted(items, key=lambda i: (i["created"], i["report"], i["index"])):
        key = (item["code"], item["forecast"]["metric"], item["forecast"]["period"])
        item["first"] = key not in seen
        seen.add(key)
    return items


def price_wait(lows: dict[str, float], price: float, entry: str, until: str, last: str) -> dict:
    """等价格 outcome: first session from entry through `until` whose unadjusted low touched the price.

    The price is in the report-day unadjusted basis; an ex-dividend inside the window lowers later lows by
    the dividend, slightly favouring a hit. 'pending' while the window has not closed in the data.
    """
    end = min(until.replace("-", ""), last)
    hit = next((d for d in sorted(lows) if entry <= d <= end and lows[d] <= price), None)
    if hit:
        return {"status": "hit", "date": hit}
    return {"status": "missed" if last >= until.replace("-", "") else "pending"}


def evidence_wait(trigger: dict, row: dict, today: date) -> dict:
    """等证据 outcome from the disclosed financial table row (values are stored as text)."""
    if not period_ended(trigger["period"], today):
        return {"status": "pending"}
    actual = (row.get("values") or {}).get(trigger["period"])
    met = stances.met(trigger, actual)
    if met is None:
        return {"status": "pending", "reason": "目标期尚未披露或数值缺失"}
    return {"status": "met" if met else "not_met", "actual": actual}


def checkpoint_outcome(row: dict, table: dict, today: date) -> dict:
    """Score frozen new checkpoints and legacy waits against the same observed financial row."""
    calc = row.get("stance_calc") or {}
    trigger = calc.get("add_trigger") if row["stance"] == "现在分批" else calc.get("trigger", row.get("trigger"))
    if not trigger:
        return {}
    return evidence_wait(trigger, table.get(trigger["metric"], {}), today)


def summarize_stances(rows: list[dict]) -> dict:
    """Counts of wait outcomes plus each wait's V0 excess at the latest date (the cost or saving of waiting)."""
    out = {}
    for row in rows:
        action, outcome = row["stance"], row.get("stance_outcome") or {}
        key = f"{action}·{outcome['status']}" if outcome else action
        # Boundary stances (close within stance.BOUNDARY of the hurdle) are scored apart, never pooled.
        group = out.setdefault(key + ("·边界" if row.get("boundary") else ""), {"n": 0, "excess": []})
        group["n"] += 1
        if latest := row["returns"].get("latest"):
            group["excess"].append(latest["excess"])
    return {key: {"n": g["n"], "scored": len(g["excess"]),
                  "mean_excess": statistics.fmean(g["excess"]) if g["excess"] else None}
            for key, g in sorted(out.items())}


def size_bucket(mv: float | None, cuts: tuple[float, float]) -> str:
    if mv is None:
        return "未知"
    return "小" if mv <= cuts[0] else "中" if mv <= cuts[1] else "大"


# ---------- data ----------

def load_forecasts(database: str, contract: int | None = None) -> list[dict]:
    """Every checkpoint forecast in passed, non-replay research reports (corrections excluded, as in V0)."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT r.id, r.code, r.name, r.created_at, r.body FROM reports r JOIN runs u ON u.id = r.run_id "
            "WHERE r.passed = 1 AND u.kind = 'research' AND u.replay_of IS NULL "
            "AND r.body LIKE '%\"forecast\"%'").fetchall()
    out = []
    for report_id, code, name, created, body in rows:
        body = report_body.load(body)
        table = body.get("table") or []
        base = forecasts.base_period(table)
        written = {r["id"]: r for r in table}
        for i, item in enumerate(body.get("checkpoints") or []):
            forecast = item.get("forecast") if isinstance(item, dict) else None
            if base and forecast and not forecasts.problems(forecast, table):
                out.append({"report": report_id, "code": code, "name": name, "created": created, "index": i,
                            "contract": judgment_contract(body), "models": model_roles(body), "base": base, "forecast": forecast, "written": written[forecast["metric"]]})
    return mark_first(filter_contract(out, contract))


def period_ended(period: str, today: date) -> bool:
    """A period cannot be disclosed before the day after it ends; skips the financials fetch (~2.5 s/company)."""
    return date.fromisoformat(period) < today


def score_forecasts(items: list[dict], today: date | None = None) -> list[dict]:
    from a_share_research import material, sources  # provider imports only when scoring
    today = today or datetime.now(SHANGHAI).date()
    tables = {}
    for item in items:
        if not period_ended(item["forecast"]["period"], today):
            item["result"] = {"status": "pending"}
            continue
        if item["code"] not in tables:
            tables[item["code"]] = {r["id"]: r for r in material.financial_table(sources.financials(item["code"]), {})}
        row = tables[item["code"]].get(item["forecast"]["metric"], {})
        item["result"] = forecasts.score(item["forecast"], item["base"], row, item["written"])
    return items


def load_reports(database: str, contract: int | None = None) -> list[dict]:
    """Passed, non-replay, non-feedback research reports from a read-only connection."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT r.id, r.code, r.name, r.verdict, r.created_at, u.request, "
            "r.body FROM reports r "
            "JOIN runs u ON u.id = r.run_id WHERE r.passed = 1 AND u.kind = 'research' AND u.replay_of IS NULL"
        ).fetchall()
    reports = []
    for report_id, code, name, verdict, created, request, body in rows:
        src = source(json.loads(request) if request else {})
        if src is None:
            continue
        body = report_body.load(body)
        calc = body.get("stance_calc")
        reports.append({"id": report_id, "code": code, "name": name, "verdict": verdict, "source": src,
                        "completed": datetime.fromisoformat(created), "contract": judgment_contract(body),
                        "stance": calc["action"] if calc else "未记录", "stance_calc": calc,
                        "trigger": calc.get("trigger", (body.get("stance") or {}).get("trigger")) if calc else None,
                        "add_trigger": calc.get("add_trigger") if calc else None,
                        "boundary": stances.on_boundary(body.get("valuation_calc"))})
    return first_per_month(filter_contract(reports, contract))


def load_agenda(database: str, contract: int | None = None) -> list[dict]:
    """Passed, non-replay, non-feedback continuations with their prior period and status, if recorded."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT r.id, r.code, r.name, r.created_at, r.coverage, u.request, "
            "r.body FROM reports r "
            "JOIN runs u ON u.id = r.run_id WHERE r.passed = 1 AND u.kind = 'research' AND u.replay_of IS NULL "
            "AND json_type(r.body, '$.prior') = 'object'"
        ).fetchall()
    reports = []
    for report_id, code, name, created, coverage, request, body in rows:
        body = report_body.load(body)
        if src := source(json.loads(request or "{}") or {}):
            reports.append({"id": report_id, "code": code, "name": name, "source": src, "period": coverage,
                            "completed": datetime.fromisoformat(created), "prior_period": body["prior"].get("period"),
                            "contract": judgment_contract(body), "prior_status": body.get("prior_status")})
    return filter_contract(reports, contract)


def score_agenda(reports: list[dict], pro=None, today: date | None = None) -> dict:
    """V5 summary; market data is only requested once a scored sample exists."""
    split = agenda.sample(reports)
    rows = split["picked"]
    if rows:
        rows = score(pro or market.client(), rows, today or datetime.now(SHANGHAI).date())
    counts = {name: {"reports": 0, "scored": 0, "pending_entry": 0, "no_market_data": 0} for name in agenda.GROUPS}
    for row in rows:
        group = counts[row["group"]]
        group["reports"] += 1
        key = "scored" if row["returns"] else "no_market_data" if row["entry"] else "pending_entry"
        group[key] += 1
    return {"rows": rows, "contracts": contract_counts(rows), "unrecorded": len(split["unrecorded"]), "reruns": len(split["reruns"]),
            "tally": agenda.tally(rows), "groups": counts, "returns": summarize(rows, "group")}


def agenda_lines(result: dict) -> list[str]:
    tally = result["tally"]
    share = lambda v: f"{v:.0%}" if v is not None else "—"
    lines = [f"V5 议程状态：入样 {len(result['rows'])} 份，历史未记录 {result['unrecorded']} 份，"
             f"同期重跑 {result['reruns']} 份（不入比率）"]
    lines.append(contract_line(result["rows"]))
    for section, label in (("risks", "旧风险"), ("checkpoints", "旧检查点")):
        lines.append(f"{label}：" + "，".join(f"{status} {n}" for status, n in tally["counts"][section].items()))
    lines.append(f"可判定条目中的已发生占比 {share(tally['occurred'])}；检查点已触发占比 {share(tally['triggered'])}")
    lines += [f"#{r['id']} {r['code']} 共 {r['items']} 项，无法判断 {share(r['unknown'])}" for r in tally["per_report"]]
    for name, group in result["groups"].items():
        lines.append(f"{name}：{group['reports']} 份，可记分 {group['scored']}，待入场 {group['pending_entry']}，"
                     f"缺行情 {group['no_market_data']}")
    for s in result["returns"]:
        lines.append(f"{s['source']}·{s['group']} {s['horizon']}：n={s['n']} 平均超额 {_pct(s['mean'])} "
                     f"中位 {_pct(s['median'])} 跑赢 {s['beat']:.0%} 左尾 {s['left_tail']:.0%}")
    return lines


def _adjusted_closes(pro, code: str, start: str, end: str) -> dict[str, float]:
    bars = screening._query(pro, "daily", ts_code=code, start_date=start, end_date=end, fields="trade_date,close")
    factors = screening._query(pro, "adj_factor", ts_code=code, start_date=start, end_date=end)
    factor = dict(zip(factors["trade_date"].astype(str), factors["adj_factor"]))
    out = {}
    for day, close in zip(bars["trade_date"].astype(str), bars["close"]):
        if not screening._positive(close) or not screening._positive(factor.get(day)):
            raise screening.ScreenError(f"{code} {day} 收盘价或复权因子无效")
        out[day] = float(close) * float(factor[day])
    return out


def score(pro, reports: list[dict], today: date) -> list[dict]:
    end = screening._compact(today)
    start = min(r["completed"] for r in reports).date() if reports else today
    trade_days = market.trade_days(pro, screening._compact(start), end)
    bench = {}
    if trade_days:
        frame = screening._query(pro, "index_daily", ts_code=BENCHMARK, start_date=trade_days[0], end_date=end)
        bench = dict(zip(frame["trade_date"].astype(str), frame["close"].astype(float)))
    last = max(bench, default=None)  # total-return index publishes after stocks; never score beyond it
    rows = []
    for report in reports:
        entry = entry_day(report["completed"], trade_days)
        row = report | {"entry": entry, "returns": {}, "mv": None}
        if entry and last and entry <= last:
            stock = _adjusted_closes(pro, report["code"], entry, last)
            basic = screening._query(pro, "daily_basic", ts_code=report["code"], trade_date=entry, fields="total_mv")
            row["mv"] = float(basic["total_mv"].iloc[0]) / 1e4 if not basic.empty else None  # 亿元
            for horizon, day in horizon_dates(trade_days, entry, last).items():
                if result := excess(stock, bench, entry, day):
                    row["returns"][horizon] = result | {"date": day}
        rows.append(row)
    sizes = sorted(r["mv"] for r in rows if r["mv"] is not None)
    cuts = (sizes[len(sizes) // 3], sizes[2 * len(sizes) // 3]) if len(sizes) >= 3 else (float("inf"), float("inf"))
    for row in rows:
        row["size"] = size_bucket(row["mv"], cuts)
    return rows


def score_stances(pro, rows: list[dict], today: date) -> list[dict]:
    """Attach V4 wait outcomes to V0-scored rows; prices from TuShare daily lows, evidence from financials."""
    from a_share_research import material, sources  # provider imports only when scoring
    last = max((d for r in rows for d in [v["date"] for v in r["returns"].values()]), default=None)
    tables = {}
    for row in rows:
        calc = row["stance_calc"]
        if row["stance"] == "等价格" and calc and calc.get("price") is not None and row["entry"]:
            if last is None:
                row["stance_outcome"] = {"status": "pending"}
                continue
            bars = screening._query(pro, "daily", ts_code=row["code"], start_date=row["entry"], end_date=last,
                                    fields="trade_date,low")
            lows = dict(zip(bars["trade_date"].astype(str), bars["low"].astype(float)))
            row["stance_outcome"] = price_wait(lows, calc["price"], row["entry"], calc["valid_until"], last)
        elif (row["stance"] == "等证据" and row.get("trigger")) or (row["stance"] == "现在分批" and calc and calc.get("add_trigger")):
            trigger = calc["add_trigger"] if row["stance"] == "现在分批" else row["trigger"]
            if not period_ended(trigger["period"], today):
                row["stance_outcome"] = checkpoint_outcome(row, {}, today)
                continue
            if row["code"] not in tables:
                tables[row["code"]] = {r["id"]: r for r in material.financial_table(sources.financials(row["code"]), {})}
            row["stance_outcome"] = checkpoint_outcome(row, tables[row["code"]], today)
    return rows


def _pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="V0：已有报告的事后超额（只读，不调用模型）")
    parser.add_argument("--database", required=True, help="生产或隔离库 app.db，只读打开")
    parser.add_argument("--json", help="另存逐份结果与汇总的 JSON 路径")
    parser.add_argument("--html", help="V1：另存可直接打开的只读复盘快照 HTML")
    parser.add_argument("--frozen", action="store_true", help="仅与 --forecasts 同用：只看冻结预测，不访问行情/财务接口")
    parser.add_argument("--contract", type=int, help="只看指定判断契约；0 表示未记录")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--forecasts", action="store_true", help="V1：为检查点预测记分，而不是计算事后超额")
    modes.add_argument("--hypotheses", action="store_true", help="V3：H1 避坑与 H3 读得快假设记分")
    modes.add_argument("--stances", action="store_true", help="V4：研究立场、加仓检查点与历史等证据兑现与等待代价")
    modes.add_argument("--agenda", action="store_true", help="V5：接续报告对旧风险与检查点的逐项状态及各状态组后续表现")
    args = parser.parse_args()
    if (args.html or args.frozen) and not args.forecasts:
        parser.error("--html / --frozen 仅适用于 --forecasts")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.hypotheses:
        from a_share_research import hypotheses
        hypotheses.run(args.database, args.json, args.contract)
        return
    if args.forecasts:
        items = load_forecasts(args.database, args.contract)
        scored = frozen_forecasts(items) if args.frozen else score_forecasts(items)
        review = forecast_review(scored)
        summary = summarize_forecasts(scored)
        lines = [f"V1 预测记分：首份 {summary['total']} 条（{summary['companies']} 家公司、{summary['periods']} 个目标期，"
                 f"另有更新 {summary['updates']} 条不重复记分），已兑现 {summary['scored']}，待披露 {summary['pending']}，"
                 f"缺失/不可比 {summary['unavailable']}，未查询兑现 {summary['unchecked']}，基期数据已更正 {summary['restated']}"]
        lines.append(review["scope"])
        lines.append(contract_line([s for s in scored if s["first"]]))
        for s in review["items"]:
            r = s["result"]
            lines.append(f"#{s['report']}[{s['index']}]{'' if s['first'] else '（更新）'} {s['code']} {s['name']} "
                         f"{forecasts.describe(s['forecast'], [])} → "
                         + (f"实际 {r['actual']:g} {'命中' if r['hit'] else '未中'}，朴素 {r['baseline'] if r['baseline'] is not None else '缺失'}"
                            f"{'，胜过朴素' if r['beat_baseline'] else '，未胜过朴素（含持平）' if r['beat_baseline'] is False else ''}"
                            if r["status"] == "scored" else r.get("reason", "待披露")))
            d = s["diagnostics"]
            baseline_err = f"{d['baseline_absolute_error']}{d['difference_unit']}" if d['baseline_absolute_error'] is not None else "缺失"
            lines.append(f"  区间宽度 {d['width']:g}{d['difference_unit']}，宽度/冻结朴素值绝对值 "
                         f"{_pct(d['width_to_baseline']) if d['width_to_baseline'] is not None else '—'}"
                         + (f"（{d['baseline_note']}）" if d.get("baseline_note") else "")
                         + (f"；中点绝对误差 {d['midpoint_absolute_error']:g}{d['difference_unit']}，"
                            f"朴素绝对误差 {baseline_err}" if r["status"] == "scored" else ""))
        cov_parts = [f"{c['confidence']}% {c['total']} 条" for c in review["confidence_coverage"]]
        missing_levels = [f"{c['confidence']}%" for c in review["confidence_coverage"] if c["total"] == 0]
        if summary["total"]:
            lines.append("主观把握入样分布（首份）：" + " · ".join(cov_parts)
                         + (f"（缺 {'/'.join(missing_levels)} 样本，校准度暂不完整）" if missing_levels else ""))
        for level, g in summary["by_confidence"].items():
            lines.append(f"把握 {level}%：n={g['n']} 命中 {g['hit']:.0%}")
        if summary["beat_baseline"] is not None:
            lines.append(f"区间中点胜过朴素预测：{summary['beat_baseline']:.0%}（n={summary['compared']}）")
            if summary["distorted_compared"]:
                lines.append(f"  剔除 {summary['distorted_compared']} 条外推失真基线后胜过朴素预测："
                             f"{_pct(summary['clean_beat_baseline'])}（n={summary['clean_compared']}）")
        for group in review["model_groups"]:
            lines.append(f"契约 {group['contract']} 作者 {group['author']} × 复核 {group['reviewer']}："
                         f"{group['reports']} 份报告，{group['companies']} 家公司，{group['periods']} 个目标期，"
                         f"首份 {group['total']} 条，已兑现 {group['scored']}，朴素可比 {group['compared']}（仅描述，不作排名）")
            for width in group["width_groups"]:
                distorted_note = (f"；剔除 {width['distorted_baselines']} 条外推失真基线后中位 "
                                  f"{_pct(width['clean_median_width_to_baseline'])}（n={width['clean_width_compared']}）"
                                  if width["distorted_baselines"] else "")
                lines.append(f"  {width['metric']}（{width['unit']}）把握 {width['confidence']}%："
                             f"首份 {width['total']}，已兑现 {width['scored']}，朴素可比 {width['compared']}；"
                             f"相对宽度中位 {_pct(width['median_width_to_baseline']) if width['median_width_to_baseline'] is not None else '—'}"
                             f"（非零基线 n={width['width_compared']}，含未兑现样本{distorted_note}）")
        if review["shadowed_model_groups"]:
            lines.append("以下契约/模型配置因早期报告已占首份名额，在当前未筛选口径下仅作更新、未计入上方首份分组：")
            for sg in review["shadowed_model_groups"]:
                hint = f"（单独观测请加 --contract {sg['contract']}）" if sg["contract"] is not None else ""
                lines.append(f"  契约 {sg['contract']} 作者 {sg['author']} × 复核 {sg['reviewer']}："
                             f"{sg['reports']} 份报告，{sg['companies']} 家公司，更新 {sg['updates']} 条{hint}")
        if review["contract_independent_first"]:
            lines.append("按契约独立去重（不受早期契约占槽影响）：")
            for cs in review["contract_independent_first"]:
                lines.append(f"  契约 {cs['contract']}：首份 {cs['total']} 条（{cs['companies']} 家公司、"
                             f"{cs['periods']} 个目标期、已兑现 {cs['scored']}）；完整详情加 --contract {cs['contract']}")
        log.info("\n%s", "\n".join(lines))
        if args.html:
            write_forecast_html(review, args.html)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump({"forecasts": review["items"], "summary": summary, "model_groups": review["model_groups"],
                           "shadowed_model_groups": review["shadowed_model_groups"],
                           "contract_independent_first": review["contract_independent_first"],
                           "confidence_coverage": review["confidence_coverage"],
                           "generated_at": review["generated_at"], "scope": review["scope"],
                           "contracts": contract_counts([s for s in scored if s["first"]])}, fh, ensure_ascii=False, indent=1, default=str)
        return
    if args.agenda:
        result = score_agenda(load_agenda(args.database, args.contract))
        log.info("\n%s", "\n".join(agenda_lines(result)))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1, default=str)
        return
    pro, today = market.client(), datetime.now(SHANGHAI).date()
    rows = score(pro, load_reports(args.database, args.contract), today)
    if args.stances:
        rows = score_stances(pro, [r for r in rows if r["stance"] != "未记录"], today)
        summary = summarize_stances(rows)
        lines = [f"V4 研究立场记分（{len(rows)} 份带立场报告；超额为入场至今相对 {BENCHMARK}）"]
        lines.append(contract_line(rows))
        for row in rows:
            outcome = row.get("stance_outcome") or {}
            latest = row["returns"].get("latest")
            target = (f" 门槛价 {row['stance_calc']['price']:.2f} 至 {row['stance_calc']['valid_until']}" if row["stance"] == "等价格"
                      else f" {row['trigger']['period']} {row['trigger']['metric']} {row['trigger']['op']} {row['trigger']['value']:g}"
                      if row["stance"] == "等证据" and row["trigger"] else "")
            if row.get("add_trigger"):
                t = row["add_trigger"]
                target = f" 加仓检查点（参考） {t['period']} {t['metric']} {t['op']} {t['value']:g}"
            status = {"met": "达成", "not_met": "未达成", "pending": "待披露"}.get(outcome.get("status"), outcome.get("status", "—")) if row["stance"] != "等价格" else outcome.get("status", "—")
            lines.append(f"#{row['id']} {row['code']} {row['name']} {row['stance']}{'（边界）' if row.get('boundary') else ''}{target} → {status}"
                         f"{' ' + outcome['date'] if outcome.get('date') else ''} 至今超额 {_pct(latest['excess']) if latest else '—'}")
        for key, g in summary.items():
            lines.append(f"{key} n={g['n']} 已入场 {g['scored']} 平均超额 {_pct(g['mean_excess']) if g['mean_excess'] is not None else '—'}")
        log.info("\n%s", "\n".join(lines))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump({"rows": rows, "summary": summary, "contracts": contract_counts(rows)}, fh, ensure_ascii=False, indent=1, default=str)
        return
    summary = {"verdict": summarize(rows, "verdict"), "size": summarize(rows, "size"), "stance": summarize(rows, "stance")}
    lines = [f"V0 事后超额（基准 {BENCHMARK}，起点为报告完成后首个完整交易日收盘）", contract_line(rows), ""]
    for row in rows:
        latest = row["returns"].get("latest")
        lines.append(f"#{row['id']} {row['source']} {row['code']} {row['name']} {row['verdict']} 入场 {row['entry'] or '待入场'} "
                     f"市值 {row['mv'] and round(row['mv'])}亿 至今超额 {_pct(latest['excess']) if latest else '—'}")
    for key in ("verdict", "size", "stance"):
        groups = summary[key]
        lines += ["", f"按{ {'verdict': '结论', 'size': '市值三分位', 'stance': '研究立场'}[key] }汇总"]
        for g in groups:
            lines.append(f"{g['source']} {g[key]} {g['horizon']} n={g['n']} 均值 {_pct(g['mean'])} 中位 {_pct(g['median'])} "
                         f"跑赢 {g['beat']:.0%} 左尾≤{LEFT_TAIL:.0%} {g['left_tail']:.0%}")
    log.info("\n%s", "\n".join(lines))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "summary": summary, "contracts": contract_counts(rows)}, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
