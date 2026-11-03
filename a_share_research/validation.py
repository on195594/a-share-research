"""Judgment validation: V0 excess return after each report, V1 checkpoint forecast scoring (read-only, no model calls).

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

from a_share_research import forecasts, screening
from a_share_research.db import SHANGHAI

log = logging.getLogger(__name__)

BENCHMARK = "H00906.CSI"  # 中证800全收益
HORIZONS = (20, 60, 120)  # trading days after entry
LEFT_TAIL = -0.30
SESSION_OPEN = time(9, 30)


# ---------- pure functions ----------

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
    return {"total": len(first), "updates": len(scored) - len(first), "scored": len(done), "pending": sum(s["result"]["status"] == "pending" for s in first),
            "unavailable": sum(s["result"]["status"] == "unavailable" for s in first),
            "companies": len({s["code"] for s in first}), "periods": len({s["forecast"]["period"] for s in first}),
            "restated": sum(bool(s["result"].get("restated")) for s in done),
            "by_confidence": by_confidence,
            "beat_baseline": sum(s["result"]["beat_baseline"] for s in compared) / len(compared) if compared else None,
            "compared": len(compared)}


def mark_first(items: list[dict]) -> list[dict]:
    """A forecast's identity is company × metric × target period; the earliest report holds the first one."""
    seen = set()
    for item in sorted(items, key=lambda i: (i["created"], i["report"], i["index"])):
        key = (item["code"], item["forecast"]["metric"], item["forecast"]["period"])
        item["first"] = key not in seen
        seen.add(key)
    return items


def size_bucket(mv: float | None, cuts: tuple[float, float]) -> str:
    if mv is None:
        return "未知"
    return "小" if mv <= cuts[0] else "中" if mv <= cuts[1] else "大"


# ---------- data ----------

def load_forecasts(database: str) -> list[dict]:
    """Every checkpoint forecast in passed, non-replay research reports (corrections excluded, as in V0)."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT r.id, r.code, r.name, r.created_at, r.body FROM reports r JOIN runs u ON u.id = r.run_id "
            "WHERE r.passed = 1 AND u.kind = 'research' AND u.replay_of IS NULL "
            "AND r.body LIKE '%\"forecast\"%'").fetchall()
    out = []
    for report_id, code, name, created, body in rows:
        body = json.loads(body)
        table = body.get("table") or []
        base = forecasts.base_period(table)
        written = {r["id"]: r for r in table}
        for i, item in enumerate(body.get("checkpoints") or []):
            forecast = item.get("forecast") if isinstance(item, dict) else None
            if base and forecast and not forecasts.problems(forecast, table):
                out.append({"report": report_id, "code": code, "name": name, "created": created, "index": i,
                            "base": base, "forecast": forecast, "written": written[forecast["metric"]]})
    return mark_first(out)


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


def load_reports(database: str) -> list[dict]:
    """Passed, non-replay, non-feedback research reports from a read-only connection."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT r.id, r.code, r.name, r.verdict, r.created_at, u.request FROM reports r "
            "JOIN runs u ON u.id = r.run_id WHERE r.passed = 1 AND u.kind = 'research' AND u.replay_of IS NULL"
        ).fetchall()
    reports = []
    for report_id, code, name, verdict, created, request in rows:
        src = source(json.loads(request) if request else {})
        if src is None:
            continue
        reports.append({"id": report_id, "code": code, "name": name, "verdict": verdict, "source": src,
                        "completed": datetime.fromisoformat(created)})
    return first_per_month(reports)


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
    trade_days = screening._trade_days(pro, screening._compact(start), end)
    frame = screening._query(pro, "index_daily", ts_code=BENCHMARK, start_date=trade_days[0], end_date=end)
    bench = dict(zip(frame["trade_date"].astype(str), frame["close"].astype(float)))
    last = max(bench)  # total-return index publishes after stocks; never score beyond it
    rows = []
    for report in reports:
        entry = entry_day(report["completed"], trade_days)
        row = report | {"entry": entry, "returns": {}, "mv": None}
        if entry and entry <= last:
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


def _pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="V0：已有报告的事后超额（只读，不调用模型）")
    parser.add_argument("--database", required=True, help="生产或隔离库 app.db，只读打开")
    parser.add_argument("--json", help="另存逐份结果与汇总的 JSON 路径")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--forecasts", action="store_true", help="V1：为检查点预测记分，而不是计算事后超额")
    modes.add_argument("--hypotheses", action="store_true", help="V3：H1 避坑与 H3 读得快假设记分")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.hypotheses:
        from a_share_research import hypotheses
        hypotheses.run(args.database, args.json)
        return
    if args.forecasts:
        scored = score_forecasts(load_forecasts(args.database))
        summary = summarize_forecasts(scored)
        lines = [f"V1 预测记分：首份 {summary['total']} 条（{summary['companies']} 家公司、{summary['periods']} 个目标期，"
                 f"另有更新 {summary['updates']} 条不重复记分），已兑现 {summary['scored']}，待披露 {summary['pending']}，"
                 f"缺失/不可比 {summary['unavailable']}，基期数据已更正 {summary['restated']}"]
        for s in scored:
            r = s["result"]
            lines.append(f"#{s['report']}[{s['index']}]{'' if s['first'] else '（更新）'} {s['code']} {s['name']} "
                         f"{forecasts.describe(s['forecast'], [])} → "
                         + (f"实际 {r['actual']:g} {'命中' if r['hit'] else '未中'}，朴素 {r['baseline']}"
                            f"{'，胜过朴素' if r['beat_baseline'] else '，未胜过朴素（含持平）' if r['beat_baseline'] is False else ''}"
                            if r["status"] == "scored" else r.get("reason", "待披露")))
        for level, g in summary["by_confidence"].items():
            lines.append(f"把握 {level}%：n={g['n']} 命中 {g['hit']:.0%}")
        if summary["beat_baseline"] is not None:
            lines.append(f"区间中点胜过朴素预测：{summary['beat_baseline']:.0%}（n={summary['compared']}）")
        log.info("\n%s", "\n".join(lines))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump({"forecasts": scored, "summary": summary}, fh, ensure_ascii=False, indent=1, default=str)
        return
    rows = score(screening._pro(), load_reports(args.database), datetime.now(SHANGHAI).date())
    summary = {"verdict": summarize(rows, "verdict"), "size": summarize(rows, "size")}
    lines = [f"V0 事后超额（基准 {BENCHMARK}，起点为报告完成后首个完整交易日收盘）", ""]
    for row in rows:
        latest = row["returns"].get("latest")
        lines.append(f"#{row['id']} {row['source']} {row['code']} {row['name']} {row['verdict']} 入场 {row['entry'] or '待入场'} "
                     f"市值 {row['mv'] and round(row['mv'])}亿 至今超额 {_pct(latest['excess']) if latest else '—'}")
    for key in ("verdict", "size"):
        groups = summary[key]
        lines += ["", f"按{ {'verdict': '结论', 'size': '市值三分位'}[key] }汇总"]
        for g in groups:
            lines.append(f"{g['source']} {g[key]} {g['horizon']} n={g['n']} 均值 {_pct(g['mean'])} 中位 {_pct(g['median'])} "
                         f"跑赢 {g['beat']:.0%} 左尾≤{LEFT_TAIL:.0%} {g['left_tail']:.0%}")
    log.info("\n%s", "\n".join(lines))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "summary": summary}, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
