"""Pure economic positions from explicit opening costs and already reconciled journal events.

Seeded cycles expose cash flows only since the account opening. Policy identity is
preserved as provenance; reference costs, stop lines and policy actions are not inferred.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Annotated

from pydantic import Field, StrictInt, StringConstraints

from a_share_research.account.reconciliation import Code, Event, Money, Value

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveId = Annotated[StrictInt, Field(gt=0)]


class PositionSeed(Value):
    cycle_id: Text
    code: Code
    shares: PositiveId
    cost_price: Annotated[Money, Field(gt=0)]
    buy_date: date
    legacy_holding_id: PositiveId | None = None
    policy_identity: Text


class PositionEntry(Value):
    policy_identity: Text
    name: str | None = None


class MissingBasis(ValueError):
    """The caller must supply the policy reference for a newly opened cycle."""


class Cycle(Value):
    cycle_id: str
    code: Code
    shares: Annotated[StrictInt, Field(ge=0)]
    cost_price: Annotated[Money, Field(gt=0)]
    buy_date: date
    exit_date: date | None = None
    exit_price: Money | None = None
    legacy_holding_id: PositiveId | None = None
    policy_identity: Text
    name: str | None = None
    realized_pnl_since_opening: Money = Decimal(0)
    invested_since_opening: Money = Decimal(0)
    sale_cash_since_opening: Money = Decimal(0)
    dividends_since_opening: Money = Decimal(0)


def replay(seeds: list[PositionSeed], records: list[tuple[str, Event]],
           entries: dict[str, PositionEntry], *, dividend_cycles: dict[str, str] | None = None) -> dict:
    """Rebuild cycles deterministically, retaining input order for equal execution timestamps.

    Corrections are handled by the caller supplying only active journal records. Missing
    entries, duplicate identities, oversells and ambiguous dividends fail the whole replay.
    Explicit dividend bindings may target a closed cycle; multiple cycles for a company
    require a binding rather than assuming its currently active position earned the cash.
    The result is JSON-ready with decimal strings, never a lifetime cash-flow assertion.
    """
    seeds = [PositionSeed.model_validate(seed) for seed in seeds]
    records = [(key, Event.model_validate(event)) for key, event in records]
    entries = {key: PositionEntry.model_validate(entry) for key, entry in entries.items()}
    dividend_cycles = {} if dividend_cycles is None else dict(dividend_cycles)
    keys = [key for key, _ in records]
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise ValueError('事件业务身份须为非空字符串')
    if len(keys) != len(set(keys)):
        raise ValueError('事件业务身份重复')
    if set(entries) - set(keys):
        raise ValueError('开仓元数据引用了不存在的事件')
    kinds = {key:event.kind for key,event in records}
    if any(kinds.get(key) != 'dividend' for key in dividend_cycles):
        raise ValueError('分红归属只能绑定已存在的分红事件')
    if any(not isinstance(value, str) or not value.strip() for value in dividend_cycles.values()):
        raise ValueError('分红归属周期须为非空字符串')
    if len({seed.code for seed in seeds}) != len(seeds) or len({seed.cycle_id for seed in seeds}) != len(seeds):
        raise ValueError('期初代码或生命周期身份重复')
    legacy_ids = [seed.legacy_holding_id for seed in seeds if seed.legacy_holding_id is not None]
    if len(legacy_ids) != len(set(legacy_ids)):
        raise ValueError('期初旧持仓周期编号重复')
    cycles = [Cycle(**seed.model_dump()) for seed in seeds]
    active = {cycle.code:cycle for cycle in cycles}
    cycle_ids = {cycle.cycle_id for cycle in cycles}
    cycle_by_id = {cycle.cycle_id:cycle for cycle in cycles}
    projected_events = []
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        for key, event in sorted(records, key=lambda pair: pair[1].occurred_at):
            cycle = active.get(event.code)
            opening = event.kind == 'buy' and cycle is None
            if key in entries and not opening:
                raise ValueError('开仓元数据只能绑定真正新开仓买入')
            if event.code is None:
                continue
            if opening:
                if key not in entries:
                    raise MissingBasis(f'新开仓事件 {key} 缺少政策身份元数据')
                cycle_id = 'event:' + key
                if cycle_id in cycle_ids:
                    raise ValueError('新开仓生命周期身份与已有周期冲突')
                entry = entries[key]
                cycle = Cycle(cycle_id=cycle_id,code=event.code,shares=0,cost_price=event.price,
                              buy_date=event.occurred_at.date(),policy_identity=entry.policy_identity,name=entry.name)
                cycles.append(cycle)
                cycle_ids.add(cycle_id)
                cycle_by_id[cycle_id] = cycle
                active[event.code] = cycle
            if event.kind == 'dividend':
                if key in dividend_cycles:
                    cycle = cycle_by_id.get(dividend_cycles[key])
                    if cycle is None or cycle.code != event.code:
                        raise ValueError('分红归属周期不存在或公司不匹配')
                elif cycle is None or sum(c.code == event.code for c in cycles) != 1:
                    raise MissingBasis('分红缺少可唯一绑定的活动生命周期，须明确指定周期')
            if cycle is None:
                raise ValueError(f'{event.code} 没有可唯一绑定的活动生命周期')
            if event.occurred_at.date() < cycle.buy_date:
                raise ValueError('事件早于生命周期买入日期')
            before_shares, before_cost = cycle.shares, cycle.cost_price
            realized = Decimal(0)
            if event.kind == 'buy':
                added = event.shares * event.price + event.fees + event.tax
                cycle.cost_price = (cycle.shares * cycle.cost_price + added) / (cycle.shares + event.shares)
                cycle.shares += event.shares
                cycle.invested_since_opening += added
            elif event.kind == 'sell':
                if event.shares > cycle.shares:
                    raise ValueError('卖出超过生命周期持仓股数')
                net = event.shares * event.price - event.fees - event.tax
                realized = net - event.shares * cycle.cost_price
                cycle.sale_cash_since_opening += net
                cycle.realized_pnl_since_opening += realized
                cycle.shares -= event.shares
                if cycle.shares == 0:
                    cycle.exit_date, cycle.exit_price = event.occurred_at.date(), event.price
                    del active[event.code]
            elif event.kind == 'dividend':
                cycle.dividends_since_opening += event.cash
            elif event.kind == 'adjustment':
                new_shares = cycle.shares + event.shares
                if new_shares <= 0:
                    raise ValueError('送转后股数必须为正，不能借送转关闭周期')
                cycle.cost_price = cycle.cost_price * cycle.shares / new_shares
                cycle.shares = new_shares
            projected_events.append(dict(business_id=key,cycle_id=cycle.cycle_id,kind=event.kind,
                occurred_at=event.occurred_at.isoformat(),before_shares=before_shares,after_shares=cycle.shares,
                before_cost=str(before_cost),after_cost=str(cycle.cost_price),realized_pnl=str(realized)))
    return dict(positions={code:cycle.shares for code,cycle in sorted(active.items())},
                cycles=[cycle.model_dump(mode='json') for cycle in cycles],events=projected_events,
                cash_flow_scope='since_opening')
