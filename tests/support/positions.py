"""Structural positions, account books and projection inputs."""

from datetime import datetime
from decimal import Decimal

from a_share_research import api_keys, db
from a_share_research.account import evidence, ledger, position_store, positions
from a_share_research.account.mirror import MANIFEST
from a_share_research.account.reconciliation import Event, Opening, Snapshot


def insert(conn, table, row):
    columns = MANIFEST[table]
    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                 [row[column] for column in columns])


def policy_account_book(tmp_path):
    for fixture in cost_book(tmp_path):
        conn, principal, directory, sha = fixture
        insert(conn, 'holdings', opening())
        closed = opening() | dict(id=2, code='600002', name='Structural closed', shares=0,
            buy_date='2026-09-01', exit_date='2026-09-30', exit_price=11)
        insert(conn, 'holdings', closed)
        for identity, holding, kind, day, price in (
            (10, 2, 'buy', '2026-09-01', 10), (11, 2, 'sell', '2026-09-30', 11),
            (20, 1, 'buy', '2026-10-01', 10)):
            historical = dict.fromkeys(MANIFEST['holding_events'])
            historical.update(id=identity, holding_id=holding, code=f'60000{holding}',
                event_type=kind, event_date=day, shares=100, price=price, fees=0, tax=0,
                inferred=0, created_at=day, notes='Structural historical evidence',
                realized_pnl=100 if kind == 'sell' else None)
            insert(conn, 'holding_events', historical)
        position_store.initialize(conn, principal, 'a', [seed()], directory, [sha])
        yield fixture


def opening():
    row = dict.fromkeys(MANIFEST['holdings'])
    row.update(id=1,code='600001',name='Structural',shares=100,cost_price=10,buy_date='2026-10-01',
        framework='C资源',framework_confident=1,initial_shares=100,reference_cost=10,
        main_entry_date='2026-10-01',main_entry_basis=1000,additions_since_main=0,
        stop_loss_15=8.5,stop_loss_20=8,buy_score=50,notes='Structural original note',updated_at='2026-10-01')
    return row


def cost_book(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'structural-cost', role='assistant'))
    directory = tmp_path / 'evidence'
    sha = evidence.store(directory, b'Structural opening evidence; no investment or broker assertion')
    ledger.initialize(conn, principal, 'a', Opening(positions={'600001':100},cash='10000'),
                      datetime.fromisoformat('2026-11-01T00:00:00+08:00'),directory,sha,tolerance=Decimal(0))
    yield conn, principal, directory, sha
    conn.close()


def seed(**overrides):
    return positions.PositionSeed.model_validate(dict(cycle_id='legacy:1',code='600001',shares=100,
        cost_price='10',buy_date='2026-10-01',legacy_holding_id=1,policy_identity='structural-policy-v0') | overrides)


def initialize(book, seeds=None, hashes=None):
    return position_store.initialize(book[0],book[1],'a',[seed()] if seeds is None else seeds,
                                     book[2],[book[3]] if hashes is None else hashes)


def event(kind='buy', **overrides):
    return Event.model_validate(dict(kind=kind,code='600001',shares=100,price='12',broker_id='structural-1',
        occurred_at='2026-11-02T10:00:00+08:00') | overrides)


def batch(book, events, *, version=0, shares=200, cash='8800', entries=None, voids=()):
    holding = {'600001':shares} if shares else {}
    values = {'600001':Decimal(shares)*12} if shares else {}
    return ledger.Batch(expected_version=version,events=events,void_event_ids=list(voids),
        evidence_hashes=[book[3]],position_entries=entries or {},
        snapshot=Snapshot(positions=holding,cash=cash,market_values=values,total_assets=Decimal(cash)+sum(values.values())))


def submit(book, value):
    return ledger.submit(book[0],book[1],'a',value,book[2])


def state(book):
    return position_store.state(book[0],book[1],'a')
