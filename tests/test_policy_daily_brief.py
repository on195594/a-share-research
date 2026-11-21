"""Daily-brief service contracts with structural accounts; no upstream requests."""
from copy import deepcopy
from datetime import datetime
import json

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from a_share_research import api_keys, gate
from a_share_research.core.market_data import MarketDataResult
from a_share_research.policy import daily_brief, daily_evaluation, monitor, registry
from a_share_research.tools.server import create_app
from support.policy import policy_input_book as ledger_book
from support.policy import scoped_parameters


@pytest.fixture
def configured(policy_setup, monkeypatch):
    setup = policy_setup
    path, conn, _, _, _, bundle, _ = setup
    calls = []
    def capture(database, module, payload, *, market_token):
        assert database == path and module == 'a_share_research.policy.daily_brief' and market_token is True
        assert not conn.in_transaction
        calls.append(deepcopy(payload))
        return deepcopy(bundle) | dict(research_result={'status': 'not_configured'}, dividends=[],
                                      dividend_today=bundle['now'][:10], brief_today=bundle['now'][:10])
    monkeypatch.setattr(monitor, '_capture', capture)
    return setup, calls


def run(configured, request='daily-once'):
    (path, conn, principals, _, active, _, _), _ = configured
    return daily_brief.run(conn, principals['assistant'], path, request, active['id'])


def test_saved_daily_input_result_identity_and_retry(configured):
    setup, calls = configured
    path, conn, principals, manifest, active, _, _ = setup
    saved = run(configured)
    assert saved['mode'] == 'service_daily_brief_capture'
    assert saved['identity'] == manifest['identity'] and saved['activation_id'] == active['id']
    assert saved['capture_context']['schema_version'] == 4
    assert saved['result'] == daily_evaluation.evaluate(saved['input'], manifest['parameters'])
    assert saved['result']['status'] == 'silent' and saved['result']['dividends']['checked'] == 0
    assert run(configured) == saved and len(calls) == 1
    assert registry.evaluation(conn, principals['assistant'], saved['id']) == saved
    assert registry.inspect(conn) == []
    with pytest.raises(registry.PolicyError, match='不同'):
        monitor.run(conn, principals['assistant'], path, 'daily-once', active['id'])


def test_http_validates_daily_brief_arguments(configured):
    path, conn, _, _, active, _, _ = configured[0]
    token = api_keys.create(conn, 'daily_brief-http', role='assistant')
    args = dict(request_id='daily-http', expected_activation_id=active['id'])
    with TestClient(create_app(path, account_shadow_enabled=True)) as client:
        headers = {'Authorization': 'Bearer ' + token}
        endpoint = '/tools/policy_daily_brief'
        assert client.post(endpoint, headers=headers, json=args | {'token': 'no'}).status_code == 422
        response = client.post(endpoint, headers=headers, json=args)
        assert response.status_code == 200, response.text


def test_tampered_daily_brief_action_is_rejected(configured):
    setup, _ = configured
    _, conn, principals, _, _, _, _ = setup
    saved = run(configured)
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    context = deepcopy(saved['capture_context'])
    context['request']['action'] = 'policy.monitor'
    conn.execute('UPDATE policy_evaluations SET capture_context=?,capture_context_hash=?',
                 (gate.canonical(context), gate.digest(context)))
    with pytest.raises(registry.PolicyError, match='损坏'):
        registry.evaluation(conn, principals['assistant'], saved['id'])
    assert registry.inspect(conn)


def test_provider_evidence_preserves_null_cells_without_rounding_and_sanitizes_errors(monkeypatch):
    from a_share_research.core import providers
    amount = 0.1234567890123456  # Explicit precision boundary variant, not a financial assertion.
    frame = pd.DataFrame([dict(div_proc='实施', record_date='2026-09-17', ex_date=None,
                              pay_date=None, cash_div_tax=amount, stk_div=float('nan'))])
    response = MarketDataResult(frame, 'ok', 'tushare.dividend', '2026-11-11T15:35:00+08:00')
    class Provider:
        def __init__(self, **kwargs): pass
        def fetch_dividend_history(self, code): return response
    monkeypatch.setattr(providers, 'TushareDividendProvider', Provider)
    captured = daily_brief._dividend_response('000963')
    assert captured['rows'][0]['cash_div_tax'] == amount and captured['rows'][0]['stk_div'] is None
    assert captured['fetched_at'] == response.fetched_at
    def fail(*args): raise RuntimeError('secret-provider-response')
    monkeypatch.setattr(Provider, 'fetch_dividend_history', fail)
    failed = daily_brief._dividend_response('000963')
    assert failed['rows'] is None and failed['error_type'] == 'RuntimeError'
    assert 'secret' not in json.dumps(failed)


def test_ledger_capture_keeps_full_event_rows_and_provider_failure(tmp_path, monkeypatch):
    from a_share_research.policy import capture
    from a_share_research.policy.v0 import domain, commands_holdings
    for path, conn, principal, directory, sha in ledger_book(tmp_path):
        monkeypatch.setenv('POLICY_V0_DB', str(path))
        now = datetime.fromisoformat('2026-11-02T15:35:00+08:00')
        monkeypatch.setattr(domain, 'cst_now', lambda: now)
        monkeypatch.setattr(domain, 'cst_today', lambda: now.date().isoformat())
        monkeypatch.setattr(commands_holdings, 'fetch_monitor_price_quotes', lambda codes: ({c: None for c in codes}, None))
        monkeypatch.setattr(capture.monitor_research, 'fetch_cards', lambda: pytest.fail('daily must not acquire research'))
        calls = []
        def unavailable(code):
            calls.append(code)
            return dict(rows=None, source='tushare.dividend', status='failed', fetched_at=now.isoformat(),
                        source_as_of=None, error_code='AUTH_MISSING', error_type=None)
        monkeypatch.setattr(daily_brief, '_dividend_response', unavailable)
        source = monitor._source(conn, tmp_path)
        before = list(conn.iterdump())
        value = daily_brief.collect(scoped_parameters(), source=source)
        assert calls == ['600001']
        assert value['dividends'][0]['events'][0][0:3] == ['buy', '2026-10-01', 100]
        result = daily_evaluation.evaluate(value, scoped_parameters())
        assert result['dividends']['failures'][0]['error_code'] == 'AUTH_MISSING'
        assert result['status'] == 'silent' and result['stdout'] == ''
        assert list(conn.iterdump()) == before


def test_actual_empty_child_has_no_quote_or_dividend_requests(policy_setup):
    setup = policy_setup
    path, conn, principals, _, active, _, _ = setup
    saved = daily_brief.run(conn, principals['assistant'], path, 'actual-empty-daily', active['id'])
    assert saved['input']['local']['holdings'] == [] and saved['input']['dividends'] == []
    assert saved['result']['status'] == 'silent'
