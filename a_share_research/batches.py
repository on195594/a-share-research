"""Saved candidate batch values shared by the worker and web."""

import json


def load(row) -> dict:
    batch = dict(row)
    for key in ("rule", "universe", "q20", "q5", "selection", "l5", "entry", "marks"):
        batch[key] = json.loads(batch[key]) if batch.get(key) else None
    return batch


def group_return(entry: dict, mark: dict, codes: list[str], seats: int, cost: float) -> float | None:
    """Equal-weight unit NAV return: unfilled or empty seats are cash; cost applied once on the whole group."""
    if not codes:
        return None
    total = 0.0
    for code in codes:
        e, m = entry["prices"].get(code, {}), mark["prices"].get(code)
        if e.get("filled") and m:
            total += m["close"] * m["adj"] / (e["open"] * e["adj"])
        else:
            total += 1.0
    total += seats - len(codes)
    return (total / seats) * (1 - cost) - 1


def returns(batch: dict) -> dict | None:
    entry, mark, rule = batch.get("entry"), (batch.get("marks") or {}).get("latest"), batch["rule"]
    if not entry or not mark:
        return None
    q20 = [r["code"] for r in batch["q20"]]
    groups = {"L5": (batch["l5"]["codes"], rule["picks"]), "Q5": (batch["q5"], rule["picks"]), "Q20": (q20, len(q20))}
    out = {name: group_return(entry, mark, codes, seats, rule["cost"]) for name, (codes, seats) in groups.items()}
    for code, b in mark["benchmarks"].items():
        out[code] = b["value"] / entry["benchmarks"][code]["value"] - 1
    return {"date": mark["date"], "values": out}


FROZEN_BY = {"owner": "所有者确认", "auto": "草案生成后按模型草案自动冻结，未经人工确认",
             "delegate": "所有者授权 Claude 代为冻结"}


RULE = {
    "version": "screen-v1.1",  # v1.1 adds the windfall flag; scoring and picks unchanged
    "min_list_years": 3,
    "min_total_mv_yi": 100,
    "min_avg_amount_yi": 1,
    "amount_days": 20,
    "momentum_days": 60, "drawdown_flag": -0.3,  # recorded and shown, never scored
    # Recorded and shown, never scored: in the 2016–2025 backtest these picks lagged the rest (screening-experiment §5).
    "windfall_flag": {"min_growth": 0.5, "max_pe": 10},
    "weights": {"roe": 0.2, "cash": 0.2, "growth": 0.2, "pe": 0.4},
    "industry": "SW2021 L1",
    "min_group": 10,
    "q20": 20, "q20_per_industry": 3,
    "picks": 5, "picks_per_industry": 2,
    "cost": 0.002,  # flat round-trip cost on unit NAV
    "benchmarks": {"H00906.CSI": "中证800全收益", "H00300.CSI": "沪深300全收益"},
    "horizons_months": [3, 6, 12],
}
