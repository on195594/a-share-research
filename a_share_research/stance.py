"""Research stance: a structured, later-scored answer to "act now or wait for what" (judgment validation V4).

New reports choose 现在分批, 等价格 or 回避. A next-disclosure add_trigger is optional for
现在分批 and never blocks the first tranche. Legacy 等证据 remains readable and scoreable.

Sizing and real orders stay with the account owner and Skills. See docs/judgment-validation.md.
"""
from decimal import Decimal, InvalidOperation

from a_share_research import forecasts, valuation

ACTIONS = ("现在分批", "等价格", "回避")
OPERATORS = (">=", "<=")
WAIT_MONTHS = 6
# Same-version reruns on one market day moved the v2 hurdle by up to ±5.5% around its midpoint (11-05: 新乳业
# 15.89–17.74 around a 16.90 close flipped 现在分批/等价格). Within this band the side of the hurdle is not
# treated as a stable judgment. Owner-approved 5%; it narrows the noise, it does not remove it.
BOUNDARY = Decimal("0.05")


def hurdle_margin(valuation_calc) -> float | None:
    """Close relative to the v2 expected-return hurdle price (close / hurdle - 1), or None when not priced."""
    calc = valuation_calc if isinstance(valuation_calc, dict) else {}
    v2 = calc.get("v2") if isinstance(calc.get("v2"), dict) else {}
    close, hurdle = calc.get("close"), v2.get("support_below")
    if not all(isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in (close, hurdle)):
        return None
    return close / hurdle - 1


def on_boundary(valuation_calc) -> bool | None:
    """True when the close sits within BOUNDARY of the hurdle; None when there is no priced hurdle."""
    margin = hurdle_margin(valuation_calc)
    if margin is None:
        return None
    calc = valuation_calc
    # Decimal on the shown values keeps the band edge exact (105 vs 100 is on it, not 5.000…04% off).
    return abs(Decimal(str(calc["close"])) / Decimal(str(calc["v2"]["support_below"])) - 1) <= BOUNDARY


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def trigger_problems(trigger, table: list[dict]) -> list[str]:
    if not isinstance(trigger, dict) or set(trigger) != {"metric", "period", "op", "value"}:
        return ['add_trigger 检查点须为: {"metric": "T.* 行标识", "period": "下一期期间键", "op": ">= 或 <=", "value": 数字}']
    out = []
    rows = {r["id"] for r in table if r["id"].startswith("T.")}
    if not isinstance(trigger["metric"], str) or trigger["metric"] not in rows:
        out.append(f"stance.add_trigger.metric 须是资料财务表中的 T.* 行标识，可用：{'、'.join(sorted(rows))}")
    base = forecasts.base_period(table)
    expected = forecasts.next_period(base) if base else None
    if trigger["period"] != expected:
        out.append(f"stance.add_trigger.period 须是下一期定期报告的期间键 {expected}（累计口径同财务表）")
    if trigger["op"] not in OPERATORS:
        out.append("stance.add_trigger.op 须是 >= 或 <=，表示达到该阈值才加后续批次")
    if _number(trigger["value"]) is None:
        out.append("stance.add_trigger.value 须是有限数字，单位同该行")
    return out


def wait_price(value, table: list[dict], market_date: str) -> tuple[float | None, str | None]:
    """The v2 hurdle price for 等价格, or why the stance cannot be a price wait."""
    if not isinstance(value, dict) or "not_priced" in value:
        return None, "等价格需要三情景估值；未定价时请选择现在分批或回避，并说明依据与风险"
    try:
        calc = valuation.compute(value, table, market_date)
    except ValueError as exc:
        return None, f"等价格需要可测算的三情景估值：{exc}"
    price = calc["v2"]["support_below"]
    if price is None or price <= 0:
        shown = "无" if price is None else f"{price:.2f} 元"
        return price, (f"期望回报门槛价（{shown}）非正数，等价格不成立："
                       "请选择现在分批或回避，并说明依据与风险")
    if calc["close"] <= price:
        return price, (f"现价 {calc['close']:g} 元已不高于期望回报门槛价 {price:.2f} 元，等价格不成立："
                       "请选择现在分批或回避，并说明依据与风险")
    return price, None


def problems(stance, table: list[dict], value, market_date: str) -> list[str]:
    """Structure only; the stance text and cites use checking's ordinary item checks."""
    if not isinstance(stance, dict):
        return ['stance 须为 {"action": "现在分批 | 等价格 | 回避", "text": "理由", "cites": [出处]}']
    out = []
    action = stance.get("action")
    if action not in ACTIONS:
        out.append(f"stance.action 须是 {'、'.join(ACTIONS)} 之一")
    allowed = {"action", "text", "cites"}
    if action == "现在分批" and "add_trigger" in stance:
        allowed.add("add_trigger")
    if set(stance) != allowed:
        out.append("stance 只含 action、text、cites；现在分批可另带 add_trigger"
                   + ("；等价格的价格由程序按估值计算，不要填写" if action == "等价格" else ""))
    if "add_trigger" in allowed:
        out += trigger_problems(stance["add_trigger"], table)
    if action == "等价格":
        _, problem = wait_price(value, table, market_date)
        if problem:
            out.append(problem)
    return out


def met(trigger: dict, actual) -> bool | None:
    """Whether a disclosed table value (stored as text) satisfies an evidence trigger; None if absent."""
    threshold = _number(trigger["value"])
    try:
        actual = Decimal(str(actual)) if actual is not None and not isinstance(actual, bool) else None
    except InvalidOperation:
        actual = None
    if actual is None or not actual.is_finite() or threshold is None:
        return None
    return actual >= threshold if trigger["op"] == ">=" else actual <= threshold


def describe(stance: dict, table: list[dict]) -> str:
    """One human line for pages and notices; the program's own price is shown separately."""
    if stance.get("action") != "等证据":
        return stance.get("action", "")
    t = stance["trigger"]
    row = next((r for r in table if r["id"] == t["metric"]), {})
    return f"等证据：{t['period']} {row.get('label', t['metric'])} {t['op'].replace('>=', '≥').replace('<=', '≤')} {t['value']:g}{row.get('unit', '')}"
