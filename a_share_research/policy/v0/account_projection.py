"""Pure v0 policy projection of evidenced journal cycles into the legacy table shapes."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import math

from a_share_research.account.mirror import MANIFEST
from a_share_research.account.positions import PositionEntry
from a_share_research.account.reconciliation import Event, money


def _amount(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('旧表金额超出有限 REAL 范围')
    return result


def _mapped(mapping, key, label):
    value = mapping.get(key)
    if type(value) is not int or value <= 0:
        raise ValueError(f'{label} 缺少有效稳定编号：{key}')
    return value


def _parameters(profiles, identity, framework):
    profile = profiles.get(identity)
    if not isinstance(profile, dict):
        raise ValueError(f'缺少已绑定政策：{identity}')
    pair = profile.get('stops', {}).get(framework)
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise ValueError(f'政策 {identity} 未定义框架 {framework} 的止损参数')
    first, second = map(money, pair)
    ratio = money(profile.get('main_entry_ratio'))
    if not 0 < second <= first <= 1 or ratio <= 0:
        raise ValueError('政策参数非法')
    return first, second, ratio


def project(openings: dict[str, dict], economic: dict, records: list[dict],
            entries: dict[str, PositionEntry], profiles: dict[str, dict], default_profile: str,
            cycle_ids: dict[str, int], event_ids: dict[str, int],
            distributions: dict[str, Decimal] | None = None) -> dict:
    """Preserve opening history, applying only explicitly bound v0 rules and distribution facts.

    Economic steps own quantities and cost basis. Cash distributions alone do not imply
    ex-dividend policy adjustments. The caller binds profiles and immutable integer IDs.
    """
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        return _project(openings, economic, records, entries, profiles, default_profile,
                        cycle_ids, event_ids, distributions or {})


def _project(openings, economic, records, entries, profiles, default_profile, cycle_ids, event_ids, distributions):
    if default_profile not in profiles:
        raise ValueError('缺少期初绑定的 v0 政策')
    states = deepcopy(openings)
    selected = {cycle:default_profile for cycle in states}
    steps = {step['business_id']:step for step in economic['events']}
    if len(steps) != len(economic['events']):
        raise ValueError('经济投影事件重复')
    parsed = [(record['business_id'], Event.model_validate(record['event']), record['created_at']) for record in records]
    if len({key for key, _, _ in parsed}) != len(parsed):
        raise ValueError('输入业务身份重复')
    stock_keys = {key for key, event, _ in parsed if event.code is not None}
    if set(steps) != stock_keys or set(distributions) - stock_keys:
        raise ValueError('经济事件或除权元数据范围不一致')
    for cycle, row in states.items():
        if set(row) != set(MANIFEST['holdings']) or row['id'] != _mapped(cycle_ids, cycle, '周期'):
            raise ValueError('期初持仓形状或编号不一致')
    output_events = []
    for key, event, created_at in sorted(parsed, key=lambda item:item[1].occurred_at):
        if event.code is None:
            continue
        step = steps[key]
        cycle = step['cycle_id']
        if step['kind'] != event.kind:
            raise ValueError('经济步骤与事件类型不一致')
        before, after = step['before_shares'], step['after_shares']
        if cycle not in states:
            entry = entries.get(key)
            if event.kind != 'buy' or before != 0 or entry is None:
                raise ValueError('新周期缺少开仓上下文')
            framework = getattr(entry, 'framework', None)
            if not framework:
                raise ValueError('新开仓须明确框架，不能推断历史决策')
            _parameters(profiles, entry.policy_identity, framework)
            selected[cycle] = entry.policy_identity
            states[cycle] = dict.fromkeys(MANIFEST['holdings'])
            states[cycle].update(id=_mapped(cycle_ids,cycle,'周期'),code=event.code,name=entry.name,
                framework=framework,framework_confident=int(getattr(entry,'framework_confident',False)),
                initial_shares=event.shares,buy_date=event.occurred_at.date().isoformat(),
                additions_since_main=0,shares=0)
        row = states[cycle]
        if row['code'] != event.code or row['shares'] != before:
            raise ValueError('周期公司或步骤前股数不一致')
        reference_changed = event.kind in ('buy','adjustment') or key in distributions
        parameters = _parameters(profiles,selected[cycle],row['framework']) if reference_changed else None
        reference = money(row['reference_cost']) if row['reference_cost'] is not None else None
        notes = None
        if event.kind == 'buy':
            added = event.shares * event.price + event.fees + event.tax
            if before == 0:
                reference = event.price
                row.update(main_entry_date=event.occurred_at.date().isoformat(),main_entry_basis=_amount(added),additions_since_main=0)
            else:
                if reference is None or row['main_entry_basis'] is None or row['additions_since_main'] is None:
                    raise ValueError('已有周期缺少明确参考成本或主仓位期初')
                reference = (reference * before + event.price * event.shares) / after
                additions = money(row['additions_since_main']) + added
                basis = money(row['main_entry_basis'])
                if additions > basis * parameters[2]:
                    row['main_entry_date'] = event.occurred_at.date().isoformat()
                    basis, additions = money(step['before_cost']) * before + added, Decimal(0)
                row.update(main_entry_basis=_amount(basis),additions_since_main=_amount(additions))
        if key in distributions:
            if event.kind not in ('dividend','adjustment') or before <= 0 or reference is None:
                raise ValueError('每股现金除权只允许有明确在仓基准的分红或送转')
            dps = money(distributions[key])
            if dps < 0:
                raise ValueError('每股现金除权不能为负')
            reference -= dps
            notes = f'dps={dps};split_ratio=0'
        else:
            dps = Decimal(0)
        if event.kind == 'adjustment':
            if before <= 0 or after <= 0 or reference is None or row['initial_shares'] is None:
                raise ValueError('送转缺少明确参考成本或股数期初')
            multiplier = Decimal(after) / before
            reference /= multiplier
            row['initial_shares'] = int((Decimal(row['initial_shares']) * multiplier).to_integral_value())
            notes = f'dps={dps};split_ratio={multiplier - 1}'
        if reference_changed:
            if reference is None or reference <= 0:
                raise ValueError('政策参考成本必须为正')
            row['reference_cost'] = _amount(reference)
            row['stop_loss_15'] = round(_amount(reference) * _amount(parameters[0]), 3)
            row['stop_loss_20'] = round(_amount(reference) * _amount(parameters[1]), 3)
        row.update(shares=after,cost_price=_amount(money(step['after_cost'])),updated_at=created_at)
        if event.kind == 'sell' and after == 0:
            row.update(exit_date=event.occurred_at.date().isoformat(),exit_price=_amount(event.price))
        recorded = dict.fromkeys(MANIFEST['holding_events'])
        recorded.update(id=_mapped(event_ids,key,'事件'),holding_id=row['id'],code=event.code,event_type=event.kind,
            event_date=event.occurred_at.date().isoformat(),shares=event.shares if event.kind != 'dividend' else None,
            price=_amount(reference) if event.kind=='adjustment' else _amount(event.price) if event.kind in ('buy','sell') else None,
            fees=_amount(event.fees),tax=_amount(event.tax),cash_amount=_amount(event.cash) if event.kind=='dividend' else None,
            realized_pnl=_amount(money(step['realized_pnl'])) if event.kind=='sell' else None,
            notes=notes,inferred=0,created_at=created_at,report_id=event.report_id)
        output_events.append(recorded)
    final = {cycle['cycle_id']:cycle for cycle in economic['cycles']}
    if set(final) != set(states):
        raise ValueError('经济与政策生命周期集合不一致')
    for cycle, row in states.items():
        expected = final[cycle]
        if row['shares'] != expected['shares'] or money(row['cost_price']) != money(expected['cost_price']):
            # REAL legacy outputs may round a Decimal recurring fraction; compare at their output precision.
            if row['shares'] != expected['shares'] or row['cost_price'] != _amount(money(expected['cost_price'])):
                raise ValueError('经济与政策最终成本或股数不一致')
    if len({row['id'] for row in states.values()}) != len(states) or len({row['id'] for row in output_events}) != len(output_events):
        raise ValueError('投影整数编号重复')
    return dict(holdings=sorted(states.values(),key=lambda r:r['id']),holding_events=output_events)
