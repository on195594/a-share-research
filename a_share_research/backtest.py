"""Exploratory backtest of the deterministic screen (Q20/Q5) on month-end dates, point in time.

Reuses the live screen's filters, factors, scoring and picks; only the data is rebuilt per date:
the pool includes later-delisted companies, industry and names (ST) come from dated records, and
statements count only once announced (see docs/screening-experiment.md §5 for the residual bias).
Results are evidence of rule weaknesses, not of rule validity.

    uv run --env-file .env python -m a_share_research.backtest --out DIR [--start 201601 --end 202510]
"""

import argparse
import hashlib
import json
import logging
import os
import pickle
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from a_share_research import screening
from a_share_research.screening import RULE, ScreenError

log = logging.getLogger(__name__)

CACHE: Path = Path(".")
FIN_SINCE = "20110101"  # fina_indicator filters on end_date, the statements on ann_date
PAGE = 100
HORIZONS = (6, 12)


def cached(pro, api: str, **params) -> pd.DataFrame:
    """One TuShare call, kept on disk: history does not change between runs of the backtest.

    Pickle is safe here only because this process alone writes the cache, under the private --out directory.
    """
    key = hashlib.sha256(json.dumps([api, params], sort_keys=True).encode()).hexdigest()
    path = CACHE / f"{api}-{key[:24]}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    frame = screening._query(pro, api, **params)
    tmp = path.with_suffix(f".{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_bytes(pickle.dumps(frame))
    tmp.replace(path)
    return frame


def history(pro, api: str, code: str, end: str, fields: str | None = None) -> pd.DataFrame:
    """A company's rows announced FIN_SINCE..end; single-stock financial calls return 100 rows a page."""
    extra = {"fields": fields} if fields else {}
    pages, offset = [], 0
    while True:
        page = cached(pro, api, ts_code=code, start_date=FIN_SINCE, end_date=end, offset=offset, limit=PAGE, **extra)
        pages.append(page)
        offset += len(page)
        if len(page) < PAGE:
            break
    return pd.concat(pages, ignore_index=True)


def _on(frame: pd.DataFrame, day: str, start: str, end: str) -> pd.DataFrame:
    """Rows in force on `day`: start ≤ day and (no end or end ≥ day); the latest start per code."""
    starts = frame[start].astype(str)
    ends = frame[end].astype(str)
    live = frame[(starts <= day) & (frame[end].isna() | (ends >= day))]
    return live.sort_values(start).drop_duplicates("ts_code", keep="last").set_index("ts_code")


class Market:
    """Whole-history reference tables fetched once."""

    def __init__(self, pro, data_end: str):
        self.pro, self.data_end = pro, data_end
        self.basic = pd.concat([cached(pro, "stock_basic", list_status=s,
                                       fields="ts_code,name,industry,market,list_date,delist_date") for s in ("L", "D")])
        self.members = pd.concat([cached(pro, "index_member_all", is_new=s) for s in ("Y", "N")])
        names, offset = [], 0
        while True:
            page = cached(pro, "namechange", fields="ts_code,name,start_date,end_date", offset=offset, limit=10000)
            names.append(page)
            offset += len(page)
            if len(page) < 10000:
                break
        self.names = pd.concat(names)
        self.days = screening._trade_days(pro, "20150101", data_end)

    def next_day(self, day: str) -> str | None:
        return next((d for d in self.days if d > day), None)

    def first_on_or_after(self, day: str) -> str | None:
        return next((d for d in self.days if d >= day), None)

    def month_ends(self, start: str, end: str) -> list[str]:
        by_month: dict[str, str] = {}
        for d in self.days:
            if start <= d[:6] <= end:
                by_month[d[:6]] = d
        return list(by_month.values())

    def daily(self, day: str) -> pd.DataFrame:
        return cached(self.pro, "daily", trade_date=day, fields="ts_code,open,close,amount").set_index("ts_code")

    def adj(self, day: str) -> pd.Series:
        return cached(self.pro, "adj_factor", trade_date=day).set_index("ts_code")["adj_factor"]

    def suspended(self, day: str) -> set[str]:
        return set(cached(self.pro, "suspend_d", trade_date=day, suspend_type="S")["ts_code"])


def universe(m: Market, as_of: str) -> tuple[pd.DataFrame, dict, list[str]]:
    """The live screen's coarse filters on the market as it stood on `as_of`."""
    basic = m.basic[(m.basic["list_date"].astype(str) <= as_of)
                    & (m.basic["delist_date"].isna() | (m.basic["delist_date"].astype(str) > as_of))].copy()
    names = _on(m.names, as_of, "start_date", "end_date")["name"]
    basic["name"] = basic["ts_code"].map(names).fillna(basic["name"])
    sw = _on(m.members, as_of, "in_date", "out_date")["l1_name"]
    valuation = cached(m.pro, "daily_basic", trade_date=as_of, fields="ts_code,close,pe_ttm,total_mv")
    days = [d for d in m.days if d <= as_of][-RULE["amount_days"]:]
    amount = pd.concat([m.daily(d)["amount"] for d in days])
    avg_amount = amount.groupby(level=0).sum() / len(days)
    frame = basic.merge(valuation, on="ts_code", how="left")
    frame["industry_sw"] = frame["ts_code"].map(sw)
    frame["avg_amount"] = frame["ts_code"].map(avg_amount).fillna(0)
    return screening.market_filters(frame, m.suspended(as_of), as_of)


def company(m: Market, row, as_of: str) -> dict:
    code = row.ts_code
    base = {"code": code, "name": row.name, "industry": row.industry_sw,
            "pe": float(row.pe_ttm) if pd.notna(row.pe_ttm) else None,
            "total_mv_yi": round(float(row.total_mv) / 1e4, 2)}
    try:
        ind = history(m.pro, "fina_indicator", code, m.data_end, screening.INDICATOR_FIELDS)
        if ind.empty:
            return base | {"status": "gap", "reason": "没有财务指标"}
        pre = screening.indicator_check(ind, as_of)
        if pre["status"] != "ok":
            return base | pre
        inc = history(m.pro, "income", code, m.data_end, screening.INCOME_FIELDS)
        cf = history(m.pro, "cashflow", code, m.data_end, screening.CASHFLOW_FIELDS)
        return screening.evaluate(base, ind, inc, cf, lambda: history(m.pro, "fina_audit", code, m.data_end), as_of)
    except ScreenError as exc:
        return base | {"status": "gap", "reason": f"取数失败：{exc}"}


def entry(m: Market, day: str, codes: list[str]) -> dict:
    """The live entry rule: next-session open; suspended, missing or limit-up at the open stays cash."""
    daily, adj = m.daily(day), m.adj(day)
    limit = cached(m.pro, "stk_limit", trade_date=day).set_index("ts_code")["up_limit"]
    out = {}
    for code in codes:
        if code not in daily.index or not screening._positive(daily.at[code, "open"]) or code not in adj.index:
            out[code] = {"filled": False, "reason": "停牌或无行情"}
        elif code in limit.index and daily.at[code, "open"] >= limit[code]:
            out[code] = {"filled": False, "reason": "开盘涨停"}
        else:
            out[code] = {"filled": True, "open": float(daily.at[code, "open"]), "adj": float(adj[code])}
    return out


def mark(m: Market, start: str, day: str, codes: list[str]) -> dict:
    """Close × factor on `day`; a code without a bar keeps its last valid close (suspended or delisted)."""
    daily, adj = m.daily(day), m.adj(day)
    out = {}
    for code in codes:
        if code in daily.index and code in adj.index and screening._positive(daily.at[code, "close"]):
            out[code] = {"close": float(daily.at[code, "close"]), "adj": float(adj[code]), "date": day}
            continue
        bars = cached(m.pro, "daily", ts_code=code, start_date=start, end_date=day, fields="ts_code,trade_date,close")
        bars = bars[bars["close"].map(screening._positive)].sort_values("trade_date")
        if bars.empty:
            continue
        last = bars.iloc[-1]
        factor = cached(m.pro, "adj_factor", ts_code=code, trade_date=str(last["trade_date"]))["adj_factor"]
        if not factor.empty and screening._positive(factor.iloc[0]):
            out[code] = {"close": float(last["close"]), "adj": float(factor.iloc[0]), "date": str(last["trade_date"]),
                         "stale": True}
    return out


def batch(m: Market, as_of: str, workers: int) -> dict:
    frame, funnel, no_industry = universe(m, as_of)
    with ThreadPoolExecutor(workers) as pool:
        companies = list(pool.map(lambda r: company(m, r, as_of), frame.itertuples(index=False)))
    ranked = screening.score([c for c in companies if c["status"] == "ok"])
    q20 = screening.pick(ranked, RULE["q20"], RULE["q20_per_industry"])
    q5 = screening.pick(q20, RULE["picks"], RULE["picks_per_industry"])
    counts = {s: sum(c["status"] == s for c in companies) for s in ("ok", "excluded", "gap")}
    failed = [c["code"] for c in companies if str(c.get("reason", "")).startswith("取数失败")]
    if failed:  # a provider error must not pass for a data gap; rerun to fetch them again
        log.warning("%s: %s companies failed to fetch: %s", as_of, len(failed), failed[:10])
    day = m.next_day(as_of)
    groups = {"Q20": ([r["code"] for r in q20], RULE["q20"]), "Q5": ([r["code"] for r in q5], RULE["picks"]),
              "U": ([r["code"] for r in ranked], len(ranked))}
    prices = entry(m, day, groups["U"][0])
    bench_entry = screening.benchmark_prices(m.pro, day, day, "open")
    horizons = {}
    for months in HORIZONS:
        hday = m.first_on_or_after(screening._months_after(day, months))
        if hday is None:
            continue
        marks = mark(m, day, hday, [c for c, p in prices.items() if p["filled"]])
        values = {name: screening.group_return({"prices": prices}, {"prices": marks}, codes, seats, RULE["cost"])
                  for name, (codes, seats) in groups.items()}
        bench = screening.benchmark_prices(m.pro, day, hday, "close")
        values |= {code: bench[code]["value"] / bench_entry[code]["value"] - 1 for code in bench}
        horizons[f"{months}m"] = {"date": hday, "values": values,
                                  "stale": sorted(c for c, p in marks.items() if p.get("stale"))}
    log.info("%s: %s ranked, %s, Q5 %s", as_of, len(ranked), counts, [r["name"] for r in q5])
    return {"as_of": as_of, "entry": day, "funnel": funnel | {f"factor_{k}": v for k, v in counts.items()},
            "no_industry": len(no_industry), "fetch_failures": failed,
            "q20": [{k: r.get(k) for k in ("code", "name", "industry", "score", "roe", "cash", "growth", "pe")}
                    for r in q20],
            "q5": [r["code"] for r in q5],
            "unfilled": {c: p["reason"] for c, p in prices.items() if not p["filled"] and c in groups["Q20"][0]},
            "horizons": horizons}


def main() -> None:
    global CACHE
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start", default="201601")
    parser.add_argument("--end", default="202510")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    CACHE = args.out / "cache"
    CACHE.mkdir(parents=True, exist_ok=True)
    pro = screening._pro()
    m = Market(pro, screening.latest_session(pro))
    results = args.out / "batches"
    results.mkdir(exist_ok=True)
    for as_of in m.month_ends(args.start, args.end):
        path = results / f"{as_of}.json"
        if path.exists():
            continue
        started = time.monotonic()
        path.write_text(json.dumps(batch(m, as_of, args.workers), ensure_ascii=False, indent=1))
        log.info("%s done in %.0fs", as_of, time.monotonic() - started)


if __name__ == "__main__":
    main()
