"""Pure A4b disclosure checks; no storage, fetching or changes to frozen judgments.

Observation values use Decimal bounds and an optional metric-local reason. Missing
bounds are unbounded; a reason prevents judgment even when numbers are present.
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypedDict, NotRequired

from a_share_research import companies, forecasts, stance


class _Interval(TypedDict):
    low: Decimal | None
    high: Decimal | None
    reason: NotRequired[str]
    # Half-width of the source's rounding, in 亿元; comparisons closer than this are undetermined.
    tolerance: NotRequired[Decimal]


IDENTITY_CONFLICT_REASON = "同一披露版本内容冲突"

_BASIS_REASON = "口径不符：快报营业收入不等于营业总收入口径"
# TuShare rounds forecast bounds to whole 万元 (603960.SH 2026-01-23: 2,691.29 万元 in the 巨潮 PDF → 2691.0).
_FORECAST_TOLERANCE = Decimal("0.0001")


def _express_tolerance(value: object) -> Decimal:
    """Express figures are the company's rounded numbers (601009.SH: 555.40 亿元 → 55540000000 元).

    Infer the rounding unit from trailing zeros of the yuan value; a genuinely round number only
    widens the margin, which can turn a verdict into 未定 but never into a false deviation."""
    number = _number(value)
    if number is None or number != number.to_integral_value():
        return Decimal(0)
    digits, unit = int(abs(number)), 1
    while digits and digits % 10 == 0 and unit < 10**8:
        digits //= 10
        unit *= 10
    return Decimal(unit) / 2 / 100000000 if unit > 1 else Decimal(0)


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
        group = versions.setdefault(observation["identity"], [])
        if observation not in group:
            group.append(observation)
    result = []
    for group in versions.values():
        if len(group) == 1:
            result.append(group[0])
            continue
        # Sorting canonicalizes an unordered provider response for deduplication
        # only. It never establishes a "newer" disclosure or a numeric verdict.
        variants = sorted(group, key=lambda o: json.dumps(o, sort_keys=True, default=str))
        values = {}
        for metric in dict.fromkeys(m for o in group for m in o["values"]):
            bounds = [o["values"].get(metric) for o in group]
            values[metric] = bounds[0] if all(b == bounds[0] for b in bounds) else {
                "low": None, "high": None, "reason": IDENTITY_CONFLICT_REASON}
        result.append(group[0] | {"values": values, "variants": variants})
    return result


def normalize_forecast(rows: list[dict], code: str) -> list[dict]:
    """Normalize TuShare forecasts; retain conflicting variants as undetermined."""
    observations = []
    for row in rows:
        observation = _base(row, code, "forecast")
        observation.update(values={"T.net_profit": {
            "low": _scaled(row.get("net_profit_min"), 10000),
            "high": _scaled(row.get("net_profit_max"), 10000),
            "tolerance": _FORECAST_TOLERANCE}},
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
            values[metric] = {"low": value, "high": value, "tolerance": _express_tolerance(row.get(column))}
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


def _outcome(item: dict, a: Decimal | None, b: Decimal | None, tolerance: Decimal) -> tuple[str, str | None]:
    """Compare observed bounds widened by the source's rounding tolerance."""
    a = a - tolerance if a is not None else None
    b = b + tolerance if b is not None else None
    if item["kind"] == "forecast":
        low, high = Decimal(str(item["low"])), Decimal(str(item["high"]))
        if a is not None and a > high:
            return "deviated", "above"
        if b is not None and b < low:
            return "deviated", "below"
        if a is not None and b is not None and low <= a and b <= high:
            return "inside", None
        return "undetermined", None
    threshold = Decimal(str(item["value"]))
    if item["op"] == ">=":
        if a is not None and a >= threshold:
            return "met", None
        if b is not None and b < threshold:
            return "missed", None
    elif item["op"] == "<=":
        if b is not None and b <= threshold:
            return "met", None
        if a is not None and a > threshold:
            return "missed", None
    return "undetermined", None


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
        tolerance = value.get("tolerance", Decimal(0))
        outcome, direction = _outcome(item, a, b, tolerance)
        if outcome == "undetermined" and tolerance and _outcome(item, a, b, Decimal(0))[0] != "undetermined":
            reason = f"披露数值精度约 ±{tolerance.normalize():f} 亿元，距边界过近，不能判定"
    return {**{key: item[key] for key in ("ref", "kind", "metric", "period")},
            **{key: observation[key] for key in ("source", "identity", "announced")},
            "observed": {"low": str(a) if a is not None else None, "high": str(b) if b is not None else None},
            "outcome": outcome, "direction": direction, "reason": reason}


def periodic_observation(table: list[dict], period: str, *, identity: str, announced: str,
                         acquired: bool = False) -> dict:
    """Adapt the V1 financial table, preserving exact values and unavailable reasons."""
    values: dict[str, _Interval] = {}
    acquired = acquired or any(period in (row.get(key) or {}) for row in table
                               for key in ("values", "exact_values", "unavailable"))
    for row in table:
        if not row["id"].startswith("T."):
            continue
        if reason := row.get("unavailable", {}).get(period):
            values[row["id"]] = {"low": None, "high": None, "reason": reason}
        else:
            points = row.get("exact_values") or row.get("values") or {}
            if period in points or acquired:
                value = _number(points.get(period))
                values[row["id"]] = {"low": value, "high": value}
                if value is None:
                    values[row["id"]]["reason"] = "已取得该期报表，但目标字段缺失"
    return {"source": "periodic", "period": period, "identity": identity, "announced": announced, "values": values}


def latest(checks: list[dict]) -> list[dict]:
    """Use disclosure dates/source priority and persisted per-identity revisions.

    Different identities tied on date/priority have no reliable order. A content
    hash (or provider row order) is not evidence of a correction.
    """
    priority = {"forecast": 0, "express": 1, "periodic": 2}
    def rank(check: dict) -> tuple[str, int]:
        return check["announced"], priority[check["source"]]
    grouped = {}
    for check in checks:
        grouped.setdefault(check["ref"], []).append(check)
    selected = []
    for group in grouped.values():
        best = max(map(rank, group))
        versions = {}
        for check in group:
            if rank(check) != best:
                continue
            old = versions.get(check["identity"])
            if old is None or check.get("version", 1) > old.get("version", 1):
                versions[check["identity"]] = check
        candidates = list(versions.values())
        current = candidates[-1]
        if any(any(c.get(k) != current.get(k) for k in ("observed", "outcome", "direction", "reason"))
               for c in candidates):
            current = current | {"outcome": "undetermined", "direction": None,
                                 "observed": {"low": None, "high": None},
                                 "reason": "同日披露版本顺序不明，内容冲突"}
        selected.append(current)
    return selected


def normalize_periodic(groups: list[dict], code: str) -> list[dict]:
    """Adapt table slices with their own statement version, not another statement's date."""
    observations = []
    for group in groups:
        row = group['row']
        base = _base(row, code, 'periodic')
        announced = max(base['announced'], _day(row['f_ann_date']) if row.get('f_ann_date') else base['announced'])
        identity = f"{base['identity']}:{group['statement']}:{announced}:{row.get('update_flag') or ''}"
        observations.append(periodic_observation(group['table'], base['period'], identity=identity, announced=announced, acquired=True))
    return _unique(observations)
