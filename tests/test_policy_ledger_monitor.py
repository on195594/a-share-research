"""Ledger collector/persistence contracts use invented structural accounts, never broker evidence."""
import json

import pytest

from a_share_research import api_keys
from a_share_research.policy import capture, monitor, registry
from test_policy_account_input import bound_book as input_book, observe
from test_policy_capture import NOW, scoped_parameters


@pytest.fixture
def bound_book(tmp_path):
    yield from input_book.__wrapped__(tmp_path)


@pytest.fixture
def configured(bound_book, monkeypatch):
    path,conn,principal,directory,sha=bound_book
    owner=api_keys.authenticate(conn,api_keys.create(conn,'ledger-monitor-owner',role='owner'))
    manifest=registry.register(conn,owner,scoped_parameters())
    active=registry.activate(conn,owner,manifest['identity'],expected_id=0,
        request_id='ledger-monitor-activate',reason='structural ledger collector')
    monkeypatch.setenv('POLICY_V0_DB',str(path))
    monkeypatch.delenv('POLICY_V0_ACCOUNT_SHADOW',raising=False)
    monkeypatch.delenv('POLICY_V0_ANALYSIS_SHADOW',raising=False)
    monkeypatch.setattr(capture.domain,'cst_now',lambda:NOW)
    monkeypatch.setattr(capture.monitor_research,'fetch_cards',lambda:{'status':'not_configured','cards':[]})
    monkeypatch.setattr(capture.commands_holdings,'fetch_monitor_price_quotes',lambda codes:({code:None for code in codes},None))
    calls=[]
    def collect(database,parameters,*,source):
        assert not conn.in_transaction
        calls.append(source)
        return capture.collect(parameters,source=source)
    monkeypatch.setattr(monitor,'capture',collect)
    return bound_book,owner,active,calls


def run(configured,request_id='ledger-capture'):
    book,_,active,_=configured
    path,conn,principal,_,_=book
    return monitor.run(conn,principal,path,request_id,active['id'])


def test_ledger_collection_reads_scoped_projection_and_never_legacy_asset_file(configured,monkeypatch):
    book,_,_,calls=configured
    path,conn,principal,_,_=book
    (path.parent/'account-snapshot.json').write_text('deliberately invalid old asset file')
    def forbidden():pytest.fail('ledger must not read legacy asset file')
    monkeypatch.setattr(capture.risk_policy,'_load_account_snapshot',forbidden)
    receipt=run(configured)
    assert len(receipt['input']['local']['holdings'])==1
    assert receipt['input']['portfolio_value'] is None
    assert receipt['capture_context']['schema_version']==2
    assert receipt['capture_context']['source']['mode']=='ledger'
    assert receipt['capture_context']['source']['watermark']['account_id']=='a'
    assert run(configured)==receipt and len(calls)==1
    assert registry.evaluation(conn,principal,receipt['id'])==receipt
    assert registry.inspect(conn)==[]
    assert 'POLICY_V0_ACCOUNT_SHADOW' not in capture.os.environ
    assert 'POLICY_V0_ANALYSIS_SHADOW' not in capture.os.environ


def test_ledger_asset_observation_is_not_a_valuation_timestamp(configured):
    book,_,_,_=configured
    observe(book)
    receipt=run(configured)
    assert receipt['input']['portfolio_value']==11200.
    denominator=receipt['input']['denominator_evidence']
    assert denominator['portfolio_value_source']=='account_observation'
    assert denominator['portfolio_value_as_of'] is None
    assert denominator['denominator_freshness_status']=='unknown'


def test_observation_during_network_collection_rejects_even_when_book_version_unchanged(configured,monkeypatch):
    book,_,_,_=configured
    _,conn,_,_,_=book
    original=monitor.capture
    def changed(*args,**kwargs):
        bundle=original(*args,**kwargs)
        observe(book)
        assert conn.execute("SELECT version FROM account_books WHERE account_id='a'").fetchone()[0]==0
        return bundle
    monkeypatch.setattr(monitor,'capture',changed)
    with pytest.raises(registry.PolicyError,match='账户来源改变'):run(configured)
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0]==0


def test_wrong_bound_scope_rejects_before_collection(configured):
    book,_,_,calls=configured
    _,conn,_,_,_=book
    binding=json.loads(conn.execute('SELECT watermark FROM account_source').fetchone()[0])
    binding['account_scope']='another-scope'
    conn.execute('UPDATE account_source SET watermark=?',(json.dumps(binding),))
    with pytest.raises(registry.PolicyError,match='账户范围'):run(configured)
    assert calls==[]


def test_child_detects_source_change_and_restores_projection_environment(configured,monkeypatch):
    book,_,_,_=configured
    _,conn,_,_,_=book
    source=monitor._source(conn,book[0].parent)
    monkeypatch.setenv('POLICY_V0_ACCOUNT_SHADOW','previous-account')
    monkeypatch.setenv('POLICY_V0_ANALYSIS_SHADOW','previous-analysis')
    def quotes(codes):
        assert capture.os.environ['POLICY_V0_ACCOUNT_SHADOW']=='a'
        assert capture.os.environ['POLICY_V0_ANALYSIS_SHADOW']=='1'
        observe(book)
        return {code:None for code in codes},None
    monkeypatch.setattr(capture.commands_holdings,'fetch_monitor_price_quotes',quotes)
    with pytest.raises(capture.CaptureError,match='source changed'):
        capture.collect(scoped_parameters(),source=source)
    assert capture.os.environ['POLICY_V0_ACCOUNT_SHADOW']=='previous-account'
    assert capture.os.environ['POLICY_V0_ANALYSIS_SHADOW']=='previous-analysis'


def test_saved_ledger_context_remains_valid_after_later_observation(configured):
    receipt=run(configured)
    book,_,_,_=configured
    observe(book)
    assert registry.inspect(book[1])==[]
    assert run(configured)==receipt
