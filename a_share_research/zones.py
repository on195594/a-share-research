"""A3: read-only, forward zone diagnostics, not a trading simulation.

Serves retrospective improvement: measure conditional excess returns and left tails
against the PE baseline before claiming that valuation zones improve decisions.
"""

import argparse
import json
import logging
import math
import sqlite3
import statistics
from contextlib import closing
from datetime import date, datetime, timedelta
from heapq import heappop, heappush
from pathlib import Path

from a_share_research import report as report_body, screening, validation, valuation
from a_share_research.db import SHANGHAI

log = logging.getLogger(__name__)
PE_RULE = "pe-percentile-5y"
PE_WINDOW = 1250
PE_MINIMUM = 250
HORIZONS = tuple(f"{h}d" for h in validation.HORIZONS) + ("latest",)
METHOD = {
    "entry": "报告按完成时间排序，从 validation.entry_day 生效；同日生效取最后一份，不按月去重。",
    "observation": "每规则连续区间首日；换报告但区间不变不新增。无收盘或无法判定的日子跳过，不开启也不中断连续段。",
    "return": "观测后的下一交易日收盘起算；起点缺价不顺延。收益复用 V0 复权价和中证800全收益。",
    "pe": "此前 1250 个交易日（不含当日）的正 PE，至少 250 个；分位=严格小于当日 PE 的样本数/有效样本数。<30%低位，>70%高位，其余中位。",
    "v2": "valuation-v2（三情景概率加权期望回报）与 v1 用同一套观测与收益口径并行记分；只有带 v2 的报告才有 v2 观测，分歧与重叠统计仍只比较 v1 与 PE。",
    "disagreement": "两规则段首日的并集，按公司×观测日去重；当日支持区对高位，或透支区对低位才计分歧，中性和缺失不计。",
    "overlap": "同时为两规则段首日的比例；另按规则×期限统计同公司收益窗口与此前窗口相交（不含仅共用端点）的观测数及配对数。",
    "sample": "n 和公司数只计该期限可记分的观测；同时列出总观测及待记分数。财报期取报告财务表最新期。",
    "interpretation": "仅描述样本，不作有效性结论；未到期或不足约60条估值规则120日观测时证据不足。观测与重叠收益不是独立判断。",
}


# ---------- pure functions ----------

def zone_of(close: float | None, zones: dict) -> str | None:
    """Classify an unadjusted close; thresholds have been validated at loading."""
    return None if close is None else valuation.price_zone(close, zones["support_below"], zones["stretched_above"])


def first_days(labels: dict[str, str | None]) -> list[str]:
    """First day of each run of the same known label, independent of report identity.

    Unknown days (suspension, missing close or PE) neither start nor break a run: a stock that resumes
    in the zone it left is the same observation, not a new one.
    """
    out, previous = [], None
    for day, label in sorted(labels.items()):
        if label is not None and label != previous:
            out.append(day)
            previous = label
    return out


def pe_percentile_labels(pe: dict[str, float | None], trade_days: list[str], *,
                         window: int = PE_WINDOW, minimum: int = PE_MINIMUM) -> dict[str, str | None]:
    """Trailing exchange sessions, excluding today; ties use strict-less rank.

    Missing/nonpositive observations consume a session but not a valid sample.
    Parameters allow checking the same ranking arithmetic on short real captures.
    """
    if not 1 <= minimum <= window:
        raise ValueError("PE 样本下限必须在 1 与窗口长度之间")
    values = [pe.get(day) for day in trade_days]
    out = {}
    for i, (day, value) in enumerate(zip(trade_days, values)):
        history = [v for v in values[max(0, i - window):i] if v is not None and math.isfinite(v) and v > 0]
        label = None
        if value is not None and math.isfinite(value) and value > 0 and len(history) >= minimum:
            percentile = sum(v < value for v in history) / len(history)
            label = "分位低位" if percentile < 0.3 else "分位高位" if percentile > 0.7 else "分位中位"
        out[day] = label
    return out


def observations(reports: list[dict], trade_days: list[str], closes: dict[str, float],
                 pe_labels: dict[str, str | None]) -> list[dict]:
    """One company's report windows and independent segment starts for both rules."""
    schedule = {}
    for report in sorted(reports, key=lambda r: (r["completed"], r["id"])):
        if entry := validation.entry_day(report["completed"], trade_days):
            schedule[entry] = report
    active, by_day, valuations, percentiles, shadows = None, {}, {}, {}, {}
    next_day = dict(zip(trade_days, trade_days[1:]))
    for day in trade_days:
        active = schedule.get(day, active)
        if active is None:
            continue
        by_day[day] = active
        valuations[day] = zone_of(closes.get(day), active["valuation_calc"]["zones"])
        v2 = active["valuation_calc"].get("v2")
        shadows[day] = zone_of(closes.get(day), v2) if isinstance(v2, dict) else None
        # Both rules must be observable in the same report/price window.
        percentiles[day] = pe_labels.get(day) if day in closes else None
    rows = []
    for labels, kind in ((valuations, "v1"), (percentiles, "pe"), (shadows, "v2")):
        for day in first_days(labels):
            report = by_day[day]
            row = {"report": report["id"], "code": report["code"], "name": report["name"],
                   "source": report["source"], "period": report.get("period"), "contract": report.get("contract"),
                   "rule": {"pe": PE_RULE, "v2": valuation.RULE_V2}.get(kind, report["valuation_calc"]["rule"]),
                   "zone": labels[day], "observation": day, "entry": next_day.get(day),
                   "valuation_zone": valuations[day], "pe_zone": percentiles[day]}
            if kind == "v2":
                row["v2_zone"] = shadows[day]
            rows.append(row)
    return sorted(rows, key=lambda r: (r["observation"], r["rule"]))


def score_observations(rows: list[dict], trade_days: list[str], adjusted: dict[str, float],
                       benchmark: dict[str, float]) -> list[dict]:
    """Score one company's observations without moving a missing entry close."""
    scored = []
    last = max(benchmark, default="")
    for row in rows:
        returns = {}
        if row["entry"]:
            for horizon, end in validation.horizon_dates(trade_days, row["entry"], last).items():
                if result := validation.excess(adjusted, benchmark, row["entry"], end):
                    returns[horizon] = result | {"date": end}
        scored.append(row | {"returns": returns})
    return scored


def summarize(rows: list[dict]) -> list[dict]:
    """Per rule/zone/horizon, show empty/pending groups rather than implying zero returns."""
    groups = {}
    for row in rows:
        groups.setdefault((row["rule"], row["zone"]), []).append(row)
    out = []
    for (rule, zone), group in sorted(groups.items()):
        for horizon in HORIZONS:
            done = [r for r in group if horizon in r["returns"]]
            values = [r["returns"][horizon]["excess"] for r in done]
            out.append({"rule": rule, "zone": zone, "horizon": horizon, "n": len(done),
                        "companies": len({r["code"] for r in done}), "observations": len(group),
                        "pending": len(group) - len(done),
                        "periods": sorted({r["period"] for r in done if r.get("period")}),
                        "mean": statistics.fmean(values) if values else None,
                        "median": statistics.median(values) if values else None,
                        "beat": sum(v > 0 for v in values) / len(values) if values else None,
                        "left_tail": sum(v <= validation.LEFT_TAIL for v in values) / len(values) if values else None})
    return out


def diagnostics(rows: list[dict]) -> dict:
    """Count opposing directions once per company/day, plus correlated observations.

    v1-versus-PE metrics ignore v2 shadow rows; v2 only adds its own maturity and v1-difference counts.
    """
    shadow = [r for r in rows if r["rule"] == valuation.RULE_V2]
    days = {}
    for row in (r for r in rows if r["rule"] != valuation.RULE_V2):
        days.setdefault((row["code"], row["observation"]), []).append(row)
    paired = [group[0] for group in days.values() if group[0]["valuation_zone"] and group[0]["pe_zone"]]
    opposites = {("测算支持区", "分位高位"), ("测算透支区", "分位低位")}
    both = sum(len(group) == 2 for group in days.values())
    overlaps = []
    for rule in sorted({r["rule"] for r in rows}):
        for horizon in HORIZONS:
            done = sorted((r for r in rows if r["rule"] == rule and horizon in r["returns"]),
                          key=lambda r: (r["entry"], r["code"]))
            ends, count, pairs = {}, 0, 0
            for row in done:
                previous = ends.setdefault(row["code"], [])
                while previous and previous[0] <= row["entry"]:
                    heappop(previous)
                count += bool(previous)
                pairs += len(previous)
                heappush(previous, row["returns"][horizon]["date"])
            overlaps.append({"rule": rule, "horizon": horizon, "n": len(done),
                             "overlapping": count, "pairs": pairs})
    matured = [r for r in rows if r["rule"] not in (PE_RULE, valuation.RULE_V2) and "120d" in r["returns"]]
    return {"observation_days": len(days), "comparable_days": len(paired),
            "disagreements": sum((r["valuation_zone"], r["pe_zone"]) in opposites for r in paired),
            "both_first_days": both, "both_first_share": both / len(days) if days else None,
            "companies": len({r["code"] for r in rows}),
            "periods": sorted({r["period"] for r in rows if r.get("period")}),
            "unknown_period_observations": sum(r.get("period") is None for r in rows),
            "matured_120d": len(matured), "matured_120d_companies": len({r["code"] for r in matured}),
            "evidence": "证据不足" if len(matured) < 60 else "达到首次诊断观察门槛，仍不自动作有效性结论",
            "return_overlap": overlaps,
            "v2_observations": len(shadow), "v2_differs_from_v1": sum(r["zone"] != r["valuation_zone"] for r in shadow),
            "v2_matured_120d": sum("120d" in r["returns"] for r in shadow)}


# ---------- data boundaries ----------

def load_reports(database: str, contract: int | None = None) -> list[dict]:
    """V0 eligibility, but retain every usable valuation report (no monthly dedup)."""
    with closing(sqlite3.connect(Path(database).expanduser().resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT r.id, r.code, r.name, r.created_at, u.request, r.body FROM reports r "
            "JOIN runs u ON u.id = r.run_id WHERE r.passed = 1 AND u.kind = 'research' AND u.replay_of IS NULL"
        ).fetchall()
    reports = []
    for report_id, code, name, created, request, body in rows:
        src = validation.source(json.loads(request) if request else {})
        if src is None:
            continue
        body = report_body.load(body)
        calc = body.get("valuation_calc")
        if not isinstance(calc, dict) or calc.get("not_priced") or "error" in calc:
            continue
        zones = calc.get("zones")
        if not isinstance(zones, dict) or zones.get("inverted") is not False:
            continue
        v2 = calc.get("v2")
        bounds = [zones.get(key) for key in ("support_below", "stretched_above")]
        if v2 is not None:
            bounds += [v2.get(key) if isinstance(v2, dict) else None for key in ("support_below", "stretched_above")]
        if (not isinstance(calc.get("rule"), str) or not calc["rule"]
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in bounds) or bounds[0] > bounds[1] or (len(bounds) == 4 and bounds[2] > bounds[3])):
            raise ValueError(f"报告 #{report_id} valuation_calc 的规则或区间阈值无效")
        reports.append({"id": report_id, "code": code, "name": name, "source": src,
                        "completed": datetime.fromisoformat(created), "valuation_calc": calc,
                        "contract": validation.judgment_contract(body),
                        "period": validation.forecasts.base_period(body.get("table") or [])})
    return sorted(validation.filter_contract(reports, contract), key=lambda r: (r["completed"], r["id"]))


def load_market(pro, reports: list[dict], today: date) -> tuple[list[str], dict, dict]:
    """All provider reads live here; year chunks stay below TuShare row limits."""
    if not reports:
        return [], {}, {}
    start = min(r["completed"] for r in reports).date()
    end = screening._compact(today)
    # Six calendar years comfortably cover the requested 1250 prior SSE sessions.
    calendar = screening._trade_days(pro, screening._compact(start - timedelta(days=6 * 366)), end)

    def series(api, code, since, field):
        out = {}
        for year in range(int(since[:4]), today.year + 1):
            lo, hi = max(since, f"{year}0101"), min(end, f"{year}1231")
            frame = screening._query(pro, api, ts_code=code, start_date=lo, end_date=hi,
                                     fields=f"trade_date,{field}")
            for day, value in zip(frame["trade_date"].astype(str), frame[field]):
                if not screening._positive(value):
                    if field == "pe_ttm":
                        out[day] = None
                        continue
                    raise screening.ScreenError(f"{api} {code} {day} {field} 无效")
                out[day] = float(value)
        return out

    benchmark = series("index_daily", validation.BENCHMARK, screening._compact(start), "close")
    market = {}
    for code in sorted({r["code"] for r in reports}):
        since = screening._compact(min(r["completed"] for r in reports if r["code"] == code).date())
        prior = [d for d in calendar if d < since]
        pe_start = prior[-PE_WINDOW] if len(prior) >= PE_WINDOW else calendar[0] if calendar else since
        adjusted = {}
        for year in range(int(since[:4]), today.year + 1):
            adjusted.update(validation._adjusted_closes(pro, code, max(since, f"{year}0101"), min(end, f"{year}1231")))
        market[code] = {"close": series("daily", code, since, "close"), "adjusted": adjusted,
                        "pe": series("daily_basic", code, pe_start, "pe_ttm")}
    return calendar, market, benchmark


def diagnose(reports: list[dict], trade_days: list[str], market: dict, benchmark: dict, *, contract: int | None = None) -> dict:
    """Offline-capable composition: pure analysis of already loaded inputs."""
    rows = []
    for code in sorted({r["code"] for r in reports}):
        data = market[code]
        labels = pe_percentile_labels(data["pe"], trade_days)
        company_rows = observations([r for r in reports if r["code"] == code], trade_days, data["close"], labels)
        rows.extend(score_observations(company_rows, trade_days, data["adjusted"], benchmark))
    rows = validation.filter_contract(rows, contract)
    reports = validation.filter_contract(reports, contract)
    return {"benchmark": validation.BENCHMARK, "benchmark_last": max(benchmark, default=None),
            "reports": len(reports), "contracts": validation.contract_counts(reports), "method": METHOD, "rows": rows,
            "summary": summarize(rows), "diagnostics": diagnostics(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description="A3：区间条件收益诊断（只读，不调用模型）")
    parser.add_argument("--database", required=True, help="app.db，只读打开")
    parser.add_argument("--json", help="另存逐条观测、汇总与口径的 JSON 路径")
    parser.add_argument("--contract", type=int, help="只看指定判断契约；0 表示未记录")
    args = parser.parse_args()
    if args.json and Path(args.json).resolve() == Path(args.database).expanduser().resolve():
        parser.error("JSON 输出不能覆盖输入数据库")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    reports = load_reports(args.database)
    calendar, market, benchmark = load_market(screening._pro() if reports else None, reports, datetime.now(SHANGHAI).date())
    result = diagnose(reports, calendar, market, benchmark, contract=args.contract)
    log.info("A3 区间条件收益诊断：%s 份有效报告，基准 %s，截至 %s", result["reports"], validation.BENCHMARK, result["benchmark_last"])
    log.info("%s", validation.contract_line(validation.filter_contract(reports, args.contract)))
    for description in METHOD.values():
        log.info("%s", description)
    for row in result["rows"]:
        log.info("#%s %s %s %s %s %s 观测 %s 起点 %s 收益 %s", row["report"], row["source"], row["code"],
                 row["name"], row["rule"], row["zone"], row["observation"], row["entry"] or "待下一交易日",
                 json.dumps(row["returns"], ensure_ascii=False))
    for group in result["summary"]:
        log.info("汇总 %s", json.dumps(group, ensure_ascii=False))
    log.info("诊断 %s", json.dumps(result["diagnostics"], ensure_ascii=False))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
