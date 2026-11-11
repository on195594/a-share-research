"""Pure, database-independent calculations for a holding lifecycle.

The cache CLI owns SQLite reads and state transitions.  This module owns only
the cash-flow invariant, so every reporting command can use exactly the same
return calculation without importing the cache layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math


@dataclass(frozen=True)
class LifecycleReturn:
    """Cash-flow return for one holding lifecycle."""

    invested: float
    sale_cash: float
    dividends: float
    market_value: float
    pnl: float
    total_return_pct: float
    holding_days: int
    contains_inferred: bool


def _finite_event_amount(value: object, field: str) -> float:
    """Read one persisted monetary field without silently accepting NaN/Inf."""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"账本字段 {field} 非法")
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"账本字段 {field} 非法") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"账本字段 {field} 非有限数值")
    return parsed


def calculate_lifecycle_return(
    events: list[tuple],
    remaining_shares: int,
    *,
    end_date: str,
    current_price: float | None = None,
) -> LifecycleReturn:
    """Calculate one lifecycle's return from immutable cash-flow events.

    Event rows use the query order ``event_type, event_date, shares, price,
    fees, tax, cash_amount, inferred``.  Corporate-action adjustment rows do
    not carry cash flow and are intentionally ignored here; their share effect
    is already represented by ``remaining_shares`` and later sell events.
    """
    if not events:
        raise ValueError("当前持仓生命周期无交易事件")
    if (
        isinstance(remaining_shares, bool)
        or not isinstance(remaining_shares, int)
        or remaining_shares < 0
    ):
        raise ValueError("当前持仓剩余股数非法")

    dates: list[date] = []
    invested = sale_cash = dividends = 0.0
    contains_inferred = False
    for (
        event_type,
        event_date,
        shares,
        price,
        fees,
        tax,
        cash_amount,
        inferred,
    ) in events:
        try:
            dates.append(date.fromisoformat(event_date))
        except (TypeError, ValueError) as exc:
            raise ValueError("账本事件日期非法") from exc
        contains_inferred = contains_inferred or bool(inferred)
        if event_type in ("buy", "sell"):
            quantity = _finite_event_amount(shares, "shares")
            unit_price = _finite_event_amount(price, "price")
            if quantity <= 0 or quantity != int(quantity) or unit_price <= 0:
                raise ValueError("账本买卖事件缺少有效股数或价格")
            cash = quantity * unit_price
            fees_value = _finite_event_amount(fees, "fees")
            tax_value = _finite_event_amount(tax, "tax")
            if fees_value < 0 or tax_value < 0:
                raise ValueError("账本费用或税费不能为负数")
            if event_type == "buy":
                invested += cash + fees_value + tax_value
            else:
                sale_cash += cash - fees_value - tax_value
        elif event_type == "dividend":
            dividend = _finite_event_amount(cash_amount, "cash_amount")
            if dividend < 0:
                raise ValueError("账本分红不能为负数")
            dividends += dividend

    if invested <= 0:
        raise ValueError("账本没有有效买入现金流")
    try:
        first_day = min(dates)
        last_day = date.fromisoformat(end_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("持仓结束日期非法") from exc
    if last_day < first_day:
        raise ValueError("持仓结束日期早于首笔账本事件")

    market_value = 0.0
    if remaining_shares:
        if current_price is None:
            raise ValueError("仍有持仓但无法取得有效当前价")
        price_value = _finite_event_amount(current_price, "current_price")
        if price_value <= 0:
            raise ValueError("仍有持仓但无法取得有效当前价")
        market_value = remaining_shares * price_value
    pnl = sale_cash + dividends + market_value - invested
    return LifecycleReturn(
        invested=invested,
        sale_cash=sale_cash,
        dividends=dividends,
        market_value=market_value,
        pnl=pnl,
        total_return_pct=pnl / invested * 100,
        holding_days=(last_day - first_day).days,
        contains_inferred=contains_inferred,
    )


def ledger_cash_flows(events: list[tuple]) -> list[tuple[str, float]]:
    """Investor cash flows (buys negative) from all lifecycles' ledger events.

    Rows use ``event_type, event_date, shares, price, fees, tax, cash_amount``.
    """
    flows = []
    for event_type, event_date, shares, price, fees, tax, cash_amount, *_ in events:
        costs = _finite_event_amount(fees, "fees") + _finite_event_amount(tax, "tax")
        if event_type == "buy":
            flows.append((event_date, -(shares * price + costs)))
        elif event_type == "sell":
            flows.append((event_date, shares * price - costs))
        elif event_type == "dividend":
            flows.append((event_date, _finite_event_amount(cash_amount, "cash_amount")))
    return flows


def public_market_equivalent(
    flows: list[tuple[str, float]], closes: dict[str, float]
) -> float:
    """Net P&L had every ledger cash flow bought or sold the index instead.

    Each flow trades at the first index close on or after its date; the mirror
    is valued at the last close supplied, which must be the valuation date.
    """
    days = sorted(closes)
    units = 0.0
    for flow_date, flow in flows:
        trade_day = next((day for day in days if day >= flow_date), None)
        if trade_day is None:
            raise ValueError(f"指数在 {flow_date} 之后没有收盘价")
        units -= flow / closes[trade_day]
    return sum(flow for _, flow in flows) + units * closes[days[-1]]
