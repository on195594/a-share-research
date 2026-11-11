"""Replay v0 reference-cost/main-entry rules without changing saved historical stop lines."""
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import re

from a_share_research.account.lifecycle import Replay
from a_share_research.account.reconciliation import money
from a_share_research.policy.v0.domain import get_stop_loss_pct

_NUMBER = r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
_CORPORATE = re.compile(rf'dps=({_NUMBER});split_ratio=({_NUMBER})')
_FRAMEWORK = re.compile(r'framework:([^\n]+)->([^\n]+)')


def project(replayed: Replay, notes: dict[int, str | None]) -> dict:
    """Use only recorded adjustment metadata. Unknown adjustments cannot silently become no-ops."""
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        return _project(replayed, notes)


def _project(replayed, notes):
    reference = basis = additions = Decimal(0)
    main_date = None
    adjustments = []
    for step in replayed.steps:
        event = step.movement
        if event.kind == 'buy':
            added = event.price * event.shares + event.fees + event.tax
            if step.before_shares == 0:
                reference, basis, main_date = event.price, added, event.day
            else:
                reference = (reference * step.before_shares + event.price * event.shares) / step.after_shares
                additions += added
                if additions > basis * Decimal('0.5'):
                    main_date, basis, additions = event.day, step.before_cost * step.before_shares + added, Decimal(0)
        elif event.kind == 'adjustment':
            raw = notes.get(event.id) or ''
            corporate = _CORPORATE.fullmatch(raw)
            framework = _FRAMEWORK.fullmatch(raw)
            if corporate:
                dps, ratio = map(money, corporate.groups())
                if Decimal(step.before_shares) * (1 + ratio) != step.after_shares:
                    raise ValueError(f'事件 {event.id} 送转元数据与股数不一致')
                reference = (reference - dps) / (1 + ratio)
                if reference <= 0:
                    raise ValueError(f'事件 {event.id} 调整后参考成本非正')
                if abs(reference - event.price) > Decimal('0.00000001'):
                    raise ValueError(f'事件 {event.id} 参考成本与记录不一致')
                adjustments.append(dict(event_id=event.id, kind='corporate_action', dps=dps, split_ratio=ratio, implied_cash=dps * step.before_shares))
            elif framework and event.shares == 0:
                adjustments.append(dict(event_id=event.id, kind='framework', before=framework[1], after=framework[2]))
            else:
                raise ValueError(f'事件 {event.id} 缺少可解释的调整元数据')
    return dict(reference_cost=reference, main_entry_date=main_date, main_entry_basis=basis,
                additions_since_main=additions, adjustments=adjustments)


def current_stops(framework: str | None, reference: Decimal) -> dict | None:
    """Diagnostic under today's v0 parameters; this is not the identity of a historical policy."""
    if not framework:
        return None
    first, second = get_stop_loss_pct(framework)
    return dict(stop_loss_15=round(float(reference) * first, 3), stop_loss_20=round(float(reference) * second, 3))
