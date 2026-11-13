"""Structural service/ledger transport tests; fixed samples are not financial evidence."""
from copy import deepcopy
from datetime import datetime
import json

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from a_share_research import api_keys, gate
from a_share_research.core.market_data import MarketDataResult
from a_share_research.policy import benchmark, benchmark_evaluation, monitor, registry
from a_share_research.tools.server import create_app
from test_benchmark_evaluation import bundle as structural_bundle
from test_policy_monitor import setup as monitor_setup
from test_policy_account_input import bound_book as ledger_book
from test_policy_capture import scoped_parameters


@pytest.fixture
def setup(tmp_path, monkeypatch):
    yield from monitor_setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def configured(setup, monkeypatch):
    path, conn, _, _, _, original, _ = setup
    calls = []
    def capture(database, module, payload, *, market_token):
        assert database == path and module == 'a_share_research.policy.benchmark' and market_token is True
        assert not conn.in_transaction
        calls.append(deepcopy(payload))
        value = structural_bundle()
        value.update(risk_policy=deepcopy(payload['parameters']), now=original['now'], end=payload['end'])
        return value
    monkeypatch.setattr(monitor, '_capture', capture)
    return setup, calls


def run(configured, request='benchmark-once', end='2026-10-22'):
    (path, conn, principals, _, active, _, _), _ = configured
    return benchmark.run(conn, principals['assistant'], path, request, active['id'], end)


def test_saved_benchmark_identity_end_hashes_and_retry(configured):
    setup, calls = configured
    path, conn, principals, manifest, active, _, _ = setup
    saved = run(configured)
    assert saved['mode'] == 'service_benchmark_capture'
    assert saved['identity'] == manifest['identity'] and saved['activation_id'] == active['id']
    assert saved['capture_context']['schema_version'] == 5
    assert saved['capture_context']['request'] == dict(action='policy.benchmark', end='2026-10-22', expected_activation_id=active['id'])
    assert saved['input']['end'] == '2026-10-22'
    assert saved['input_hash'] == gate.digest(saved['input'])
    assert saved['result_hash'] == gate.digest(saved['result'])
    assert saved['result'] == benchmark_evaluation.evaluate(saved['input'], manifest['parameters'])
    assert saved['result']['status'] == 'ready' and saved['result']['metrics']['pnl'] == 2787
    assert run(configured) == saved and len(calls) == 1
    assert registry.evaluation(conn, principals['assistant'], saved['id']) == saved
    assert registry.inspect(conn) == []
    with pytest.raises(registry.PolicyError, match='不同'):
        run(configured, end='2026-10-23')
    with pytest.raises(registry.PolicyError, match='不同'):
        monitor.run(conn, principals['assistant'], path, 'benchmark-once', active['id'])
    assert len(calls) == 1


def test_http_permissions_arguments_pause_and_retry(configured):
    setup, calls = configured
    path, conn, _, _, active, _, _ = setup
    tokens = {role: api_keys.create(conn, 'benchmark-' + role, role=role) for role in ('assistant', 'research')}
    args = dict(request_id='benchmark-http', expected_activation_id=active['id'], end='2026-10-22')
    headers = lambda role: {'Authorization': 'Bearer ' + tokens[role]}
    with TestClient(create_app(path)) as client:
        assert client.post('/tools/policy.benchmark', headers=headers('assistant'), json=args).status_code == 404
    app = create_app(path, account_shadow_enabled=True)
    with TestClient(app) as client:
        assert client.post('/tools/policy.benchmark', headers=headers('research'), json=args).status_code == 403
        for changed in (args | {'token': 'no'}, args | {'end': '2026-02-30'}, args | {'end': '20261022'}, args | {'end': None}):
            assert client.post('/tools/policy.benchmark', headers=headers('assistant'), json=changed).status_code == 422
        first = client.post('/tools/policy.benchmark', headers=headers('assistant'), json=args)
        assert first.status_code == 200, first.text
        assert client.post('/tools/policy.benchmark', headers=headers('assistant'), json=args).json() == first.json()
        assert len(calls) == 1
        app.state.gate.pause(1)
        assert client.post('/tools/policy.benchmark', headers=headers('assistant'), json=args).status_code == 503


def test_changed_source_and_corrupt_request_end_are_detected(configured, monkeypatch):
    setup, _ = configured
    _, conn, principals, _, _, _, _ = setup
    original = monitor._capture
    saved = run(configured)
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        conn.execute("UPDATE account_source SET updated_at='changed'")
        return value
    monkeypatch.setattr(monitor, '_capture', changed)
    with pytest.raises(registry.PolicyError, match='来源改变'):
        run(configured, 'another')
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    context = deepcopy(saved['capture_context'])
    context['request']['end'] = '2026-10-23'
    conn.execute('UPDATE policy_evaluations SET capture_context=?,capture_context_hash=?', (gate.canonical(context), gate.digest(context)))
    with pytest.raises(registry.PolicyError, match='损坏'):
        registry.evaluation(conn, principals['assistant'], saved['id'])
    assert registry.inspect(conn)


def test_capture_end_mismatch_does_not_write(configured, monkeypatch):
    setup, _ = configured
    original = monitor._capture
    def wrong(*args, **kwargs):
        return original(*args, **kwargs) | {'end': '2026-10-23'}
    monkeypatch.setattr(monitor, '_capture', wrong)
    with pytest.raises(registry.PolicyError):
        run(configured)
    assert setup[1].execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 0


def test_response_preserves_precision_nulls_and_sanitizes_exception():
    amount = 0.1234567890123456  # Precision boundary only.
    frame = pd.DataFrame([dict(date='2026-10-21', close=amount, optional=float('nan'))])
    response = MarketDataResult(frame, 'ok', 'tushare.index_daily', '2026-11-11T15:35:00+08:00')
    captured = benchmark._response(lambda: response, kind='index', source='tushare.index_daily')
    assert captured['rows'] == [dict(date='2026-10-21', close=amount, optional=None)]
    assert captured['fetched_at'] == response.fetched_at
    price = benchmark._response(lambda: MarketDataResult(amount, 'ok', 'tushare.daily', response.fetched_at), kind='stock', source='tushare.daily')
    assert price['value'] == amount
    def fail(): raise RuntimeError('secret-provider-response')
    for kind, field in [('index', 'rows'), ('stock', 'value')]:
        failed = benchmark._response(fail, kind=kind, source='fixture')
        assert failed[field] is None and failed['error_type'] == 'RuntimeError'
        assert failed['status'] == 'failed' and 'secret' not in json.dumps(failed)


def test_only_allowed_collector_receives_market_credential(tmp_path, monkeypatch):
    monkeypatch.setenv('TUSHARE_TOKEN', 'structural-market-token')
    monkeypatch.setenv('ASHARE_OWNER_TOKEN', 'must-not-inherit')
    monkeypatch.setenv('MODEL_API_KEY', 'must-not-inherit')
    envs = []
    class Process:
        returncode = 0
        def communicate(self, *args, **kwargs): return b'{}', b''
    def popen(argv, **kwargs):
        envs.append(kwargs['env'])
        return Process()
    monkeypatch.setattr(monitor.subprocess, 'Popen', popen)
    monitor._capture(tmp_path / 'app.db', 'a_share_research.policy.benchmark', {}, market_token=True)
    monitor.capture(tmp_path / 'app.db', {})
    assert envs[0]['TUSHARE_TOKEN'] == 'structural-market-token'
    assert envs[1]['TUSHARE_TOKEN'] == 'structural-market-token'
    monitor._capture(tmp_path / 'app.db', 'a_share_research.policy.stop_alert', {})
    assert 'TUSHARE_TOKEN' not in envs[2]
    assert all('ASHARE_OWNER_TOKEN' not in e and 'MODEL_API_KEY' not in e for e in envs)
    with pytest.raises(ValueError):
        monitor._capture(tmp_path / 'app.db', 'a_share_research.policy.stop_alert', {}, market_token=True)


@pytest.mark.parametrize('index_mode', ['failed', 'before-first-event'])
def test_collector_keeps_all_history_and_skips_stocks_without_valuation(tmp_path, monkeypatch, index_mode):
    from a_share_research.core import providers
    from a_share_research.policy.v0 import domain
    for path, conn, principal, directory, sha in ledger_book.__wrapped__(tmp_path):
        monkeypatch.setenv('POLICY_V0_DB', str(path))
        now = datetime.fromisoformat('2026-11-02T15:35:00+08:00')
        monkeypatch.setattr(domain, 'cst_now', lambda: now)
        monkeypatch.setattr(domain, 'cst_today', lambda: now.date().isoformat())
        calls = []
        class Provider:
            def __init__(self, **kwargs): pass
            def fetch_index_bars(self, code, first, end):
                calls.append((code, first, end))
                if index_mode == 'failed':
                    return MarketDataResult(None, 'failed', 'fixture', now.isoformat(), error_code='AUTH_MISSING')
                frame = pd.DataFrame([dict(date='2026-08-31', close=100.)])
                return MarketDataResult(frame, 'ok', 'fixture', now.isoformat())
            def fetch_close_price(self, *args): pytest.fail('cannot fetch stock without valuation')
        monkeypatch.setattr(providers, 'TushareMarketDataProvider', Provider)
        source = monitor._source(conn, tmp_path)
        before = list(conn.iterdump())
        value = benchmark.collect(scoped_parameters(), '2026-11-02', source=source)
        assert [(r[0], r[1], r[2]) for r in value['events']] == [
            ('600002', 'buy', '2026-09-01'), ('600002', 'sell', '2026-09-30'), ('600001', 'buy', '2026-10-01')]
        assert len(calls) == 2 and all(c[1:] == ('2026-09-01', '2026-11-02') for c in calls)
        assert value['prices'] == {}
        result = benchmark_evaluation.evaluate(value, scoped_parameters())
        assert result['status'] == 'unavailable'
        assert result['failures'][0]['kind'] == ('index' if index_mode == 'failed' else 'calculation')
        assert list(conn.iterdump()) == before


def test_actual_empty_child_no_provider_calls(setup):
    path, conn, principals, _, active, _, _ = setup
    saved = benchmark.run(conn, principals['assistant'], path, 'actual-empty-benchmark', active['id'], '2026-10-22')
    assert saved['input']['events'] == [] and saved['input']['indices'] == {} and saved['input']['prices'] == {}
    assert saved['result']['status'] == 'no_events'
    assert saved['result']['stderr'] == '错误：账本没有交易事件\n'
