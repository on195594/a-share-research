"""Shadow journal invariants; structural numbers from test_account, not broker acceptance evidence."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest
from a_share_research import api_keys, db
from a_share_research.account import evidence, ledger
from a_share_research.account.reconciliation import Event, Opening, Snapshot


@pytest.fixture
def book(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'account', role='assistant'))
    directory = tmp_path / 'evidence'
    sha = evidence.store(directory, b'Structural account test evidence, not a broker statement')
    ledger.initialize(conn, principal, 'a', Opening(positions={}, cash='200000'),
                      datetime.fromisoformat('2026-10-01T00:00:00+08:00'), directory, sha, tolerance=Decimal('0'))
    yield conn, principal, directory, sha
    conn.close()


def buy(**changes):
    return Event.model_validate(dict(kind='buy', code='600519', shares=100, price='1400.5',
                                    occurred_at='2026-10-08T15:30:00+08:00', broker_id='trade1') | changes)


def batch(book, *, version=0, events=None, positions=None, cash='59950', voids=()):
    positions = positions if positions is not None else {'600519':100}
    values = {code: Decimal(shares)*100 for code, shares in positions.items() if shares}
    return ledger.Batch(expected_version=version, events=[buy()] if events is None else events,
                        void_event_ids=list(voids), evidence_hashes=[book[3]],
                        snapshot=Snapshot(positions=positions, cash=cash, market_values=values,
                                          total_assets=Decimal(cash)+sum(values.values())))


def submit(book, value):
    return ledger.submit(book[0],book[1],'a',value,book[2])


def test_posted_retries_and_cross_batch_duplicates_are_not_new_events(book):
    value=batch(book)
    result=submit(book,value)
    assert result['status']=='posted' and result['added']==1 and result['version']==1
    assert submit(book,value)==result
    assert ledger.receipt(book[0],book[1],'a',result['receipt_id'])==result
    repeated=submit(book,batch(book,version=1))
    assert repeated['duplicates']==1 and repeated['added']==0 and repeated['version']==1
    assert book[0].execute('SELECT count(*) FROM account_journal').fetchone()[0]==1
    assert book[0].execute('SELECT count(*) FROM holdings').fetchone()[0]==0
    assert book[0].execute('SELECT count(*) FROM holding_events').fetchone()[0]==0


def test_same_broker_change_needs_void_and_preserves_original(book):
    submit(book,batch(book))
    changed=buy(price='1401')
    result=submit(book,batch(book,version=1,events=[changed],cash='59900'))
    assert result['status']=='conflict'
    page=ledger.events(book[0],book[1],'a')
    assert len(page['events'])==1 and page['events'][0]['active']
    original=book[0].execute('SELECT id,event FROM account_journal').fetchone()
    assert page['events'][0]['id']==original['id']
    corrected=submit(book,batch(book,version=1,events=[changed],cash='59900',voids=[original['id']]))
    assert corrected['status']=='posted' and corrected['voided']==1 and corrected['added']==1
    assert book[0].execute('SELECT event FROM account_journal WHERE id=?',(original['id'],)).fetchone()[0]==original['event']
    assert book[0].execute('SELECT count(*) FROM account_journal').fetchone()[0]==2
    history=ledger.events(book[0],book[1],'a')['events']
    assert [r['active'] for r in history]==[False,True]
    assert len(ledger.events(book[0],book[1],'a',after_id=original['id'])['events'])==1


def test_cashflow_partial_sell_and_corporate_action(book):
    events=[buy(),buy(kind='dividend',shares=0,price=0,cash='2500',broker_id='div',occurred_at='2026-10-09T09:00:00'),
            buy(kind='adjustment',shares=100,price=0,broker_id='split',occurred_at='2026-10-10T09:00:00'),
            buy(kind='sell',shares=50,price='700',fees='5',tax='17.5',broker_id='sell',occurred_at='2026-10-11T09:00:00'),
            Event(kind='withdrawal',cash='1000',occurred_at='2026-10-12T09:00:00')]
    result=submit(book,batch(book,events=events,positions={'600519':150},cash='96427.5'))
    assert result['status']=='posted' and result['added']==5


@pytest.mark.parametrize('field', ['shares','cash','assets'])
def test_reconciliation_conflict_changes_no_projection_or_events(book,field):
    value=batch(book)
    if field=='shares':value.snapshot.positions['600519']=101
    if field=='cash':value.snapshot.cash+=1
    if field=='assets':value.snapshot.total_assets+=1
    before=ledger.state(book[0],book[1],'a')
    assert submit(book,value)['status']=='conflict'
    assert ledger.state(book[0],book[1],'a')==before
    assert book[0].execute('SELECT count(*) FROM account_journal').fetchone()[0]==0


def test_atomic_insert_failure_rolls_back_receipt_and_projection(book):
    conn=book[0]
    conn.execute("CREATE TRIGGER reject_journal BEFORE INSERT ON account_journal BEGIN SELECT RAISE(ABORT,'write interrupted'); END")
    result=submit(book,batch(book))
    assert result['status']=='service_failure'
    assert ledger.receipt(conn,book[1],'a',result['receipt_id'])==result
    assert conn.execute("SELECT count(*) FROM account_batches WHERE status='posted'").fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0]==0
    assert ledger.state(conn,book[1],'a')['version']==0


def test_missing_evidence_can_be_supplied_and_same_payload_retried(book):
    value=batch(book)
    path=book[2]/book[3]
    content=path.read_bytes();path.unlink()
    assert submit(book,value)['status']=='missing_material'
    evidence.store(book[2],content)
    assert submit(book,value)['status']=='posted'


def test_stale_version_and_research_credential_rejected(book):
    other=db.connect(Path(book[0].execute('PRAGMA database_list').fetchone()[2]))
    stale_version=ledger.state(other,book[1],'a')['version']
    submit(book,batch(book))
    stale=batch(book,version=stale_version,events=[])
    try:
        assert ledger.submit(other,book[1],'a',stale,book[2])['status']=='conflict'
    finally:
        other.close()
    principal=api_keys.authenticate(book[0],api_keys.create(book[0],'research'))
    with pytest.raises(ledger.LedgerError) as error:
        ledger.state(book[0],principal,'a')
    assert error.value.status==403


def test_explicit_basis_rejects_other_company_future_and_failed_reports(book):
    conn=book[0]
    for code,passed,created in [('000858',1,'2026-10-01'),('600519',1,'2026-10-09'),('600519',0,'2026-10-01')]:
        run=db.create_run(conn,code,status='done')
        db.save_report(conn,run,code,'test structure','2025-12-31',passed,None,{})
        rid=conn.execute('SELECT id FROM reports WHERE run_id=?',(run,)).fetchone()[0]
        conn.execute('UPDATE reports SET created_at=? WHERE id=?',(created,rid))
        assert submit(book,batch(book,events=[buy(report_id=rid)]))['status']=='conflict'
    assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0]==0


def test_append_guards_and_observation_never_infer_trades(book):
    submit(book,batch(book))
    for sql in ('DELETE FROM account_journal','UPDATE account_batches SET receipt=receipt',
                'UPDATE account_books SET opening=opening'):
        with pytest.raises(sqlite3.IntegrityError):book[0].execute(sql)
    observed=ledger.observe(book[0],book[1],'a',ledger.Observation(observed_at='2026-10-08T16:00:00',total_assets='300000'),book[2],book[3])
    assert observed['trades_recorded'] is False
    assert ledger.state(book[0],book[1],'a')['version']==1


def test_valid_explicit_basis_is_kept_on_event(book):
    conn=book[0]
    run=db.create_run(conn,'600519',status='done')
    db.save_report(conn,run,'600519','structural','2025-12-31',True,None,{})
    rid=conn.execute('SELECT id FROM reports WHERE run_id=?',(run,)).fetchone()[0]
    conn.execute("UPDATE reports SET created_at='2026-10-01T09:00:00+08:00' WHERE id=?",(rid,))
    assert submit(book,batch(book,events=[buy(report_id=rid)]))['status']=='posted'
    import json
    assert json.loads(conn.execute('SELECT event FROM account_journal').fetchone()[0])['report_id']==rid
