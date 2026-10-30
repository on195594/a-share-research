"""Three fixed offline screening experiments; never changes the live rule or source batches.

uv run python -m a_share_research.backtest_experiment --source DIR --calendar FILE --out DIR
The calendar is a captured TuShare SSE open-day list (20150101 through data_end).
Only read private, trusted backtest caches: they contain pickle files.
"""

import argparse
import hashlib
import json
import logging
import math
import multiprocessing
import statistics as st
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from a_share_research import backtest as bt, screening as sc, screening_factors

log = logging.getLogger(__name__)
VARIANTS = ("baseline", "industry_l2", "quarter_guard", "cash_yield")
CYCLICAL = {"有色金属", "钢铁", "煤炭", "石油石化", "基础化工", "建筑材料"}


def quarter_signal(ind: pd.DataFrame, as_of: str) -> dict:
    rows = sc.announced_by(ind, as_of).sort_values(
        ["end_date", "ann_date", "update_flag"], kind="stable", na_position="first"
    ).drop_duplicates("end_date", keep="last").set_index("end_date")
    if rows.empty:
        return {"yoy": None, "gap": "no_disclosure"}
    period = str(rows.index[-1])
    prior = f"{int(period[:4]) - 1}{period[4:]}"
    current = rows.at[period, "profit_dedt"]
    base = rows.at[prior, "profit_dedt"] if prior in rows.index else None
    if current is None or pd.isna(current) or base is None or pd.isna(base) or base <= 0:
        return {"period": period, "yoy": None, "gap": "missing_or_nonpositive_base"}
    return {"period": period, "yoy": float(current / base - 1), "gap": None}


def enrich(m: bt.Market, ranked: list[dict], frame: pd.DataFrame, as_of: str) -> None:
    members = bt._on(m.members, as_of, "in_date", "out_date")
    caps = frame.set_index("ts_code")["total_mv"]
    for row in ranked:
        code = row["code"]
        row["l2"] = str(members.at[code, "l2_code"]) if code in members.index and pd.notna(members.at[code, "l2_code"]) else None
        ind = bt.history(None, "fina_indicator", code, m.data_end, sc.INDICATOR_FIELDS)
        row["quarter"] = quarter_signal(ind, as_of)
        cf = sc.annual_rows(sc.announced_by(
            bt.history(None, "cashflow", code, m.data_end, sc.CASHFLOW_FIELDS), as_of), consolidated=True)
        amounts = [cf["n_cashflow_act"].get(f"{year}1231") for year in row["years"][1:]]
        cap = caps[code]
        row["ocf_yield"] = (sum(map(float, amounts)) / 3 / (float(cap) * 1e4)
                            if all(v is not None and pd.notna(v) and math.isfinite(v) for v in amounts)
                            and pd.notna(cap) and cap > 0 else None)


def candidates(ranked: list[dict]) -> dict[str, list[dict]]:
    # Baseline percentiles and U stay fixed for the exclusion-only quarter experiment.
    variants = {"baseline": ranked,
                "quarter_guard": [r for r in ranked if r["quarter"]["yoy"] is None or r["quarter"]["yoy"] >= -0.2]}
    l2_groups = defaultdict(list)
    for row in ranked:
        l2_groups[row["l2"]].append(row)
    l2 = []
    for group, members in l2_groups.items():
        if group is None or len(members) < sc.RULE["min_group"]:
            l2.extend(r | {"group": r["industry"], "l2_fallback": True} for r in members)
        else:
            industries = {r["code"]: r["industry"] for r in members}
            scored = sc.score([r | {"industry": group} for r in members])
            l2.extend(r | {"industry": industries[r["code"]], "group": group, "l2_fallback": False} for r in scored)
    variants["industry_l2"] = sorted(l2, key=lambda r: (-r["score"], r["code"]))
    l1_groups = defaultdict(list)
    for row in ranked:
        l1_groups[row["industry"]].append(row)
    cash = []
    for members in l1_groups.values():
        available = [r for r in members if r["ocf_yield"] is not None]
        pcts = dict(zip((r["code"] for r in available), sc.percentiles([r["ocf_yield"] for r in available])))
        for r in members:
            pct = pcts.get(r["code"])
            mixed = (r["pe_pct"] + pct) / 2 if pct is not None else r["pe_pct"]
            cash.append(r | {"ocf_pct": pct, "score": r["score"] + sc.RULE["weights"]["pe"] * (mixed - r["pe_pct"])})
    variants["cash_yield"] = sorted(cash, key=lambda r: (-r["score"], r["code"]))
    return variants


def run_batch(m: bt.Market, base: dict) -> dict:
    as_of = base["as_of"]
    frame, funnel, no_industry, no_name = bt.universe(m, as_of)
    companies = [bt.company(m, r, as_of) for r in frame.itertuples(index=False)]
    failures = [r for r in companies if str(r.get("reason", "")).startswith("取数失败")]
    if failures:
        raise sc.ScreenError(f"{as_of}: {failures[:3]}")
    ranked = sc.score([r for r in companies if r["status"] == "ok"])
    # These real-data checks make a changed universe/timing/formula fail before any attribution.
    assert len(ranked) == base["funnel"]["factor_ok"], as_of
    assert funnel == {k: base["funnel"][k] for k in funnel}, as_of
    assert len(no_industry) == base["no_industry"], as_of
    expected_names = base["no_name"]
    assert (no_name == expected_names if isinstance(expected_names, list) else len(no_name) == expected_names), as_of
    assert m.next_day(as_of) == base["entry"], as_of
    q20 = sc.pick(ranked, 20, 3)
    assert [r["code"] for r in q20] == [r["code"] for r in base["q20"]], as_of
    assert all(math.isclose(a["score"], b["score"], abs_tol=1e-12) for a, b in zip(q20, base["q20"])), as_of
    assert [r["code"] for r in sc.pick(q20, 5, 2)] == base["q5"], as_of
    enrich(m, ranked, frame, as_of)
    prices = bt.entry(m, base["entry"], [r["code"] for r in ranked])
    marks = {h: bt.mark(m, base["entry"], v["date"], [c for c, p in prices.items() if p["filled"]])
             for h, v in base["horizons"].items()}
    result = {"as_of": as_of, "entry": base["entry"], "ranked": len(ranked), "variants": {}}
    for name, rows in candidates(ranked).items():
        selected = sc.pick(rows, 20, 3)
        five = sc.pick(selected, 5, 2)
        codes = {"Q20": [r["code"] for r in selected], "Q5": [r["code"] for r in five]}
        assert all(sum(r["industry"] == industry for r in selected) <= 3 for industry in {r["industry"] for r in selected})
        assert all(sum(r["industry"] == industry for r in five) <= 2 for industry in {r["industry"] for r in five})
        horizons = {}
        for h, original in base["horizons"].items():
            values = {g: sc.group_return({"prices": prices}, {"prices": marks[h]}, cs, 20 if g == "Q20" else 5, sc.RULE["cost"])
                      for g, cs in codes.items()}
            if name == "baseline":
                values["U"] = sc.group_return({"prices": prices}, {"prices": marks[h]}, list(prices), len(prices), sc.RULE["cost"])
                assert all(math.isclose(v, original["values"][g], abs_tol=1e-12) for g, v in values.items()), (as_of, h)
            values |= {k: original["values"][k] for k in ("U", "H00906.CSI", "H00300.CSI")}
            horizons[h] = {"date": original["date"], "values": values}
        result["variants"][name] = {
            "eligible": len(rows), "q20": selected, "q5": codes["Q5"], "horizons": horizons,
            "unfilled": {g: [c for c in cs if not prices[c]["filled"]] for g, cs in codes.items()},
            "quarter_unknown": sum(r["quarter"]["yoy"] is None for r in ranked),
            "cash_unknown": sum(r["ocf_yield"] is None for r in ranked),
            "l2_fallback": sum(r.get("l2_fallback", False) for r in rows),
        }
    return result


def stats(xs: list[float]) -> dict:
    return {"n": len(xs), "mean": st.mean(xs), "median": st.median(xs),
            "positive": sum(x > 0 for x in xs)}


def summarize(batches: list[dict]) -> dict:
    out = {}
    for name in VARIANTS:
        variant = {}
        for h, months in (("6m", 6), ("12m", 12)):
            rows = [b for b in batches if h in b["variants"][name]["horizons"]]
            if not rows:
                continue
            def metrics(b):
                v = b["variants"][name]["horizons"][h]["values"]
                baseline = b["variants"]["baseline"]["horizons"][h]["values"]
                return v | {"Q5-Q20": v["Q5"] - v["Q20"], "Q20-U": v["Q20"] - v["U"],
                            "Q5-800": v["Q5"] - v["H00906.CSI"], "Q20-800": v["Q20"] - v["H00906.CSI"],
                            "delta_Q5": v["Q5"] - baseline["Q5"], "delta_Q20": v["Q20"] - baseline["Q20"]}
            def aggregate(part):
                ms = [metrics(b) for b in part]
                return {k: stats([v[k] for v in ms]) for k in ms[0]}
            phases = []
            for offset in range(min(months, len(rows))):
                chain = [rows[offset]]
                for b in rows[offset + 1:]:
                    if b["entry"] >= chain[-1]["variants"][name]["horizons"][h]["date"]:
                        chain.append(b)
                phases.append({"dates": [b["as_of"] for b in chain], "metrics": aggregate(chain)})
            variant[h] = {"all": aggregate(rows), "nonoverlap_phases": phases,
                          "years": {y: aggregate([b for b in rows if b["as_of"][:4] == y])
                                    for y in sorted({b["as_of"][:4] for b in rows})}}
        variant["coverage"] = {
            key: stats([b["variants"][name][key] for b in batches])
            for key in ("eligible", "quarter_unknown", "cash_unknown", "l2_fallback")}
        variant["unfilled"] = {g: sum(len(b["variants"][name]["unfilled"][g]) for b in batches) for g in ("Q5", "Q20")}
        variant["composition_by_year"] = {}
        for year in sorted({b["as_of"][:4] for b in batches}):
            rows = [b for b in batches if b["as_of"][:4] == year]
            variant["composition_by_year"][year] = {
                "cyclical_q5_seats": sum(r["industry"] in CYCLICAL for b in rows for r in b["variants"][name]["q20"] if r["code"] in b["variants"][name]["q5"]),
                "windfall_q5_seats": sum(sc.windfall(r) is not None for b in rows for r in b["variants"][name]["q20"] if r["code"] in b["variants"][name]["q5"]),
                "q5_seats": sum(len(b["variants"][name]["q5"]) for b in rows)}
        out[name] = variant
    return out


def implementation_hashes() -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
            (Path(__file__), Path(bt.__file__), Path(sc.__file__), Path(screening_factors.__file__))}


def main():
    if not __debug__:
        raise SystemExit("离线实验依赖断言核验基线，请勿使用 python -O 或 PYTHONOPTIMIZE")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, help="first N source batches for a smoke check")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    source, output_root = args.source.resolve(), args.out.resolve()
    if source == output_root or source in output_root.parents or output_root in source.parents:
        parser.error("output must be separate from the frozen source")
    bt.CACHE = args.source / "cache"
    calendar = json.loads(args.calendar.read_text())
    days = calendar["days"]
    assert days == sorted(set(days)) and days[0] >= "20150101" and days[-1] == calendar["end"]
    source_paths = sorted((args.source / "batches").glob("*.json"))
    if not source_paths:
        parser.error("no source batches")
    cache_hash = hashlib.sha256()
    for path in sorted(bt.CACHE.glob("*.pkl")):
        cache_hash.update(path.name.encode())
        cache_hash.update(hashlib.sha256(path.read_bytes()).digest())
    identity = {"calendar_sha256": hashlib.sha256(args.calendar.read_bytes()).hexdigest(),
                "cache_sha256": cache_hash.hexdigest(),
                "source": str(args.source.resolve()), "data_end": calendar["end"],
                "code": implementation_hashes(),
                "batches": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}}
    run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != identity:
        parser.error("source or implementation changed; choose a new output directory")
    manifest.write_text(json.dumps(identity, indent=2))
    m = bt.Market(None, calendar["end"], days=days)
    output = args.out / "batches"
    output.mkdir(exist_ok=True)
    results = []
    paths = source_paths[:args.limit]
    # Each month is independent; four processes bound memory and reuse each month's data for all variants.
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("fork")) as pool:
        for start in range(0, len(paths), 4):
            pending = {p: pool.submit(run_batch, m, json.loads(p.read_text()))
                       for p in paths[start:start + 4] if not (output / p.name).exists()}
            for path in paths[start:start + 4]:
                destination = output / path.name
                if destination.exists():
                    result = json.loads(destination.read_text())
                    assert result["run_id"] == run_id
                else:
                    result = pending[path].result() | {"run_id": run_id}
                    tmp = destination.with_suffix(".tmp")
                    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1, allow_nan=False))
                    tmp.replace(destination)
                results.append(result)
                log.info("%s checked (%s/%s)", result["as_of"], len(results), len(paths))
    (args.out / "summary.json").write_text(json.dumps(summarize(results), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
