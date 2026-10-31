"""Forecasts attached to checkpoints (judgment validation V1): structure checks and mechanical scoring.

A checkpoint may carry {"metric": "T.xxx", "period": "YYYY-MM-DD", "low": n, "high": n, "confidence": 60|75|90}:
a closed interval for one financial-table row in the next periodic report, in that row's unit. Once the
period is disclosed it scores against the table: interval hit, and whether the interval midpoint beats the
naive forecast that the base period simply continues. See docs/judgment-validation.md.
"""
from decimal import Decimal, InvalidOperation

CONFIDENCE = (60, 75, 90)
MIN_FORECASTS = 2
QUARTER_ENDS = ("03-31", "06-30", "09-30", "12-31")
# Rows whose values are point-in-time balances; every other 亿元 row is a cumulative flow within the year.
BALANCES = {"T.cash", "T.interest_debt", "T.receivables", "T.inventory", "T.contract_liab", "T.goodwill"}


def next_period(period: str) -> str:
    """Quarter end after `period` ("2026-09-30" → "2026-12-31")."""
    year, index = int(period[:4]), QUARTER_ENDS.index(period[5:])
    return f"{year + 1}-{QUARTER_ENDS[0]}" if index == 3 else f"{year}-{QUARTER_ENDS[index + 1]}"


def base_period(table: list[dict]) -> str | None:
    """Latest period of the income table the report was written on."""
    rows = {r["id"]: r for r in table}
    values = (rows.get("T.revenue") or {}).get("values") or {}
    return max(values) if values else None


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def problems(forecast, table: list[dict]) -> list[str]:
    """Why one forecast object is malformed; empty when it can be scored mechanically later."""
    if not isinstance(forecast, dict):
        return ["forecast 必须是对象"]
    rows = {r["id"]: r for r in table if r["id"].startswith("T.")}
    out = []
    metric = forecast.get("metric")
    if metric not in rows:
        out.append(f"forecast.metric 须是资料财务表中的 T.* 行标识，可用：{'、'.join(sorted(rows))}")
    base = base_period(table)
    expected = next_period(base) if base else None
    if forecast.get("period") != expected:
        out.append(f"forecast.period 须是下一期定期报告的期间键 {expected}")
    low, high = _number(forecast.get("low")), _number(forecast.get("high"))
    if low is None or high is None:
        out.append("forecast.low 与 forecast.high 须都是数字（闭区间，单位同该行）")
    elif low > high:
        out.append("forecast.low 不能大于 forecast.high")
    if forecast.get("confidence") not in CONFIDENCE:
        out.append(f"forecast.confidence 须是 {CONFIDENCE} 之一（实际值落入区间的把握，%）")
    return out


def count_problem(checkpoints) -> str | None:
    """At least MIN_FORECASTS checkpoints must carry a forecast (or all of them, when there are fewer)."""
    if not isinstance(checkpoints, list):
        return None
    have = sum(isinstance(c, dict) and "forecast" in c for c in checkpoints)
    need = min(MIN_FORECASTS, len(checkpoints))
    return None if have >= need else f"至少 {need} 条检查点须带 forecast（当前 {have} 条）"


def describe(forecast: dict, table: list[dict]) -> str:
    row = next((r for r in table if r["id"] == forecast.get("metric")), {})
    return (f"{row.get('label', forecast.get('metric'))} {forecast.get('period')}："
            f"{forecast.get('low')}–{forecast.get('high')}{row.get('unit', '')}，把握 {forecast.get('confidence')}%")


def baseline(metric: str, unit: str, period: str, base: str, values: dict[str, Decimal]) -> Decimal | None:
    """Naive forecast: ratios and balances stay at the base value; cumulative flows grow at the base period's YTD rate."""
    if base not in values:
        return None
    if unit != "亿元" or metric in BALANCES:
        return values[base]
    prior_base, prior_period = f"{int(base[:4]) - 1}{base[4:]}", f"{int(period[:4]) - 1}{period[4:]}"
    if prior_base not in values or prior_period not in values or values[prior_base] == 0:
        return None
    # Same-period prior-year value scaled by the base period's growth (a Q1 forecast uses the full prior year).
    return values[prior_period] * values[base] / values[prior_base]


def score(forecast: dict, base: str, row: dict) -> dict:
    """Score against a later table row; 'pending' until the forecast period is disclosed."""
    values = {p: Decimal(v) for p, v in (row.get("exact_values") or row.get("values") or {}).items()}
    period = forecast["period"]
    if period not in values:
        return {"status": "pending"}
    actual, low, high = values[period], Decimal(str(forecast["low"])), Decimal(str(forecast["high"]))
    naive = baseline(forecast["metric"], row.get("unit", ""), period, base, values)
    mid = (low + high) / 2
    return {"status": "scored", "actual": float(actual), "hit": low <= actual <= high,
            "baseline": float(naive) if naive is not None else None,
            "beat_baseline": abs(mid - actual) < abs(naive - actual) if naive is not None else None}
