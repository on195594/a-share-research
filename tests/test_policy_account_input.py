"""Invented ledger transport fixtures only; these are not broker or investment evidence."""
from copy import deepcopy
import json
import sqlite3

import pytest

from a_share_research import db, gate
from a_share_research.account import analysis, cycle_analysis, ledger
from a_share_research.policy import account_input
from a_share_research.policy.v0 import account_store
from test_v0_account_store import book as policy_book
from test_position_store import batch


@pytest.fixture
def bound_book(tmp_path):
    for conn,principal,old_directory,sha in policy_book.__wrapped__(tmp_path):
        directory=tmp_path/'account-evidence'
        old_directory.rename(directory)
        account_store.initialize(conn,principal,'a',directory,[sha])
        baseline=analysis.initialize(conn,principal,1)
        cycle_analysis.initialize(conn,principal,'a','legacy:1',cycle_analysis.Initialize(
            expected_account_version=0,evidence_hashes=[sha],legacy_revision_id=baseline['revision_id']),directory)
        binding=dict(schema_version=1,account_id='a',account_scope='structural-scope',mirror_watermark={})
        conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',('ledger','a'*64,db.now(),gate.canonical(binding)))
        yield tmp_path/'app.db',conn,principal,directory,sha


def source(book):
    path,conn,_,_,_=book
    return account_input.read(conn,path.parent,conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source').fetchone())


def observe(book,total='11200'):
    _,conn,principal,directory,sha=book
    return ledger.observe(conn,principal,'a',ledger.Observation(observed_at='2026-11-01T15:00:00+08:00',total_assets=total),directory,sha)


def test_bound_source_uses_account_scoped_projection_without_old_asset_file(bound_book):
    before=source(bound_book)
    assert before['mode']=='ledger' and before['account_version']==0 and before['asset'] is None
    assert before['watermark']['account_id']=='a'
    account_input.validate_source(before)
    (bound_book[0].parent/'account-snapshot.json').write_text('invalid legacy file must not be opened')
    assert source(bound_book)==before
    with sqlite3.connect(bound_book[0].as_uri()+'?mode=ro',uri=True) as plain:
        row=plain.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source').fetchone()
        assert account_input.read(plain,bound_book[0].parent,row)==before


def test_asset_observation_changes_digest_without_inventing_valuation_time(bound_book):
    before=source(bound_book)
    observed=observe(bound_book)
    after=source(bound_book)
    assert after['account_version']==before['account_version']==0
    assert after['state_sha256']!=before['state_sha256']
    assert after['asset']['source']=='account_observation'
    assert after['asset']['record_id']==observed['observation_id']
    assert after['asset']['portfolio_value']=='11200' and after['asset']['portfolio_value_as_of'] is None
    assert after['asset']['recorded_at']!='2026-11-01T15:00:00+08:00'
    assert source(bound_book)==after


def test_posted_no_event_snapshot_counts_as_asset_change_at_same_version(bound_book,monkeypatch):
    observe(bound_book)
    before=source(bound_book)
    monkeypatch.setattr(db,'now',lambda:'2099-01-01T00:00:00+08:00')
    _,conn,principal,directory,sha=bound_book
    receipt=ledger.submit(conn,principal,'a',batch((conn,principal,directory,sha),[],shares=100,cash='10000'),directory)
    assert receipt['status']=='posted' and receipt['version']==0
    after=source(bound_book)
    assert after['account_version']==0 and before['state_sha256']!=after['state_sha256']
    assert after['asset']['source']=='account_batch' and after['asset']['record_id']==receipt['receipt_id']
    assert after['asset']['portfolio_value_as_of'] is None


def test_scoped_baseline_is_required_even_when_legacy_analysis_exists(bound_book):
    conn=bound_book[1]
    conn.execute('DROP TRIGGER account_analysis_no_delete')
    conn.execute("DELETE FROM account_analysis_history WHERE account_id='a'")
    with pytest.raises(account_input.AccountInputError): source(bound_book)


def test_missing_or_altered_actual_evidence_fails_closed(bound_book):
    observe(bound_book)
    (bound_book[3]/bound_book[4]).write_bytes(b'explicit malformed evidence variant')
    with pytest.raises(account_input.AccountInputError): source(bound_book)


def test_cash_projection_corruption_cannot_be_hidden_by_cost_projection(bound_book):
    conn=bound_book[1]
    projection=json.loads(conn.execute('SELECT projection FROM account_books').fetchone()[0])
    projection['cash']='99999'
    conn.execute('UPDATE account_books SET projection=?',(gate.canonical(projection),))
    with pytest.raises(account_input.AccountInputError,match='资金投影'): source(bound_book)


@pytest.mark.parametrize('variant',['missing-account','wrong-mode','missing-binding','unknown-binding-field','boolean-version','duplicate-key'])
def test_source_binding_never_guesses_account(bound_book,variant):
    conn=bound_book[1]
    row=list(conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source').fetchone())
    binding=json.loads(row[3])
    if variant=='missing-account': binding['account_id']='missing'
    elif variant=='wrong-mode': row[0]='mirror'
    elif variant=='missing-binding': binding.pop('account_id')
    elif variant=='unknown-binding-field': binding['guess']=True
    elif variant=='boolean-version': binding['schema_version']=True
    row[3]=gate.canonical(binding)
    if variant=='duplicate-key': row[3]=row[3].replace('"schema_version":1','"schema_version":1,"schema_version":1')
    with pytest.raises(account_input.AccountInputError): account_input.read(conn,bound_book[0].parent,row)


@pytest.mark.parametrize('variant',['extra','hash','version','asset-time','asset-scope','asset-amount','asset-evidence'])
def test_saved_source_validation_is_strict_and_offline(bound_book,variant):
    observe(bound_book)
    saved=deepcopy(source(bound_book))
    if variant=='extra': saved['unrecognized']=True
    elif variant=='hash': saved['state_sha256']='invalid'
    elif variant=='version': saved['account_version']=True
    elif variant=='asset-time': saved['asset']['portfolio_value_as_of']='2026-11-01'
    elif variant=='asset-scope': saved['asset']['account_scope']='different'
    elif variant=='asset-amount': saved['asset']['portfolio_value']='NaN'
    elif variant=='asset-evidence': saved['asset']['evidence_hashes']=[]
    with pytest.raises(account_input.AccountInputError): account_input.validate_source(saved)


def test_observation_hash_and_posted_receipt_tampering_rejected(bound_book):
    observe(bound_book)
    conn=bound_book[1]
    conn.execute('DROP TRIGGER account_observations_no_update')
    conn.execute("UPDATE account_observations SET request_hash='tampered'")
    with pytest.raises(account_input.AccountInputError,match='观察哈希'): source(bound_book)


@pytest.mark.parametrize('observation_amount,conflict',[('11200.00',False),('12000',True)])
def test_same_second_cross_source_assets_do_not_guess_order(bound_book,monkeypatch,observation_amount,conflict):
    monkeypatch.setattr(db,'now',lambda:'2026-11-11T12:00:00+08:00')
    observe(bound_book,observation_amount)
    _,conn,principal,directory,sha=bound_book
    receipt=ledger.submit(conn,principal,'a',batch((conn,principal,directory,sha),[],shares=100,cash='10000'),directory)
    assert receipt['status']=='posted'
    if conflict:
        with pytest.raises(account_input.AccountInputError,match='不能猜测先后'): source(bound_book)
    else:
        assert source(bound_book)['asset']['source']=='account_batch'


def test_same_source_same_second_uses_append_id(bound_book,monkeypatch):
    monkeypatch.setattr(db,'now',lambda:'2026-11-11T12:00:00+08:00')
    observe(bound_book,'11200')
    latest=observe(bound_book,'12300')
    selected=source(bound_book)['asset']
    assert selected['record_id']==latest['observation_id'] and selected['portfolio_value']=='12300'


def test_posted_snapshot_must_reconcile_with_its_receipt(bound_book):
    _,conn,principal,directory,sha=bound_book
    receipt=ledger.submit(conn,principal,'a',batch((conn,principal,directory,sha),[],shares=100,cash='10000'),directory)
    receipt['after']['cash']='9999'
    conn.execute('DROP TRIGGER account_batches_no_update')
    conn.execute('UPDATE account_batches SET receipt=? WHERE id=?',(gate.canonical(receipt),receipt['receipt_id']))
    with pytest.raises(account_input.AccountInputError,match='未与回执资金股数对平'): source(bound_book)


def test_scoped_analysis_append_changes_source_without_account_version(bound_book):
    from test_account_analysis import request
    from test_analysis_transitions import open_alert
    before=source(bound_book)
    _,conn,principal,directory,sha=bound_book
    change=request((conn,principal,directory,sha),[open_alert()])
    cycle_analysis.update(conn,principal,'a','legacy:1',change,directory)
    after=source(bound_book)
    assert after['account_version']==before['account_version']==0
    assert after['state_sha256']!=before['state_sha256']


@pytest.mark.parametrize('kind',['observation','batch'])
def test_explicit_valuation_time_survives_verified_source_read(bound_book,kind):
    _,conn,principal,directory,sha=bound_book
    stamp='2026-11-01T15:00:00+08:00'
    if kind=='observation':
        ledger.observe(conn,principal,'a',ledger.Observation(observed_at='2026-11-01T16:00:00+08:00',
            total_assets='11200',portfolio_value_as_of=stamp),directory,sha)
    else:
        value=batch((conn,principal,directory,sha),[],shares=100,cash='10000').model_dump(mode='json')
        value['snapshot']['portfolio_value_as_of']=stamp
        assert ledger.submit(conn,principal,'a',ledger.Batch.model_validate(value),directory)['status']=='posted'
    selected=source(bound_book)
    assert selected['asset']['portfolio_value_as_of']==stamp
    assert selected['asset']['source']=='account_'+kind
    account_input.validate_source(selected)


def test_asset_cannot_claim_valuation_after_recording(bound_book):
    observe(bound_book)
    saved=source(bound_book)
    saved['asset']['portfolio_value_as_of']='2099-01-01T15:00:00+08:00'
    with pytest.raises(account_input.AccountInputError,match='晚于记录'):account_input.validate_source(saved)
