"""Pure A4b disclosure checks; no storage, fetching or changes to frozen judgments.

Observation values use Decimal bounds and an optional metric-local reason. Missing
bounds are unbounded; a reason prevents judgment even when numbers are present.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypedDict, NotRequired

from a_share_research import companies, forecasts, stance


class _Interval(TypedDict):
    low: Decimal | None
    high: Decimal | None
    reason: NotRequired[str]


_BASIS_REASON = "口径不符：快报营业收入不等于营业总收入口径"


def _number(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise companies.SourceError(f"披露数值无效：{value!r}") from exc
    if number.is_nan():
        return None
    if not number.is_finite():
        raise companies.SourceError(f"披露数值非有限：{value!r}")
    return number


def _day(value: object) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise companies.SourceError(f"披露日期无效：{value!r}") from exc


def _base(row: dict, code: str, source: str) -> dict:
    if str(row.get("ts_code", ""))[:6] != code:
        raise companies.SourceError("披露数据返回其他公司")
    return {"source": source, "period": _day(row["end_date"]), "announced": _day(row["ann_date"]),
            "identity": f"{source}:{row['ts_code']}:{row['end_date']}:{row['ann_date']}"}


def _scaled(value: object, divisor: int) -> Decimal | None:
    number = _number(value)
    return number / divisor if number is not None else None


def _unique(observations: list[dict]) -> list[dict]:
    versions = {}
    for observation in observations:
        key = observation["identity"]
        if key in versions and versions[key] != observation:
            raise companies.SourceError(f"同一披露版本数值冲突：{key}")
        versions[key] = observation
    return list(versions.values())


def normalize_forecast(rows: list[dict], code: str) -> list[dict]:
    """Normalize TuShare forecasts; reject wrong companies or conflicting versions."""
    observations = []
    for row in rows:
        observation = _base(row, code, "forecast")
        observation.update(values={"T.net_profit": {
            "low": _scaled(row.get("net_profit_min"), 10000),
            "high": _scaled(row.get("net_profit_max"), 10000)}},
            type=row.get("type"), first_announced=_day(row["first_ann_date"]) if row.get("first_ann_date") else None)
        observations.append(observation)
    return _unique(observations)


def normalize_express(rows: list[dict], code: str, *, revenue_basis_matches: bool | None) -> list[dict]:
    """Normalize express point values; retain revenue with its basis warning."""
    observations = []
    for row in rows:
        observation = _base(row, code, "express")
        values: dict[str, _Interval] = {}
        for metric, column in (("T.net_profit", "n_income"), ("T.revenue", "revenue")):
            value = _scaled(row.get(column), 100000000)
            values[metric] = {"low": value, "high": value}
        if revenue_basis_matches is not True:
            values["T.revenue"]["reason"] = _BASIS_REASON
        observations.append(observation | {"values": values})
    return _unique(observations)


def revenue_basis_matches(income_rows: list[dict]) -> bool | None:
    """Compare the latest consolidated income basis; conflicting versions are unknown."""
    rows = [r for r in income_rows if r.get("report_type") == "1"]
    if not rows:
        return None
    period = max(_day(r["end_date"]) for r in rows)
    results = set()
    for row in rows:
        if _day(row["end_date"]) != period:
            continue
        total, revenue = _number(row.get("total_revenue")), _number(row.get("revenue"))
        results.add(total == revenue if total is not None and revenue is not None else None)
    return results.pop() if len(results) == 1 else None


def _shape(value: object, kind: str) -> bool:
    # Frozen items have already passed table membership and next-period validation.
    # Here only structural validity is checked, without inventing a historical table.
    if not isinstance(value, dict) or not isinstance(value.get("metric"), str):
        return False
    period = value.get("period")
    if not value["metric"].startswith("T.") or not isinstance(period, str):
        return False
    try:
        parsed = date.fromisoformat(period)
    except ValueError:
        return False
    if parsed.isoformat() != period or period[5:] not in forecasts.QUARTER_ENDS:
        return False
    if kind == "forecast":
        low, high = forecasts._number(value.get("low")), forecasts._number(value.get("high"))
        return low is not None and high is not None and low <= high and value.get("confidence") in forecasts.CONFIDENCE
    return (set(value) == {"metric", "period", "op", "value"}
            and value["op"] in stance.OPERATORS and stance._number(value["value"]) is not None)


def items(body: dict) -> list[dict]:
    """Extract valid frozen items in checkpoint order, followed by the active trigger."""
    out = []
    checkpoints = body.get("checkpoints")
    for i, checkpoint in enumerate(checkpoints if isinstance(checkpoints, list) else []):
        value = checkpoint.get("forecast") if isinstance(checkpoint, dict) else None
        if _shape(value, "forecast"):
            out.append(value | {"ref": f"checkpoints[{i}]", "kind": "forecast"})
    stance = body.get("stance")
    calc = body.get("stance_calc")
    stance = stance if isinstance(stance, dict) else {}
    calc = calc if isinstance(calc, dict) else {}
    kind = {"现在分批": "add_trigger", "等证据": "trigger"}.get(stance.get("action"))
    value = calc.get(kind, stance.get("trigger") if kind == "trigger" else None)
    if kind and _shape(value, kind):
        out.append(value | {"ref": kind, "kind": kind})
    return out


def judge(item: dict, observation: dict) -> dict | None:
    """Judge normalized bounds; None means this observation does not cover the item."""
    if item["period"] != observation["period"] or item["metric"] not in observation["values"]:
        return None
    value = observation["values"][item["metric"]]
    a, b = value["low"], value["high"]
    outcome, direction, reason = "undetermined", None, value.get("reason")
    if not reason and a is None and b is None:
        reason = "预告未给出数值区间" if observation["source"] == "forecast" else "披露未给出数值"
    if not reason:
        if item["kind"] == "forecast":
            low, high = Decimal(str(item["low"])), Decimal(str(item["high"]))
            if a is not None and a > high:
                outcome, direction = "deviated", "above"
            elif b is not None and b < low:
                outcome, direction = "deviated", "below"
            elif a is not None and b is not None and low <= a and b <= high:
                outcome = "inside"
        else:
            threshold = Decimal(str(item["value"]))
            if item["op"] == ">=":
                if a is not None and a >= threshold:
                    outcome = "met"
                elif b is not None and b < threshold:
                    outcome = "missed"
            elif item["op"] == "<=":
                if b is not None and b <= threshold:
                    outcome = "met"
                elif a is not None and a > threshold:
                    outcome = "missed"
    return {**{key: item[key] for key in ("ref", "kind", "metric", "period")},
            **{key: observation[key] for key in ("source", "identity", "announced")},
            "observed": {"low": str(a) if a is not None else None, "high": str(b) if b is not None else None},
            "outcome": outcome, "direction": direction, "reason": reason}


def periodic_observation(table: list[dict], period: str, *, identity: str, announced: str) -> dict:
    """Adapt the V1 financial table, preserving exact values and unavailable reasons."""
    values: dict[str, _Interval] = {}
    for row in table:
        if not row["id"].startswith("T."):
            continue
        if reason := row.get("unavailable", {}).get(period):
            values[row["id"]] = {"low": None, "high": None, "reason": reason}
        else:
            points = row.get("exact_values") or row.get("values") or {}
            if period in points:
                value = _number(points[period])
                values[row["id"]] = {"low": value, "high": value}
    return {"source": "periodic", "period": period, "identity": identity, "announced": announced, "values": values}


def latest(checks: list[dict]) -> list[dict]:
    """Select effective checks by announcement, source priority, then identity; retain ref order."""
    priority = {"forecast": 0, "express": 1, "periodic": 2}
    def rank(check: dict) -> tuple[str, int, str]:
        return check["announced"], priority[check["source"]], check["identity"]
    selected = {}
    for check in checks:
        ref = check["ref"]
        if ref not in selected or rank(check) > rank(selected[ref]):
            selected[ref] = check
    return list(selected.values())
