"""Pure point-in-time eligibility, factors and ranking; no batch storage or model runtime."""

from datetime import date, datetime, timedelta

import pandas as pd


from a_share_research.batches import RULE


FINANCIAL_INDUSTRIES = {"银行", "保险", "证券", "多元金融", "非银金融"}


def announced_by(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Rows public by `as_of`. A restated statement keeps the original ann_date; f_ann_date dates the restatement."""
    day = frame["ann_date"].astype(str)
    if "f_ann_date" in frame:
        day = frame["f_ann_date"].where(frame["f_ann_date"].notna(), frame["ann_date"]).astype(str)
    return frame[day <= as_of]


def annual_rows(frame: pd.DataFrame, *, consolidated: bool) -> pd.DataFrame:
    """One row per annual period: consolidated statements, the latest restatement when several exist."""
    frame = frame[frame["end_date"].astype(str).str.endswith("1231")].copy()
    if consolidated and "report_type" in frame:
        frame = frame[frame["report_type"].astype(str) == "1"]
    order = [c for c in ("f_ann_date", "update_flag") if c in frame]
    if order:
        frame = frame.sort_values(order, kind="stable")
    return frame.groupby("end_date").tail(1).set_index("end_date").sort_index()


def indicator_check(indicator: pd.DataFrame, as_of: str) -> dict:
    """Eligibility from annual indicators announced by `as_of`: four consecutive years, last three profitable."""
    ind = annual_rows(announced_by(indicator, as_of), consolidated=False)
    years = list(ind.index[-4:])
    if len(years) < 4:
        return {"status": "gap", "reason": f"年度财务指标不足 4 期（{len(years)}）"}
    if any(int(b[:4]) - int(a[:4]) != 1 for a, b in zip(years, years[1:])):
        return {"status": "gap", "reason": "年度财务指标不连续"}
    profit = [None if pd.isna(v := ind["profit_dedt"].get(y)) else float(v) for y in years]
    if any(v is None for v in profit[1:]):
        return {"status": "gap", "reason": "扣非净利润缺失"}
    if any(v <= 0 for v in profit[1:]):
        return {"status": "excluded", "reason": "近 3 年有扣非净利润非正"}
    roe = [None if pd.isna(v := ind["roe_dt"].get(y)) else float(v) for y in years[1:]]
    return {"status": "ok", "years": [y[:4] for y in years], "profit_dedt": profit, "roe_dt": roe}


def factors(indicator: pd.DataFrame, income: pd.DataFrame, cashflow: pd.DataFrame, as_of: str) -> dict:
    """Screen factors from one company's annual data announced by `as_of`; pure for tests.

    Returns {"status": "ok"|"excluded"|"gap", "reason", "years", factor values...}.
    """
    out = indicator_check(indicator, as_of)
    if out["status"] != "ok":
        return out
    last3 = [f"{y}1231" for y in out["years"][1:]]
    inc = annual_rows(announced_by(income, as_of), consolidated=True)
    cf = annual_rows(announced_by(cashflow, as_of), consolidated=True)
    ni = [inc["n_income"].get(y) if "n_income" in inc else None for y in last3]
    ocf = [cf["n_cashflow_act"].get(y) if "n_cashflow_act" in cf else None for y in last3]
    gaps = []
    out["roe"] = None if None in out["roe_dt"] else sum(out["roe_dt"]) / 3
    if out["roe"] is None:
        gaps.append("扣非ROE缺失")
    out["cash"] = None
    if any(v is None or pd.isna(v) for v in ni + ocf):
        gaps.append("合并净利润或经营现金流缺失")
    elif (denominator := sum(map(float, ni))) <= 0:
        gaps.append("3 年合并净利润之和非正")
    else:
        out["cash"] = sum(map(float, ocf)) / denominator
    base = out["profit_dedt"][0]
    out["growth"] = None
    if base is None or base <= 0:
        gaps.append("三年前扣非净利润非正或缺失，无法计算增速")
    else:
        out["growth"] = (out["profit_dedt"][-1] / base) ** (1 / 3) - 1
    if gaps:
        out |= {"status": "gap", "reason": "；".join(gaps)}
    return out


def market_filters(frame: pd.DataFrame, suspended: set[str], as_of: str, rule: dict = RULE) -> tuple[pd.DataFrame, dict, list[str]]:
    """Coarse screen on one session's cross-section; returns survivors, the funnel and codes lacking an industry.

    `frame` needs ts_code, name, industry, industry_sw, list_date, total_mv (万元) and avg_amount (千元).
    """
    listed_before = _months_after(as_of, -12 * rule["min_list_years"])
    funnel = {"listed": len(frame)}
    steps = [
        ("financial", lambda f: ~(f["industry"].isin(FINANCIAL_INDUSTRIES) | f["industry_sw"].isin(FINANCIAL_INDUSTRIES))),
        ("st", lambda f: ~f["name"].str.contains("ST|退", na=False)),
        ("suspended", lambda f: ~f["ts_code"].isin(suspended)),
        ("listed_years", lambda f: f["list_date"].astype(str) <= listed_before),
        ("market_cap", lambda f: f["total_mv"].fillna(0) >= rule["min_total_mv_yi"] * 1e4),  # 万元
        ("liquidity", lambda f: f["avg_amount"] >= rule["min_avg_amount_yi"] * 1e5),  # 千元
    ]
    for name, keep in steps:
        frame = frame[keep(frame)]
        funnel[f"after_{name}"] = len(frame)
    no_industry = frame[frame["industry_sw"].isna()]["ts_code"].tolist()
    frame = frame[frame["industry_sw"].notna()]
    funnel["after_industry_mapping"] = len(frame)
    return frame, funnel, no_industry


def windfall(row: dict, rule: dict = RULE) -> dict | None:
    """Profit jump at a low multiple (three-year 扣非 CAGR ≥ min_growth, PE TTM < max_pe): the thresholds, else None."""
    flag = rule.get("windfall_flag")
    return dict(flag) if flag and row["growth"] >= flag["min_growth"] and row["pe"] < flag["max_pe"] else None


def percentiles(values: list[float]) -> list[float]:
    """Average-rank percentile in [0, 1], higher value → higher percentile; one value → 0.5."""
    n = len(values)
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return [r / (n - 1) for r in ranks]


def score(rows: list[dict], rule: dict = RULE) -> list[dict]:
    """Industry-relative percentiles and weighted score for ranked rows (status ok, all factors)."""
    by_industry: dict[str, list[dict]] = {}
    for row in rows:
        by_industry.setdefault(row["industry"], []).append(row)
    for members in by_industry.values():
        for key, sign in (("roe", 1), ("cash", 1), ("growth", 1), ("pe", -1)):
            for row, pct in zip(members, percentiles([sign * m[key] for m in members])):
                row[f"{key}_pct"] = pct
        for row in members:
            row["group_size"] = len(members)
            row["small_group"] = len(members) < rule["min_group"]
            row["score"] = sum(rule["weights"][k] * row[f"{k}_pct"] for k in rule["weights"])
    return sorted(rows, key=lambda r: (-r["score"], r["code"]))


def pick(rows: list[dict], n: int, per_industry: int) -> list[dict]:
    """First n rows in order with at most per_industry from one industry."""
    chosen, counts = [], {}
    for row in rows:
        if counts.get(row["industry"], 0) < per_industry:
            chosen.append(row)
            counts[row["industry"]] = counts.get(row["industry"], 0) + 1
            if len(chosen) == n:
                break
    return chosen


def _months_after(day: str, months: int) -> str:
    d = datetime.strptime(day, "%Y%m%d").date()
    year, month = divmod(d.month - 1 + months, 12)
    target = date(d.year + year, month + 1, 1)
    last = (date(target.year + (target.month == 12), target.month % 12 + 1, 1) - timedelta(days=1)).day
    return target.replace(day=min(d.day, last)).strftime("%Y%m%d")
