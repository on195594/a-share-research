"""Read-only historical replay diagnostics; no fabricated funding or execution timestamps."""
import math
from datetime import date
from decimal import Decimal

from a_share_research import db
from a_share_research.account import ledger
from a_share_research.account.mirror import MANIFEST


def read(conn, principal, holding_id: int) -> dict:
    # Imports stay behind the assistant-only tool boundary; research/web need not load policy modules.
    from a_share_research.account.lifecycle import Movement, replay
    from a_share_research.policy.v0.lifecycle_projection import project, current_stops
    with db.transaction(conn, immediate=False):
        ledger.authorize(conn, principal)
        columns = MANIFEST['holdings']
        row = conn.execute(f"SELECT {','.join(columns)} FROM holdings WHERE id=?", (holding_id,)).fetchone()
        if row is None:
            raise ledger.LedgerError('持仓周期不存在', 404)
        source = dict(zip(columns, row))
        columns = MANIFEST['holding_events']
        events = [dict(zip(columns, r)) for r in conn.execute(
            f"SELECT {','.join(columns)} FROM holding_events WHERE holding_id=? ORDER BY event_date,id", (holding_id,))]
    try:
        movements = []
        for event in events:
            if event['code'] != source['code']:
                raise ValueError(f"事件 {event['id']} 公司与持仓周期不一致")
            if type(event['inferred']) is not int or event['inferred'] not in (0, 1):
                raise ValueError(f"事件 {event['id']} 推断标记非法")
            day = event['event_date']
            if not isinstance(day, str) or len(day) != 10 or date.fromisoformat(day).isoformat() != day:
                raise ValueError(f"事件 {event['id']} 日期须为 YYYY-MM-DD")
            movements.append(Movement(id=event['id'], kind=event['event_type'], day=event['event_date'],
                shares=event['shares'] if event['shares'] is not None else 0,
                price=event['price'] if event['price'] is not None else 0,
                fees=event['fees'], tax=event['tax'], cash=event['cash_amount'] if event['cash_amount'] is not None else 0,
                inferred=bool(event['inferred'])))
        economic = replay(movements)
        policy = project(economic, {event['id']:event['notes'] for event in events})
    except ValueError as exc:
        return dict(status='incomplete', holding_id=holding_id, source=source, events=events,
                    problems=[str(exc)], production_changed=False)
    fields = {name:getattr(economic, name) for name in
              ('shares','cost_price','initial_shares','buy_date','exit_date','exit_price')}
    fields.update({name:policy[name] for name in ('reference_cost','main_entry_date','main_entry_basis','additions_since_main')})
    # Serialize monetary values explicitly; only diagnostic equality has a floating-point tolerance.
    normalized = {k: v.isoformat() if hasattr(v, 'isoformat') else str(v) if isinstance(v, Decimal) else v for k,v in fields.items()}
    def equal(a, b):
        if isinstance(a, (int,float,Decimal)) and isinstance(b, (int,float,Decimal)):
            return math.isclose(float(a),float(b),rel_tol=0,abs_tol=1e-8)
        return a == (b.isoformat() if hasattr(b, 'isoformat') else b)
    differences = [dict(field=k, stored=source[k], replayed=normalized[k]) for k,v in fields.items() if not equal(source[k],v)]
    realized = {step.movement.id:step.realized_pnl for step in economic.steps if step.movement.kind=='sell'}
    sale_differences = [dict(event_id=e['id'],stored=e['realized_pnl'],replayed=str(realized[e['id']]))
                       for e in events if e['id'] in realized and not equal(e['realized_pnl'],realized[e['id']])]
    stops = current_stops(source['framework'], policy['reference_cost'])
    stop_differences = [dict(field=k,stored=source[k],current_policy=v) for k,v in (stops or {}).items() if not equal(source[k],v)]
    corporate_cash_checks = []
    for adjustment in policy['adjustments']:
        if adjustment['kind'] == 'corporate_action':
            event = next(e for e in events if e['id'] == adjustment['event_id'])
            same_day = [dict(event_id=e['id'],cash=e['cash_amount']) for e in events
                        if e['event_type']=='dividend' and e['event_date']==event['event_date']]
            total = sum((Decimal(str(e['cash'])) for e in same_day), Decimal(0))
            corporate_cash_checks.append(dict(event_id=event['id'],implied_cash=str(adjustment['implied_cash']),
                same_day_dividends=same_day,difference=str(total-adjustment['implied_cash']),binding='not_recorded'))
    return dict(status='replayed', holding_id=holding_id, source=source, events=events,
                projection=source | normalized, derived_fields=list(fields), differences=differences,
                sale_differences=sale_differences, current_policy_stop_differences=stop_differences,
                corporate_cash_checks=corporate_cash_checks,
                historical_stop_source='saved_legacy_snapshot', historical_policy_identity='unrecorded',
                invested=str(economic.invested),sale_cash=str(economic.sale_cash),dividends=str(economic.dividends),
                realized_pnl=str(economic.realized_pnl),contains_inferred=economic.contains_inferred,
                time_precision='date; same-day order uses original event id',
                complete_account_cash_reconciliation=False, projection_scope='historical_lifecycle', production_changed=False)
