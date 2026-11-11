"""Structural account examples reused from test_account, not broker evidence or return evaluation."""
from decimal import Decimal

import pytest
from pydantic import ValidationError
from a_share_research.account.reconciliation import Event, Opening, Snapshot, identity, project, reconcile


def event(**changes):
    return Event.model_validate(dict(kind='buy',code='600519',occurred_at='2026-10-08T15:30:00+08:00',
                                    shares=100,price='1400.5') | changes)


def test_quantity_cash_and_marked_sell_dividend_adjustment_variants():
    opening = Opening(positions={}, cash='200000')
    events = [event(), event(kind='dividend',shares=0,price=0,cash='2500',occurred_at='2026-10-20T09:00:00+08:00'),
              event(kind='adjustment',shares=100,price=0,occurred_at='2026-10-21T09:00:00+08:00'),
              event(kind='sell',shares=50,price='700',fees='5',tax='17.5',occurred_at='2026-10-22T10:00:00+08:00')]
    result = project(opening, list(reversed(events)))
    assert result.positions == {'600519':150} and result.cash == Decimal('97427.5')
    snapshot = Snapshot(positions=result.positions,cash=result.cash,market_values={'600519':'105000'},total_assets='202427.5')
    assert reconcile(result,snapshot,Decimal('0')) == []
    bad = snapshot.model_copy(update={'cash':Decimal('97427.6')})
    assert {d['field'] for d in reconcile(result,bad,Decimal('0'))} == {'cash','total_assets'}
    assert reconcile(result,bad,Decimal('0.1')) == []


def test_business_identity_ignores_decimal_spelling_and_normalizes_time():
    assert identity(event(),'a') == identity(event(price='1400.500',occurred_at='2026-10-08T07:30:00Z'),'a')
    assert identity(event(),'a') != identity(event(),'b')
    assert identity(event(broker_id='123'),'a') == identity(event(broker_id='123',price='1401'),'a')


@pytest.mark.parametrize('change', [{'price':True},{'price':'NaN'},{'fees':'Infinity'},{'shares':True},
                                   {'shares':1.5},{'occurred_at':'2026-10-08'},{'kind':'deposit'},
                                   {'kind':'adjustment','price':1},{'cash':'100'}])
def test_invalid_boundary_rejected(change):
    with pytest.raises(ValidationError):event(**change)


def test_negative_states_and_missing_value_rejected():
    with pytest.raises(ValueError,match='负'):
        project(Opening(positions={},cash='1'),[event()])
    with pytest.raises(ValueError,match='负'):
        project(Opening(positions={},cash='200000'),[event(kind='sell')])
    with pytest.raises(ValidationError,match='市值'):
        Snapshot(positions={'600519':100},cash=0,market_values={},total_assets=0)
    with pytest.raises(ValueError,match='容差'):
        reconcile(Opening(positions={},cash=0),Snapshot(positions={},cash=0,market_values={},total_assets=0),Decimal('-1'))


def test_cash_only_events_are_explicit_positive_amounts():
    events=[Event(kind='deposit',occurred_at='2026-10-08T10:00:00',cash='100'),
            Event(kind='withdrawal',occurred_at='2026-10-08T10:01:00',cash='20'),
            Event(kind='fee',occurred_at='2026-10-08T10:02:00',cash='1.5')]
    assert project(Opening(positions={},cash=0),events).cash == Decimal('78.5')
