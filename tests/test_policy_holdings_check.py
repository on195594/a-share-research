"""Holdings-check transport over structural accounts; no live financial evidence."""
from copy import deepcopy
from datetime import datetime
import json

from fastapi.testclient import TestClient
import pytest

from a_share_research import api_keys, gate
from a_share_research.account import analysis, cycle_analysis
from a_share_research.policy import holdings_check, holdings_check_evaluation, monitor, registry
from a_share_research.tools.server import create_app
from test_policy_monitor import setup as monitor_setup
from test_policy_account_input import bound_book as ledger_book
from test_policy_capture import scoped_parameters


@pytest.fixture
def setup(tmp_path, monkeypatch):
    yield from monitor_setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def configured(setup, monkeypatch):
    path, conn, _, _, _, bundle, _ = setup
    calls = []
    def capture(database, module, payload, *, market_token=False):
        assert database == path and module == 'a_share_research.policy.holdings_check'
        assert market_token is False and not conn.in_transaction
        calls.append(deepcopy(payload))
        return dict(risk_policy=deepcopy(bundle['risk_policy']), now=bundle['now'], holdings=[],
                    defers=[], quotes={}, fundamentals={}, market_snapshot=None)
    monkeypatch.setattr(monitor, '_capture', capture)
    return setup, calls


def run(configured, request='holdings-check-once'):
    (path, conn, principals, _, active, _, _), _ = configured
    return holdings_check.run(conn, principals['assistant'], path, request, active['id'])


def account_rows(conn):
    return {row[0]: [tuple(item) for item in conn.execute('SELECT * FROM "' + row[0] + '"')]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'account_%'")}


def test_saved_receipt_hashes_replay_retry_and_no_account_write(configured):
    setup, calls = configured
    path, conn, principals, manifest, active, _, _ = setup
    before = account_rows(conn)
    saved = run(configured)
    assert saved['mode'] == 'service_holdings_check_capture'
    assert saved['capture_context']['schema_version'] == 8
    assert saved['identity'] == manifest['identity'] and saved['activation_id'] == active['id']
    assert saved['input_hash'] == gate.digest(saved['input']) and saved['result_hash'] == gate.digest(saved['result'])
    assert saved['result'] == holdings_check_evaluation.evaluate(saved['input'], manifest['parameters'])
    assert saved['result']['status'] == 'no_holdings' and saved['result']['exit_code'] == 0
    assert saved['result']['stderr'] == '' and saved['result']['checks'] == []
    assert run(configured) == saved and len(calls) == 1
    assert registry.evaluation(conn, principals['assistant'], saved['id']) == saved
    assert registry.inspect(conn) == [] and account_rows(conn) == before
    with pytest.raises(registry.PolicyError, match='不同'):
        monitor.run(conn, principals['assistant'], path, saved['request_id'], active['id'])


def test_http_permissions_strict_args_pause_and_retry(configured):
    setup, calls = configured
    path, conn, _, _, active, _, _ = setup
    tokens = {role: api_keys.create(conn, 'holdings-check-' + role, role=role) for role in ('assistant', 'research')}
    args = dict(request_id='holdings-check-http', expected_activation_id=active['id'])
    headers = lambda role: {'Authorization': 'Bearer ' + tokens[role]}
    endpoint = '/tools/policy.holdings_check'
    with TestClient(create_app(path)) as client:
        assert client.post(endpoint, headers=headers('assistant'), json=args).status_code == 404
    app = create_app(path, account_shadow_enabled=True)
    with TestClient(app) as client:
        assert client.post(endpoint, headers=headers('research'), json=args).status_code == 403
        for changed in (args | {'token': 'forbidden'}, args | {'expected_activation_id': True}, args | {'request_id': ''}):
            assert client.post(endpoint, headers=headers('assistant'), json=changed).status_code == 422
        first = client.post(endpoint, headers=headers('assistant'), json=args)
        assert first.status_code == 200, first.text
        assert client.post(endpoint, headers=headers('assistant'), json=args).json() == first.json()
        assert len(calls) == 1
        app.state.gate.pause(1)
        assert client.post(endpoint, headers=headers('assistant'), json=args).status_code == 503


def test_source_change_and_tampered_action_are_not_success(configured, monkeypatch):
    setup, _ = configured
    _, conn, principals, _, _, _, _ = setup
    original = monitor._capture
    saved = run(configured)
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        conn.execute("UPDATE account_source SET updated_at='changed-during-holdings-check'")
        return value
    monkeypatch.setattr(monitor, '_capture', changed)
    with pytest.raises(registry.PolicyError, match='来源改变'): run(configured, 'changed-source')
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    context = deepcopy(saved['capture_context']); context['request']['action'] = 'policy.monitor'
    conn.execute('UPDATE policy_evaluations SET capture_context=?,capture_context_hash=?',
                 (gate.canonical(context), gate.digest(context)))
    with pytest.raises(registry.PolicyError, match='损坏'):
        registry.evaluation(conn, principals['assistant'], saved['id'])
    assert registry.inspect(conn)


def test_fixed_collector_drops_credentials_and_uses_only_own_module(setup, monkeypatch):
    path, conn, principals, _, active, bundle, _ = setup
    for key in ('TUSHARE_TOKEN', 'ASHARE_OWNER_TOKEN', 'MODEL_API_KEY'):
        monkeypatch.setenv(key, 'must-not-inherit')
    seen = []
    class Process:
        returncode = 0
        def communicate(self, payload, **kwargs):
            value = json.loads(payload)
            assert set(value) == {'parameters', 'source'} and value['source'] is None
            return gate.canonical(dict(risk_policy=value['parameters'], now=bundle['now'], holdings=[],
                                       defers=[], quotes={}, fundamentals={}, market_snapshot=None)).encode(), b''
    def popen(argv, **kwargs):
        assert argv[-1] == 'a_share_research.policy.holdings_check'
        assert not {'TUSHARE_TOKEN', 'ASHARE_OWNER_TOKEN', 'MODEL_API_KEY'} & set(kwargs['env'])
        seen.append(argv)
        return Process()
    monkeypatch.setattr(monitor.subprocess, 'Popen', popen)
    assert holdings_check.run(conn, principals['assistant'], path, 'fixed-child', active['id'])['result']['status'] == 'no_holdings'
    assert len(seen) == 1


def test_ledger_capture_scopes_open_holdings_and_defers_without_writes(tmp_path, monkeypatch):
    from a_share_research.policy import capture, daily_brief
    from a_share_research.policy.v0 import domain, market_quotes, store
    for path, conn, principal, directory, sha in ledger_book.__wrapped__(tmp_path):
        cycle_analysis.update(conn, principal, 'a', 'legacy:1', analysis.Update(
            expected_version=0, evidence_hashes=[sha], reason='structural alert transport', changes=[
                analysis.AlertOpen(kind='alert_open', level='yellow', category='unverified',
                    reason_code='price_stop2_liquidity_defer', reason='structural pending check',
                    evidence='{}', review_due='2026-11-03')]), directory)
        monkeypatch.setenv('POLICY_V0_DB', str(path))
        now = datetime.fromisoformat('2026-11-02T15:35:00+08:00')
        monkeypatch.setattr(domain, 'cst_now', lambda: now)
        monkeypatch.setattr(domain, 'cst_today', lambda: now.date().isoformat())
        quote_calls, fundamental_calls, market_calls = [], [], []
        def quotes(codes):
            quote_calls.append(list(codes)); return {code: None for code in codes}
        def fundamentals(code):
            fundamental_calls.append(code); return None
        monkeypatch.setattr(market_quotes, 'fetch_current_price_quotes', quotes)
        monkeypatch.setattr(market_quotes, 'fetch_market_limit_down_snapshot', lambda: market_calls.append(True))
        monkeypatch.setattr(store, 'get_fundamentals', fundamentals)
        monkeypatch.setattr(capture.monitor_research, 'fetch_cards', lambda: pytest.fail('no research capture'))
        monkeypatch.setattr(daily_brief, '_dividend_response', lambda *a: pytest.fail('no dividends capture'))
        source = monitor._source(conn, tmp_path)
        before = list(conn.iterdump())
        value = holdings_check.collect(scoped_parameters(), source=source)
        assert len(value['holdings']) == 1 and len(value['holdings'][0]) == 6
        assert value['holdings'][0][1] == '600001'
        assert len(value['defers']) == 1 and len(value['defers'][0]) == 3
        assert value['defers'][0][0] == value['holdings'][0][0]
        assert value['defers'][0][1:] == ['{}', '2026-11-03']
        assert quote_calls == [['600001']] and fundamental_calls == ['600001'] and market_calls == [True]
        assert set(value['quotes']) == set(value['fundamentals']) == {'600001'}
        result = holdings_check_evaluation.evaluate(value, scoped_parameters())
        assert result['status'] == 'ready' and result['exit_code'] == 0 and len(result['checks']) == 1
        assert list(conn.iterdump()) == before


def test_actual_empty_child_does_not_request_market_data(setup):
    path, conn, principals, _, active, _, _ = setup
    before = account_rows(conn)
    saved = holdings_check.run(conn, principals['assistant'], path, 'actual-empty-holdings-check', active['id'])
    assert saved['input']['holdings'] == [] and saved['input']['defers'] == []
    assert saved['input']['quotes'] == saved['input']['fundamentals'] == {}
    assert saved['input']['market_snapshot'] is None
    assert saved['result']['status'] == 'no_holdings' and account_rows(conn) == before


def test_empty_collector_skips_all_providers(setup, monkeypatch):
    from a_share_research.policy import capture, daily_brief
    from a_share_research.policy.v0 import market_quotes, store
    path, conn, _, manifest, _, _, _ = setup
    monkeypatch.setenv('POLICY_V0_DB', str(path))
    def forbidden(*args, **kwargs): pytest.fail('empty holdings must not acquire provider data')
    monkeypatch.setattr(market_quotes, 'fetch_current_price_quotes', forbidden)
    monkeypatch.setattr(market_quotes, 'fetch_market_limit_down_snapshot', forbidden)
    monkeypatch.setattr(store, 'get_fundamentals', forbidden)
    monkeypatch.setattr(capture.monitor_research, 'fetch_cards', forbidden)
    monkeypatch.setattr(daily_brief, '_dividend_response', forbidden)
    before = list(conn.iterdump())
    value = holdings_check.collect(manifest['parameters'])
    assert value['holdings'] == [] and value['quotes'] == value['fundamentals'] == {}
    assert value['market_snapshot'] is None and list(conn.iterdump()) == before
