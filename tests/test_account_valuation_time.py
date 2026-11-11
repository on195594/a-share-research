"""Valuation-time contracts use invented structural evidence, never broker acceptance data."""
from datetime import date, datetime
import json

import pytest
from pydantic import ValidationError

from a_share_research import db, gate
from a_share_research.account import ledger
from a_share_research.account.reconciliation import Snapshot
from test_account_ledger import book as ledger_book, batch, submit


@pytest.fixture
def book(tmp_path):
    yield from ledger_book.__wrapped__(tmp_path)


def snapshot(**changes):
    return dict(positions={},cash='100',market_values={},total_assets='100') | changes


def observation(**changes):
    return dict(observed_at='2026-10-08T16:00:00+08:00',total_assets='100') | changes


def valued_batch(book, stamp, **changes):
    raw=batch(book,**changes).model_dump(mode='json')
    raw['snapshot']['portfolio_value_as_of']=stamp
    return ledger.Batch.model_validate(raw)


@pytest.mark.parametrize('kind',['snapshot','observation'])
@pytest.mark.parametrize('stamp',[True,0,1791430200.5,'2026-10-08','2026-10-08T15:30:00',
    datetime(2026,10,8,15,30),date(2026,10,8)])
def test_valuation_requires_explicit_full_time_and_timezone(kind,stamp):
    model,data=(Snapshot,snapshot()) if kind=='snapshot' else (ledger.Observation,observation())
    with pytest.raises(ValidationError):model.model_validate(data|{'portfolio_value_as_of':stamp})


@pytest.mark.parametrize('kind',['snapshot','observation'])
def test_timestamp_normalizes_timezone_and_absent_value_preserves_legacy_json(kind):
    model,data=(Snapshot,snapshot()) if kind=='snapshot' else (ledger.Observation,observation())
    original=model.model_validate(data).model_dump(mode='json')
    explicit_none=model.model_validate(data|{'portfolio_value_as_of':None}).model_dump(mode='json')
    assert original==explicit_none and 'portfolio_value_as_of' not in original
    assert gate.digest(original)==gate.digest(explicit_none)
    first=model.model_validate(data|{'portfolio_value_as_of':'2026-10-08T07:30:00Z'})
    second=model.model_validate(data|{'portfolio_value_as_of':'2026-10-08T15:30:00+08:00'})
    assert first.model_dump(mode='json')==second.model_dump(mode='json')
    assert first.model_dump(mode='json')['portfolio_value_as_of']=='2026-10-08T15:30:00+08:00'


@pytest.mark.parametrize('changes',[
    {'total_assets':None,'cash':'100'},
    {'observed_at':'2026-10-08T15:29:00+08:00'},
    {'observed_at':'2026-10-08T16:00:00'},
])
def test_observation_timestamp_requires_total_and_causal_aware_observation(changes):
    with pytest.raises(ValidationError):
        ledger.Observation.model_validate(observation(portfolio_value_as_of='2026-10-08T15:30:00+08:00',**changes))


def test_legacy_posted_hash_and_explicit_null_retry_are_unchanged(book):
    value=batch(book)
    original=value.model_dump(mode='json')
    receipt=submit(book,value)
    assert receipt['status']=='posted' and receipt['request_hash']==gate.digest(original)
    original['snapshot']['portfolio_value_as_of']=None
    assert submit(book,ledger.Batch.model_validate(original))==receipt
    assert book[0].execute('SELECT count(*) FROM account_batches').fetchone()[0]==1


def test_valued_snapshot_is_stored_and_repeated_submission_is_idempotent(book):
    value=valued_batch(book,'2026-10-08T15:30:00+08:00')
    result=submit(book,value)
    assert result['status']=='posted' and submit(book,value)==result
    saved=book[0].execute('SELECT request FROM account_batches WHERE id=?',(result['receipt_id'],)).fetchone()[0]
    assert json.loads(saved)['snapshot']['portfolio_value_as_of']=='2026-10-08T15:30:00+08:00'
    assert book[0].execute('SELECT count(*) FROM account_journal').fetchone()[0]==1


def test_corrected_observation_time_appends_and_keeps_original_request(book):
    conn,principal,directory,sha=book
    def save(stamp):
        return ledger.observe(conn,principal,'a',ledger.Observation.model_validate(
            observation(portfolio_value_as_of=stamp)),directory,sha)
    first=save('2026-10-08T15:30:00+08:00')
    original=conn.execute('SELECT observation FROM account_observations WHERE id=?',(first['observation_id'],)).fetchone()[0]
    assert save('2026-10-08T07:30:00Z')==first
    corrected=save('2026-10-08T15:35:00+08:00')
    assert corrected['observation_id']!=first['observation_id']
    assert corrected['request_hash']!=first['request_hash']
    assert conn.execute('SELECT observation FROM account_observations WHERE id=?',(first['observation_id'],)).fetchone()[0]==original
    assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0]==0


def test_future_observation_never_persists(book):
    conn,principal,directory,sha=book
    value=ledger.Observation.model_validate(observation(
        observed_at='2099-01-01T16:00:00+08:00',portfolio_value_as_of='2099-01-01T15:00:00+08:00'))
    with pytest.raises(ledger.LedgerError):ledger.observe(conn,principal,'a',value,directory,sha)
    assert conn.execute('SELECT count(*) FROM account_observations').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0]==0


@pytest.mark.parametrize('case',['future','before-opening','before-new-event','before-retained-event'])
def test_impossible_snapshot_time_returns_conflict_without_financial_write(book,case):
    if case=='before-retained-event':
        assert submit(book,batch(book))['status']=='posted'
        value=valued_batch(book,'2026-10-08T15:29:00+08:00',version=1,events=[])
    elif case=='before-opening':
        value=valued_batch(book,'2026-09-30T15:30:00+08:00',events=[],positions={},cash='200000')
    else:
        value=valued_batch(book,'2099-01-01T15:30:00+08:00' if case=='future' else '2026-10-08T15:29:00+08:00')
    before=ledger.state(book[0],book[1],'a')
    events=book[0].execute('SELECT id,event FROM account_journal ORDER BY id').fetchall()
    result=submit(book,value)
    assert result['status']=='conflict'
    assert ledger.receipt(book[0],book[1],'a',result['receipt_id'])==result
    assert ledger.state(book[0],book[1],'a')==before
    assert book[0].execute('SELECT id,event FROM account_journal ORDER BY id').fetchall()==events


def test_same_second_fractional_time_is_not_future_or_lost_from_record(book,monkeypatch):
    now=datetime.fromisoformat('2026-11-11T16:00:00.999999+08:00')
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return now if tz is None else now.astimezone(tz)
    monkeypatch.setattr(ledger,'datetime',Clock)
    monkeypatch.setattr(db,'now',lambda:'2026-11-11T16:00:00+08:00')
    stamp='2026-11-11T16:00:00.500000+08:00'
    value=valued_batch(book,stamp,events=[],positions={},cash='200000')
    result=submit(book,value)
    assert result['status']=='posted'
    recorded=book[0].execute('SELECT created_at FROM account_batches WHERE id=?',(result['receipt_id'],)).fetchone()[0]
    assert datetime.fromisoformat(recorded)>=datetime.fromisoformat(stamp)
    observed=ledger.observe(book[0],book[1],'a',ledger.Observation.model_validate(observation(
        observed_at=now.isoformat(),portfolio_value_as_of=stamp)),book[2],book[3])
    recorded=book[0].execute('SELECT created_at FROM account_observations WHERE id=?',(observed['observation_id'],)).fetchone()[0]
    assert datetime.fromisoformat(recorded)>=datetime.fromisoformat(stamp)
