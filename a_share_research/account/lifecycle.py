"""Pure economic-cost replay for one explicitly identified holding lifecycle.

Dividends remain separate income. This module neither evaluates investment policy nor
infers an account cash opening; a successful replay is not full broker reconciliation.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from a_share_research.account.reconciliation import Money, Value

NonnegativeMoney = Annotated[Money, Field(ge=0)]
Quantity = Annotated[StrictInt, Field(ge=0)]


class Movement(Value):
    id: Annotated[StrictInt, Field(gt=0)]
    kind: Literal['buy', 'sell', 'dividend', 'adjustment']
    day: date
    shares: StrictInt = 0
    price: NonnegativeMoney = Decimal(0)
    fees: NonnegativeMoney = Decimal(0)
    tax: NonnegativeMoney = Decimal(0)
    cash: NonnegativeMoney = Decimal(0)
    inferred: StrictBool = False

    @model_validator(mode='after')
    def shape(self):
        if self.kind in ('buy', 'sell'):
            if self.shares <= 0 or self.price <= 0 or self.cash:
                raise ValueError('买卖须提供正股数和价格，现金净额由事件计算')
        elif self.kind == 'dividend':
            if self.shares or self.price or self.fees or self.tax:
                raise ValueError('分红仅接受非负现金金额')
        elif self.cash or self.fees or self.tax:
            raise ValueError('数量或政策元数据调整不携带现金流')
        return self


class Step(Value):
    movement: Movement
    before_shares: Quantity
    before_cost: NonnegativeMoney
    after_shares: Quantity
    after_cost: NonnegativeMoney
    realized_pnl: Money


class Replay(Value):
    shares: Quantity
    cost_price: NonnegativeMoney
    initial_shares: Quantity
    buy_date: date
    exit_date: date | None
    exit_price: NonnegativeMoney | None
    invested: NonnegativeMoney
    sale_cash: Money
    dividends: NonnegativeMoney
    realized_pnl: Money
    contains_inferred: StrictBool
    steps: list[Step]


def replay(movements: list[Movement]) -> Replay:
    """Replay ordered economic movements; reject missing opening trades or mixed lifecycles.

    IDs break ties on the same recorded day without inventing intraday timestamps.
    A zero-share adjustment may retain legacy policy metadata; its price does not
    change economic cost. Signed quantity adjustments conserve total book cost.
    """
    events = [Movement.model_validate(event) for event in movements]
    if not events:
        raise ValueError('生命周期缺少买入事件')
    if len({event.id for event in events}) != len(events):
        raise ValueError('生命周期事件编号重复')
    events.sort(key=lambda event: (event.day, event.id))
    if events[0].kind != 'buy':
        raise ValueError('生命周期必须从买入事件开始')
    # Fixed arithmetic semantics, independent of a caller's ambient Decimal context.
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        shares = initial_shares = 0
        cost = invested = sale_cash = dividends = realized_total = Decimal(0)
        exit_date = exit_price = None
        steps = []
        for event in events:
            before_shares, before_cost = shares, cost
            realized = Decimal(0)
            if event.kind == 'buy':
                if exit_date is not None:
                    raise ValueError('清仓后买入属于新生命周期')
                added = event.shares * event.price + event.fees + event.tax
                cost = (shares * cost + added) / (shares + event.shares)
                shares += event.shares
                invested += added
                if not steps:
                    initial_shares = event.shares
            elif event.kind == 'sell':
                if event.shares > shares:
                    raise ValueError('卖出股数超过生命周期持仓')
                net = event.shares * event.price - event.fees - event.tax
                realized = net - event.shares * cost
                realized_total += realized
                sale_cash += net
                shares -= event.shares
                if shares == 0:
                    exit_date, exit_price = event.day, event.price
            elif event.kind == 'dividend':
                dividends += event.cash
            elif event.shares:
                if shares <= 0 or shares + event.shares <= 0:
                    raise ValueError('送转前后持仓股数必须为正')
                new_shares = shares + event.shares
                cost = cost * shares / new_shares
                initial_shares = int((Decimal(initial_shares) * new_shares / shares).to_integral_value())
                shares = new_shares
            steps.append(Step(movement=event, before_shares=before_shares, before_cost=before_cost,
                              after_shares=shares, after_cost=cost, realized_pnl=realized))
        return Replay(shares=shares, cost_price=cost, initial_shares=initial_shares,
                      buy_date=events[0].day, exit_date=exit_date, exit_price=exit_price,
                      invested=invested, sale_cash=sale_cash, dividends=dividends,
                      realized_pnl=realized_total, contains_inferred=any(e.inferred for e in events), steps=steps)
