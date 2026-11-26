"""Structural cost-basis contracts only; invented values are not broker acceptance evidence."""
from decimal import Decimal
import json
import sqlite3

import pytest

from a_share_research import api_keys, gate
from a_share_research.account import evidence, ledger, positions, position_store
from support.positions import batch, event, initialize, seed, state, submit


def test_explicit_seed_is_idempotent_immutable_and_not_lifetime_cashflow(position_book):
    book = position_book
    first = initialize(book)
    assert initialize(book) == first
    assert first['cash_flow_scope'] == 'since_opening' and first['policy_evaluation'] == 'pending'
    cycle = first['projection']['cycles'][0]
    assert cycle['legacy_holding_id'] == 1 and cycle['policy_identity'] == 'structural-policy-v0'
    assert cycle['cost_price'] == '10' and cycle['invested_since_opening'] == '0'
    assert cycle['realized_pnl_since_opening'] == cycle['dividends_since_opening'] == '0'
    with pytest.raises(ledger.LedgerError,match='不能覆盖'):
        initialize(book,[seed(cost_price='11')])
    for sql in ('UPDATE account_position_books SET basis=basis','DELETE FROM account_position_books'):
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            book[0].execute(sql)


@pytest.mark.parametrize('seeds,match', [([seed(shares=99)],'股数'),([seed(buy_date='2026-11-02')],'日期')])
def test_seed_quantity_and_date_must_match_account_opening(position_book,seeds,match):
    book = position_book
    with pytest.raises(ledger.LedgerError,match=match):
        initialize(book,seeds)
    assert book[0].execute('SELECT count(*) FROM account_position_books').fetchone()[0] == 0


def test_missing_evidence_or_late_initialization_is_rejected(position_book):
    book = position_book
    with pytest.raises(ledger.LedgerError,match='证据'):
        initialize(book,hashes=[])
    with pytest.raises(evidence.EvidenceError):
        initialize(book,hashes=['0'*64])
    assert submit(book,batch(book,[event()]))['status']=='posted'
    with pytest.raises(ledger.LedgerError,match='首笔入账前'):
        initialize(book)


def test_buy_partial_and_full_sell_then_reopen_without_context_cannot_be_backfilled(position_book):
    book = position_book
    before=initialize(book)
    value=batch(book,[event()])
    bought=submit(book,value)
    assert bought['status']=='posted' and bought['position_before']==before['projection']
    assert bought['position_after']==state(book)['projection'] and state(book)['version']==1
    assert state(book)['projection']['cycles'][0]['cost_price']=='11'
    assert submit(book,value)==bought
    sold=submit(book,batch(book,[event('sell',shares=50,price='15',broker_id='sell-1',occurred_at='2026-11-03T10:00:00')],version=1,shares=150,cash='9550'))
    assert sold['status']=='posted' and state(book)['projection']['cycles'][0]['realized_pnl_since_opening']=='200'
    closed=submit(book,batch(book,[event('sell',shares=150,price='15',broker_id='sell-2',occurred_at='2026-11-04T10:00:00')],version=2,shares=0,cash='11800'))
    assert closed['status']=='posted' and state(book)['projection']['cycles'][0]['exit_date']=='2026-11-04'
    reopen=event(price='14',broker_id='reopen',occurred_at='2026-11-05T10:00:00')
    bare=batch(book,[reopen],version=3,shares=100,cash='10400')
    opened=submit(book,bare)
    assert opened['status']=='posted' and submit(book,bare)==opened and state(book)['version']==4
    cycles=state(book)['projection']['cycles']
    assert len(cycles)==2 and cycles[0]['shares']==0 and cycles[1]['shares']==100
    assert cycles[1]['policy_identity'] is None and cycles[1]['cycle_id'].startswith('event:')
    assert position_store.inspect(book[0])==[]
    projection=state(book)['projection']
    backfill=batch(book,[reopen],version=4,shares=100,cash='10400',entries={'0':{'policy_identity':'new-structural-policy','name':'Structural'}})
    rejected=submit(book,backfill)
    assert rejected['status']=='conflict' and rejected['problems'][0]['field']=='position_entries'
    assert state(book)['version']==4 and state(book)['projection']==projection
    reopen_id,=book[0].execute('SELECT max(id) FROM account_journal').fetchone()
    ready=backfill.model_copy(update={'void_event_ids':[reopen_id]})
    result=submit(book,ready)
    assert result['status']=='posted' and submit(book,ready)==result and state(book)['version']==5
    cycles=state(book)['projection']['cycles']
    assert len(cycles)==2 and cycles[1]['policy_identity']=='new-structural-policy'
    assert position_store.inspect(book[0])==[]
    changed=ready.model_copy(update={'expected_version':5,'void_event_ids':[],'position_entries':{'0':positions.PositionEntry(policy_identity='different-policy')}})
    assert submit(book,changed)['status']=='conflict'
    assert state(book)['version']==5


def test_void_replace_replays_cost_and_retains_original_receipt(position_book):
    book = position_book
    initialize(book)
    first=submit(book,batch(book,[event()]))
    journal=book[0].execute('SELECT id,event FROM account_journal').fetchone()
    corrected=submit(book,batch(book,[event(price='14')],version=1,cash='8600',voids=[journal['id']]))
    assert corrected['status']=='posted' and corrected['voided']==1
    assert state(book)['projection']['cycles'][0]['cost_price']=='12'
    assert book[0].execute('SELECT event FROM account_journal WHERE id=?',(journal['id'],)).fetchone()[0]==journal['event']
    assert ledger.receipt(book[0],book[1],'a',first['receipt_id'])==first
    assert position_store.inspect(book[0])==[]


def test_projection_sql_failure_rolls_back_financial_events_and_records_failure(position_book):
    book = position_book
    before=initialize(book)
    book[0].execute("CREATE TRIGGER reject_position BEFORE UPDATE OF projection ON account_position_books BEGIN SELECT RAISE(ABORT,'structural projection failure'); END")
    value=batch(book,[event()])
    failed=submit(book,value)
    assert failed['status']=='service_failure' and state(book)==before
    assert ledger.receipt(book[0],book[1],'a',failed['receipt_id'])==failed
    assert book[0].execute('SELECT count(*) FROM account_journal').fetchone()[0]==0
    assert book[0].execute("SELECT count(*) FROM account_batches WHERE status='posted'").fetchone()[0]==0
    assert ledger.state(book[0],book[1],'a')['version']==0
    book[0].execute('DROP TRIGGER reject_position')
    assert submit(book,value)['status']=='posted'
    assert position_store.inspect(book[0])==[]


@pytest.mark.parametrize('role',['research','owner'])
def test_other_roles_cannot_initialize_or_read_costs(position_book,role):
    book = position_book
    conn,_,directory,sha=book
    denied=api_keys.authenticate(conn,api_keys.create(conn,'denied-'+role,role=role))
    for action in (lambda:position_store.initialize(conn,denied,'a',[seed()],directory,[sha]),
                   lambda:position_store.state(conn,denied,'a')):
        with pytest.raises(ledger.LedgerError) as caught:
            action()
        assert caught.value.status==403


def test_integrity_detects_modified_cost_projection(position_book):
    book = position_book
    initialize(book)
    submit(book,batch(book,[event()]))
    assert position_store.inspect(book[0])==[]
    corrupted=state(book)['projection']
    corrupted['cycles'][0]['cost_price']='999'
    book[0].execute('UPDATE account_position_books SET projection=?',(gate.canonical(corrupted),))
    assert any('重放不一致' in problem for problem in position_store.inspect(book[0]))


def test_empty_position_entries_preserves_legacy_batch_hash(position_book):
    book = position_book
    value=batch(book,[event()])
    dumped=value.model_dump(mode='json')
    assert 'position_entries' not in dumped
    old=dict(expected_version=0,events=[event().model_dump(mode='json')],void_event_ids=[],
             evidence_hashes=[book[3]],snapshot=value.snapshot.model_dump(mode='json'))
    assert gate.digest(dumped)==gate.digest(old)
    assert json.loads(value.model_dump_json())==old


def test_paid_dividend_can_bind_closed_cycle_after_reopening(position_book):
    book = position_book
    initialize(book)
    closed=submit(book,batch(book,[event('sell',broker_id='close')],shares=0,cash='11200'))
    assert closed['status']=='posted'
    reopened=submit(book,batch(book,[event(broker_id='reopen',occurred_at='2026-11-03T10:00:00')],
        version=1,shares=100,cash='10000',entries={'0':{'policy_identity':'structural-new-cycle'}}))
    assert reopened['status']=='posted'
    payment=event('dividend',shares=0,price=0,cash='20',broker_id='payment',occurred_at='2026-11-04T10:00:00')
    value=batch(book,[payment],version=2,shares=100,cash='10020')
    assert submit(book,value)['status']=='missing_material'
    bound=value.model_copy(update={'position_dividends':{'0':'legacy:1'}})
    result=submit(book,bound)
    assert result['status']=='posted' and submit(book,bound)==result
    old,new=state(book)['projection']['cycles']
    assert old['shares']==0 and old['dividends_since_opening']=='20' and old['exit_date']=='2026-11-02'
    assert new['shares']==100 and new['dividends_since_opening']=='0'
    assert position_store.inspect(book[0])==[]


def test_cost_version_drift_is_not_silently_repaired(position_book):
    book = position_book
    initialize(book)
    book[0].execute('UPDATE account_position_books SET version=9')
    result=submit(book,batch(book,[event()]))
    assert result['status']=='conflict' and result['problems'][0]['field']=='cost_version'
    assert book[0].execute('SELECT count(*) FROM account_journal').fetchone()[0]==0


def test_v16_entry_payload_keeps_its_original_hash(position_book):
    book = position_book
    value=batch(book,[event()],entries={'0':{'policy_identity':'v16-profile','name':'Structural'}})
    old=dict(expected_version=0,events=[event().model_dump(mode='json')],void_event_ids=[],
             evidence_hashes=[book[3]],snapshot=value.snapshot.model_dump(mode='json'),
             position_entries={'0':{'policy_identity':'v16-profile','name':'Structural'}})
    assert gate.digest(value.model_dump(mode='json'))==gate.digest(old)


def test_posted_dividend_metadata_cannot_be_added_or_replaced_by_duplicate(position_book):
    book = position_book
    initialize(book)
    payment=event('dividend',shares=0,price=0,cash='20')
    value=batch(book,[payment],shares=100,cash='10020')
    value=value.model_copy(update={'position_distributions':{'0':Decimal('0.2')}})
    assert submit(book,value)['status']=='posted'
    before=state(book)
    for metadata in ({'position_distributions':{'0':Decimal('0.3')}},
                     {'position_dividends':{'0':'legacy:1'}}):
        retry=value.model_copy(update={'expected_version':1,**metadata})
        assert submit(book,retry)['status']=='conflict'
        assert state(book)==before
    assert position_store.inspect(book[0])==[]


def test_duplicate_business_event_cannot_carry_conflicting_distribution_metadata(position_book):
    book = position_book
    payment=event('dividend',shares=0,price=0,cash='20')
    value=batch(book,[payment,payment],shares=100,cash='10020').model_dump(mode='json')
    value['position_distributions']={'0':'0.2','1':'0.3'}
    with pytest.raises(ValueError,match='同一业务事件'):
        ledger.Batch.model_validate(value)
