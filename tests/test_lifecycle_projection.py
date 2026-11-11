"""Structural lifecycle examples only; no broker records or investment assertions."""
import sqlite3
from decimal import Decimal

import pytest

from a_share_research import api_keys, db
from a_share_research.account import ledger
from a_share_research.account.lifecycle import Movement, replay
from a_share_research.policy.v0.lifecycle_projection import project
from a_share_research.tools import lifecycle


def movement(identity, kind='buy', day='2026-01-01', **values):
    return Movement(id=identity, kind=kind, day=day, **values)


def test_main_entry_requires_strictly_more_than_half_basis():
    first = movement(1, shares=100, price='10')
    half = movement(2, day='2026-01-02', shares=50, price='10')
    at_boundary = project(replay([first, half]), {})
    assert at_boundary['main_entry_date'].isoformat() == '2026-01-01'
    assert at_boundary['main_entry_basis'] == Decimal('1000')
    assert at_boundary['additions_since_main'] == Decimal('500')
    crossed = project(replay([first, half, movement(3, day='2026-01-03', shares=1, price='10')]), {})
    assert crossed['main_entry_date'].isoformat() == '2026-01-03'
    assert crossed['main_entry_basis'] == Decimal('1510')
    assert crossed['additions_since_main'] == 0


def test_reference_cost_excludes_buy_fees_and_cash_dividends():
    events = [movement(1, shares=100, price='10', fees='10'),
              movement(2, day='2026-01-02', shares=100, price='12', fees='20'),
              movement(3, 'dividend', '2026-01-03', cash='40')]
    economic = replay(events)
    policy = project(economic, {})
    assert economic.cost_price == Decimal('11.15')
    assert economic.dividends == Decimal('40')
    assert policy['reference_cost'] == Decimal('11')


def test_corporate_metadata_matches_quantity_and_recorded_reference():
    events = [movement(1, shares=100, price='10'),
              movement(2, 'dividend', '2026-01-02', cash='20'),
              movement(3, 'adjustment', '2026-01-02', shares=100, price='4.9')]
    economic = replay(events)
    policy = project(economic, {3:'dps=0.2;split_ratio=1.0'})
    assert economic.shares == economic.initial_shares == 200
    assert economic.cost_price == Decimal('5')
    assert policy['reference_cost'] == Decimal('4.9')
    assert policy['adjustments'][0]['implied_cash'] == Decimal('20')
    with pytest.raises(ValueError, match='股数不一致'):
        project(economic, {3:'dps=0.2;split_ratio=0.0'})
    with pytest.raises(ValueError, match='参考成本与记录不一致'):
        project(economic, {3:'dps=0.3;split_ratio=1.0'})


@pytest.mark.parametrize('note', [None, 'unclassified structural adjustment',
    ' dps=0.2;split_ratio=0.0', 'dps=0.2;split_ratio=0.0;extra=1',
    'dps=NaN;split_ratio=0.0', 'dps=-0.2;split_ratio=0.0'])
def test_unknown_or_noncanonical_adjustment_metadata_is_rejected(note):
    economic = replay([movement(1, shares=100, price='10'),
                       movement(2, 'adjustment', '2026-01-02', price='9.8')])
    with pytest.raises(ValueError, match='调整元数据'):
        project(economic, {2:note})


@pytest.fixture
def account(tmp_path):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'structural-lifecycle', role='assistant'))
    conn.execute("""INSERT INTO holdings
        (id,code,name,cost_price,shares,buy_date,initial_shares,main_entry_date,main_entry_basis,
         additions_since_main,reference_cost,framework,framework_confident,stop_loss_15,stop_loss_20)
        VALUES (1,'600001','Structural fixture',10,100,'2026-01-01',100,'2026-01-01',1000,0,10,'C资源',1,8.5,8)""")
    add_event(conn, 1, 'buy', shares=100, price=10, inferred=1)
    yield conn, principal, path
    conn.close()


def add_event(conn, identity, kind, day='2026-01-01', **values):
    fields = dict(id=identity, holding_id=1, code='600001', event_type=kind,
                  event_date=day, created_at=day, **values)
    conn.execute(f"INSERT INTO holding_events ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(fields.values()))


def stored(conn):
    return {table:[tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('holdings','holding_events')}


def test_read_preserves_historical_stops_and_exposes_current_policy_difference(account):
    conn, principal, path = account
    before = stored(conn)
    ro = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, isolation_level=None)
    ro.row_factory = sqlite3.Row
    try:
        result = lifecycle.read(ro, principal, 1)
        assert not ro.in_transaction
    finally:
        ro.close()
    assert result['status'] == 'replayed'
    assert result['projection']['stop_loss_15'] == 8.5
    assert result['projection']['stop_loss_20'] == 8
    assert result['historical_stop_source'] == 'saved_legacy_snapshot'
    assert result['historical_policy_identity'] == 'unrecorded'
    assert {d['field'] for d in result['current_policy_stop_differences']} == {'stop_loss_15','stop_loss_20'}
    assert result['contains_inferred'] is True
    assert result['events'][0]['inferred'] == 1
    assert result['events'][0]['event_date'] == '2026-01-01'
    assert result['time_precision'] == 'date; same-day order uses original event id'
    assert result['production_changed'] is result['complete_account_cash_reconciliation'] is False
    assert stored(conn) == before


def test_closed_legacy_residual_shares_reported_without_repair(account):
    conn, principal, _ = account
    add_event(conn, 2, 'sell', day='2026-01-02', shares=100, price=11, fees=2, tax=1, realized_pnl=97)
    conn.execute("UPDATE holdings SET exit_date='2026-01-02',exit_price=11 WHERE id=1")
    before = stored(conn)
    result = lifecycle.read(conn, principal, 1)
    assert result['projection']['shares'] == 0 and result['source']['shares'] == 100
    assert {'field':'shares','stored':100,'replayed':0} in result['differences']
    assert result['sale_differences'] == []
    assert Decimal(result['realized_pnl']) == 97
    assert stored(conn) == before


def test_same_day_dividend_records_remain_separate_and_cash_gap_is_diagnostic(account):
    conn, principal, _ = account
    add_event(conn, 2, 'dividend', day='2026-01-02', cash_amount=12)
    add_event(conn, 3, 'dividend', day='2026-01-02', cash_amount=13)
    add_event(conn, 4, 'adjustment', day='2026-01-02', shares=0, price=9.8, notes='dps=0.2;split_ratio=0.0')
    before = stored(conn)
    result = lifecycle.read(conn, principal, 1)
    check = result['corporate_cash_checks'][0]
    assert check['same_day_dividends'] == [{'event_id':2,'cash':12.0},{'event_id':3,'cash':13.0}]
    assert Decimal(check['implied_cash']) == 20 and Decimal(check['difference']) == 5
    assert check['binding'] == 'not_recorded'
    assert Decimal(result['dividends']) == 25
    assert len(result['events']) == 4
    assert stored(conn) == before


@pytest.mark.parametrize('role',['research','owner'])
def test_non_assistant_cannot_read_lifecycle(account, role):
    conn, _, _ = account
    principal = api_keys.authenticate(conn, api_keys.create(conn, role, role=role))
    before = stored(conn)
    with pytest.raises(ledger.LedgerError) as caught:
        lifecycle.read(conn, principal, 1)
    assert caught.value.status == 403 and not conn.in_transaction
    assert stored(conn) == before


def test_unexplained_adjustment_returns_incomplete_with_original_evidence(account):
    conn, principal, _ = account
    add_event(conn, 2, 'adjustment', notes='Unclassified structural record')
    before = stored(conn)
    result = lifecycle.read(conn, principal, 1)
    assert result['status'] == 'incomplete'
    assert result['events'][1]['notes'] == 'Unclassified structural record'
    assert '调整元数据' in result['problems'][0]
    assert stored(conn) == before


def test_captured_decimal_exponent_adjustment_is_accepted():
    economic = replay([movement(1, shares=100, price='10'),
                       movement(2, 'adjustment', '2026-01-02', price='9.99999')])
    assert project(economic, {2:'dps=1e-05;split_ratio=0.0'})['reference_cost'] == Decimal('9.99999')


@pytest.mark.parametrize('column,value', [('inferred',2),('event_date','20260101')])
def test_malformed_legacy_boundary_returns_incomplete(account,column,value):
    conn, principal, _ = account
    conn.execute(f'UPDATE holding_events SET {column}=? WHERE id=1',(value,))
    result = lifecycle.read(conn,principal,1)
    assert result['status'] == 'incomplete'
