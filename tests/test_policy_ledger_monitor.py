"""Ledger collector/persistence contracts use invented structural accounts, never broker evidence."""
import json

import pytest

from a_share_research.policy import capture, monitor, registry
from support.policy import observe, scoped_parameters
from support.policy import run_monitor as run


def test_ledger_collection_reads_scoped_projection_and_never_legacy_asset_file(ledger_monitor,monkeypatch):
    configured = ledger_monitor
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


def test_ledger_asset_observation_is_not_a_valuation_timestamp(ledger_monitor):
    configured = ledger_monitor
    book,_,_,_=configured
    observe(book)
    receipt=run(configured)
    assert receipt['input']['portfolio_value']==11200.
    denominator=receipt['input']['denominator_evidence']
    assert denominator['portfolio_value_source']=='account_observation'
    assert denominator['portfolio_value_as_of'] is None
    assert denominator['denominator_freshness_status']=='unknown'


def test_observation_during_network_collection_rejects_even_when_book_version_unchanged(ledger_monitor,monkeypatch):
    configured = ledger_monitor
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


def test_wrong_bound_scope_rejects_before_collection(ledger_monitor):
    configured = ledger_monitor
    book,_,_,calls=configured
    _,conn,_,_,_=book
    binding=json.loads(conn.execute('SELECT watermark FROM account_source').fetchone()[0])
    binding['account_scope']='another-scope'
    conn.execute('UPDATE account_source SET watermark=?',(json.dumps(binding),))
    with pytest.raises(registry.PolicyError,match='账户范围'):run(configured)
    assert calls==[]


def test_child_detects_source_change_and_restores_projection_environment(ledger_monitor,monkeypatch):
    configured = ledger_monitor
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


def test_saved_ledger_context_remains_valid_after_later_observation(ledger_monitor):
    configured = ledger_monitor
    receipt=run(configured)
    book,_,_,_=configured
    observe(book)
    assert registry.inspect(book[1])==[]
    assert run(configured)==receipt


def test_recorded_valuation_time_reaches_collector_and_saved_evaluation(ledger_monitor):
    configured = ledger_monitor
    from a_share_research.account import ledger
    book,_,_,_=configured
    _,conn,principal,directory,sha=book
    stamp='2026-11-01T15:00:00+08:00'
    ledger.observe(conn,principal,'a',ledger.Observation(observed_at='2026-11-01T16:00:00+08:00',
        total_assets='11200',portfolio_value_as_of=stamp),directory,sha)
    receipt=run(configured)
    assert receipt['capture_context']['source']['asset']['portfolio_value_as_of']==stamp
    assert receipt['input']['denominator_evidence']['portfolio_value_as_of']==stamp
    assert receipt['input']['denominator_evidence']['denominator_freshness_status']=='as_of_provided'
    assert registry.evaluation(conn,principal,receipt['id'])==receipt
    assert registry.inspect(conn)==[]
