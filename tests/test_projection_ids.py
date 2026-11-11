"""Structural identity fixtures only; no broker events or investment judgments."""
import json
import sqlite3

import pytest

from a_share_research import db
from a_share_research.account import projection_ids
from test_position_store import book as _book_fixture, initialize, seed, event, batch, submit, state

book = _book_fixture


@pytest.fixture
def mapped(book):
    with db.transaction(book[0]):
        projection_ids.migrate(book[0])
    return book


def ids(book, kind):
    return projection_ids.lookup(book[0], 'a', kind)


def test_seed_reservation_and_readonly_lookup(mapped):
    initialize(mapped)
    assert ids(mapped,'cycle') == {'legacy:1':1}
    assert ids(mapped,'event') == {}
    path=mapped[0].execute('PRAGMA database_list').fetchone()[2]
    ro=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    try:
        assert projection_ids.lookup(ro,'a','cycle') == {'legacy:1':1}
        assert projection_ids.lookup(ro,'other','cycle') == {}
    finally:
        ro.close()
    for sql in ('UPDATE account_projection_ids SET legacy_id=legacy_id','DELETE FROM account_projection_ids'):
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            mapped[0].execute(sql)


def test_new_ids_respect_legacy_autoincrement_high_watermarks(mapped):
    conn=mapped[0]
    conn.execute("INSERT INTO holdings(id,code,name) VALUES (50,'600002','Structural previous holding')")
    conn.execute("INSERT INTO holding_events(id,holding_id,code,event_type,event_date,created_at) VALUES (80,50,'600002','adjustment','2026-10-01','2026-10-01')")
    conn.execute("UPDATE sqlite_sequence SET seq=500 WHERE name='holdings'")
    conn.execute("UPDATE sqlite_sequence SET seq=800 WHERE name='holding_events'")
    initialize(mapped)
    assert submit(mapped,batch(mapped,[event('sell',broker_id='close')],shares=0,cash='11200'))['status']=='posted'
    new=event(broker_id='new-cycle',occurred_at='2026-11-03T10:00:00')
    assert submit(mapped,batch(mapped,[new],version=1,shares=100,cash='10000',entries={'0':{'policy_identity':'structural-v0'}}))['status']=='posted'
    assert sorted(ids(mapped,'cycle').values()) == [1,501]
    assert sorted(ids(mapped,'event').values()) == [801,802]


def test_explicit_legacy_ids_reserve_before_unassigned_cycles(mapped):
    conn=mapped[0]
    initialize(mapped)
    projection=state(mapped)['projection']
    projection['cycles']=[dict(cycle_id='new',legacy_holding_id=None),dict(cycle_id='legacy:2',legacy_holding_id=2)]
    with db.transaction(conn):
        projection_ids.sync(conn,'a',projection)
    assert ids(mapped,'cycle') == {'legacy:1':1,'legacy:2':2,'new':3}
    before=ids(mapped,'cycle')
    with pytest.raises(ValueError,match='其他实体'):
        with db.transaction(conn):
            projection_ids.sync(conn,'a',{'cycles':[dict(cycle_id='conflict',legacy_holding_id=2)]})
    assert ids(mapped,'cycle')==before
    with pytest.raises(ValueError,match='重新绑定'):
        with db.transaction(conn):
            projection_ids.sync(conn,'a',{'cycles':[dict(cycle_id='legacy:1',legacy_holding_id=99)]})


def test_void_same_business_correction_and_retry_keep_event_id(mapped):
    initialize(mapped)
    value=batch(mapped,[event()])
    posted=submit(mapped,value)
    first=ids(mapped,'event')
    assert submit(mapped,value)==posted and ids(mapped,'event')==first
    journal_id=mapped[0].execute('SELECT id FROM account_journal').fetchone()[0]
    correction=batch(mapped,[event(price='14')],version=1,cash='8600',voids=[journal_id])
    assert submit(mapped,correction)['status']=='posted'
    assert ids(mapped,'event')==first
    assert mapped[0].execute('SELECT count(*) FROM account_journal').fetchone()[0]==2
    assert ids(mapped,'cycle')=={'legacy:1':1}


def test_void_retains_original_identity_and_new_business_gets_new_id(mapped):
    initialize(mapped)
    submit(mapped,batch(mapped,[event()]))
    old=ids(mapped,'event')
    journal_id=mapped[0].execute('SELECT id FROM account_journal').fetchone()[0]
    replacement=event(price='14',broker_id='replacement')
    assert submit(mapped,batch(mapped,[replacement],version=1,cash='8600',voids=[journal_id]))['status']=='posted'
    current=ids(mapped,'event')
    assert all(current[k]==v for k,v in old.items()) and len(current)==2


def test_mapping_failure_rolls_back_cycle_and_financial_projection(mapped):
    conn=mapped[0]
    initialize(mapped)
    submit(mapped,batch(mapped,[event('sell',broker_id='close')],shares=0,cash='11200'))
    before_cycles,before_events,before_state=ids(mapped,'cycle'),ids(mapped,'event'),state(mapped)
    conn.execute("CREATE TRIGGER reject_projection_event BEFORE INSERT ON account_projection_ids WHEN NEW.kind='event' BEGIN SELECT RAISE(ABORT,'structural mapping failure'); END")
    value=batch(mapped,[event(broker_id='reopen',occurred_at='2026-11-03T10:00:00')],
                version=1,shares=100,cash='10000',entries={'0':{'policy_identity':'structural-new'}})
    result=submit(mapped,value)
    assert result['status']=='service_failure'
    assert ids(mapped,'cycle')==before_cycles and ids(mapped,'event')==before_events and state(mapped)==before_state
    assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0]==1
    conn.execute('DROP TRIGGER reject_projection_event')
    assert submit(mapped,value)['status']=='posted'


def test_migration_backfills_existing_cost_book_and_is_idempotent(mapped):
    conn=mapped[0]
    initialize(mapped)
    submit(mapped,batch(mapped,[event()]))
    row=conn.execute('SELECT id FROM account_journal').fetchone()[0]
    submit(mapped,batch(mapped,[event(broker_id='replacement')],version=1,voids=[row]))
    expected_cycles,expected_events=ids(mapped,'cycle'),ids(mapped,'event')
    conn.execute('DROP TABLE account_projection_ids')
    with db.transaction(conn):
        projection_ids.migrate(conn)
    assert ids(mapped,'cycle')==expected_cycles and ids(mapped,'event')==expected_events
    with db.transaction(conn):
        projection_ids.migrate(conn)
    assert ids(mapped,'cycle')==expected_cycles and ids(mapped,'event')==expected_events


def test_cash_only_events_have_no_legacy_stock_event_id(mapped):
    initialize(mapped)
    from a_share_research.account.reconciliation import Event
    deposit=Event(kind='deposit',cash='20',occurred_at='2026-11-02T10:00:00')
    assert submit(mapped,batch(mapped,[deposit],shares=100,cash='10020'))['status']=='posted'
    assert ids(mapped,'event')=={}


def test_independent_accounts_can_preserve_the_same_legacy_id(mapped):
    conn=mapped[0]
    initialize(mapped)
    original=conn.execute('SELECT * FROM account_books WHERE account_id=?',('a',)).fetchone()
    fields=list(original.keys())
    values=[('b' if field=='account_id' else original[field]) for field in fields]
    conn.execute(f"INSERT INTO account_books ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",values)
    projection=state(mapped)['projection']
    conn.execute('INSERT INTO account_position_books VALUES (?,?,?,?,?,?)',
        ('b',0,db.now(),'[]',json.dumps([seed().model_dump(mode='json')]),json.dumps(projection)))
    with db.transaction(conn):
        projection_ids.sync(conn,'b',projection)
    assert projection_ids.lookup(conn,'b','cycle')=={'legacy:1':1}


def test_sync_requires_transaction_and_kind_is_closed(mapped):
    initialize(mapped)
    with pytest.raises(ValueError,match='同一事务'):
        projection_ids.sync(mapped[0],'a',state(mapped)['projection'])
    with pytest.raises(ValueError,match='类型'):
        projection_ids.lookup(mapped[0],'a','arbitrary')
