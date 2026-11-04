"""Research stance: a structured, later-scored answer to "act now or wait for what" (judgment validation V4).

Before this field, "wait" was free: it named no price, no evidence and no date, so it could never be
scored wrong. A stance is one of four actions; both waits carry a trigger the program can check later:

- 现在分批: start a position in tranches at the current price;
- 等价格: wait for the v2 expected-return hurdle price, which the program derives from the frozen
  scenarios (the model writes no price); valid for WAIT_MONTHS after the report;
- 等证据: wait for one next-period financial-table row to cross a threshold;
- 回避: do not hold.

Sizing and real orders stay with the account owner and Skills. See docs/judgment-validation.md.
"""
from decimal import Decimal, InvalidOperation

from a_share_research import forecasts, valuation

ACTIONS = ("现在分批", "等价格", "等证据", "回避")
OPERATORS = (">=", "<=")
WAIT_MONTHS = 6


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
        return ['等证据须带 trigger: {"metric": "T.* 行标识", "period": "下一期期间键", "op": ">= 或 <=", "value": 数字}']
    out = []
    rows = {r["id"] for r in table if r["id"].startswith("T.")}
    if trigger["metric"] not in rows:
        out.append(f"stance.trigger.metric 须是资料财务表中的 T.* 行标识，可用：{'、'.join(sorted(rows))}")
    base = forecasts.base_period(table)
    expected = forecasts.next_period(base) if base else None
    if trigger["period"] != expected:
        out.append(f"stance.trigger.period 须是下一期定期报告的期间键 {expected}（累计口径同财务表）")
    if trigger["op"] not in OPERATORS:
        out.append("stance.trigger.op 须是 >= 或 <=，表示达到该阈值才改为行动")
    if _number(trigger["value"]) is None:
        out.append("stance.trigger.value 须是有限数字，单位同该行")
    return out


def wait_price(value, table: list[dict], market_date: str) -> tuple[float | None, str | None]:
    """The v2 hurdle price for 等价格, or why the stance cannot be a price wait."""
    if not isinstance(value, dict) or "not_priced" in value:
        return None, "等价格需要三情景估值；未定价时请改用等证据并写明缺哪项证据"
    try:
        calc = valuation.compute(value, table, market_date)
    except ValueError as exc:
        return None, f"等价格需要可测算的三情景估值：{exc}"
    price = calc["v2"]["support_below"]
    if calc["close"] <= price:
        return price, (f"现价 {calc['close']:g} 元已不高于期望回报门槛价 {price:.2f} 元，等价格不成立："
                       "请改为现在分批，或用等证据写明还缺哪项证据")
    return price, None


def problems(stance, table: list[dict], value, market_date: str) -> list[str]:
    """Structure only; the stance text and cites use checking's ordinary item checks."""
    if not isinstance(stance, dict):
        return ['stance 须为 {"action": "现在分批 | 等价格 | 等证据 | 回避", "text": "理由", "cites": [出处]}']
    out = []
    action = stance.get("action")
    if action not in ACTIONS:
        out.append(f"stance.action 须是 {'、'.join(ACTIONS)} 之一")
    allowed = {"action", "text", "cites"} | ({"trigger"} if action == "等证据" else set())
    if set(stance) != allowed:
        out.append("stance 只含 action、text、cites" + ("、trigger" if action == "等证据" else "")
                   + ("；等价格的价格由程序按估值计算，不要填写" if action == "等价格" else ""))
    if action == "等证据":
        out += trigger_problems(stance.get("trigger"), table)
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
