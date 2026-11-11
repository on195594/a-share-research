"""Stop transport and ledger contracts; structural fixture values are not financial evidence."""
from copy import deepcopy
from datetime import datetime

from fastapi.testclient import TestClient
import pytest

from a_share_research import api_keys, gate
from a_share_research.policy import monitor, registry, stop_alert, stop_evaluation
from a_share_research.tools.server import create_app
from test_policy_monitor import setup as monitor_setup
from test_policy_account_input import bound_book as ledger_book
from test_policy_capture import scoped_parameters
from test_stop_evaluation import bundle as stop_bundle


@pytest.fixture
def setup(tmp_path, monkeypatch):
    yield from monitor_setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def configured(setup, monkeypatch):
    path, conn, principals, manifest, activation, _, _ = setup
    calls = []
    def collect(database, module, payload):
        assert database == path and module == 'a_share_research.policy.stop_alert'
        assert not conn.in_transaction
        calls.append(deepcopy(payload))
        value = stop_bundle(state=payload['previous_state'])
        value['risk_policy'] = payload['parameters']
        return value
    monkeypatch.setattr(monitor, '_capture', collect)
    return setup, calls


def run(configured, request='stop-once', state=None):
    (path, conn, principals, _, activation, _, _), _ = configured
    return stop_alert.run(conn, principals['assistant'], path, request, activation['id'], {} if state is None else state)


def test_receipt_binds_identity_input_state_source_and_retries_once(configured):
    setup, calls = configured
    path, conn, principals, manifest, activation, _, _ = setup
    result = run(configured)
    assert result['mode'] == 'service_stop_alert_capture'
    assert result['identity'] == manifest['identity'] and result['activation_id'] == activation['id']
    assert result['capture_context']['schema_version'] == 3
    assert result['capture_context']['source']['mode'] == 'mirror'
    assert result['result'] == stop_evaluation.evaluate(result['input'], manifest['parameters'])
    assert result['result']['alerts'] == [{'code': '000963', 'level': 'stop1'}]
    assert run(configured) == result and len(calls) == 1
    assert registry.evaluation(conn, principals['assistant'], result['id']) == result
    assert registry.inspect(conn) == []
    with pytest.raises(registry.PolicyError, match='不同'):
        run(configured, state=result['result']['state'])
    with pytest.raises(registry.PolicyError, match='不同'):
        monitor.run(conn, principals['assistant'], path, 'stop-once', activation['id'])
    next_result = run(configured, 'stop-next', result['result']['state'])
    assert next_result['result']['status'] == 'no_alerts'


@pytest.mark.parametrize('change', ['source', 'state', 'revoked'])
def test_changed_source_state_or_key_cannot_commit_success(configured, monkeypatch, change):
    setup, _ = configured
    _, conn, principals, _, _, _, _ = setup
    collect = monitor._capture
    def changed(*args):
        value = collect(*args)
        if change == 'source': conn.execute("UPDATE account_source SET updated_at='changed'")
        elif change == 'state': value['previous_state'] = {'date': '2026-10-22'}
        else: conn.execute('DELETE FROM api_keys WHERE id=?', (principals['assistant'].key_id,))
        return value
    monkeypatch.setattr(monitor, '_capture', changed)
    with pytest.raises(registry.PolicyError): run(configured)
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 0


def test_http_permissions_shape_pause_and_receipt(configured):
    setup, calls = configured
    path, conn, _, _, activation, _, _ = setup
    tokens = {role: api_keys.create(conn, 'stop-http-' + role, role=role) for role in ('assistant', 'research')}
    payload = dict(request_id='http-stop', expected_activation_id=activation['id'], previous_state={})
    headers = lambda role: {'Authorization': 'Bearer ' + tokens[role]}
    with TestClient(create_app(path)) as client:
        assert client.post('/tools/policy.stop_alert', headers=headers('assistant'), json=payload).status_code == 404
    app = create_app(path, account_shadow_enabled=True)
    with TestClient(app) as client:
        assert client.post('/tools/policy.stop_alert', headers=headers('research'), json=payload).status_code == 403
        assert client.post('/tools/policy.stop_alert', headers=headers('assistant'),
                           json=payload | {'previous_state': {'date': '2026-02-30'}}).status_code == 422
        first = client.post('/tools/policy.stop_alert', headers=headers('assistant'), json=payload)
        assert first.status_code == 200, first.text
        assert client.post('/tools/policy.stop_alert', headers=headers('assistant'), json=payload).json() == first.json()
        assert len(calls) == 1
        app.state.gate.pause(1)
        assert client.post('/tools/policy.stop_alert', headers=headers('assistant'), json=payload).status_code == 503


def test_context_action_state_and_source_corruption_rejected(configured):
    setup, _ = configured
    _, conn, principals, _, _, _, _ = setup
    result = run(configured)
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    context = deepcopy(result['capture_context'])
    context['request']['previous_state'] = {'levels': {'000963': 'stop2'}}
    conn.execute('UPDATE policy_evaluations SET capture_context=?,capture_context_hash=?',
                 (gate.canonical(context), gate.digest(context)))
    with pytest.raises(registry.PolicyError, match='损坏'):
        registry.evaluation(conn, principals['assistant'], result['id'])
    assert registry.inspect(conn)


def test_shipping_child_on_empty_account_never_needs_market_data(setup):
    path, conn, principals, _, activation, _, _ = setup
    receipt = stop_alert.run(conn, principals['assistant'], path, 'actual-empty-child', activation['id'], {})
    assert receipt['input']['holdings'] == [] and receipt['input']['quotes'] == {}
    assert receipt['result']['status'] in ('no_holdings', 'outside_session')
    assert receipt['result']['exit_code'] == 0 and receipt['result']['stdout'] == ''
    assert registry.inspect(conn) == []


def test_actual_ledger_collector_reads_canonical_lines_without_writing_state(tmp_path, monkeypatch):
    from a_share_research.policy.v0 import domain, market_quotes
    for path, conn, principal, directory, sha in ledger_book.__wrapped__(tmp_path):
        monkeypatch.setenv('POLICY_V0_DB', str(path))
        monkeypatch.setattr(domain, 'cst_now', lambda: datetime.fromisoformat('2026-11-02T10:31:00+08:00'))
        calls = []
        def quotes(codes):
            calls.append(codes)
            return {code: market_quotes.PriceQuote(8.4, '2026-11-02', '10:30:00', 'structural') for code in codes}
        monkeypatch.setattr(market_quotes, 'fetch_current_price_quotes', quotes)
        source = monitor._source(conn, path.parent)
        before = list(conn.iterdump())
        value = stop_alert.collect(scoped_parameters(), {}, source=source)
        assert calls == [['600001']]
        assert value['holdings'][0]['stop_loss_15'] == 8.5
        assert stop_evaluation.evaluate(value, scoped_parameters())['status'] == 'alerts'
        assert list(conn.iterdump()) == before
        assert not (tmp_path / 'stop-alert-state.json').exists()
        monkeypatch.setattr(domain, 'cst_now', lambda: datetime.fromisoformat('2026-11-02T12:00:00+08:00'))
        assert stop_alert.collect(scoped_parameters(), {}, source=source)['quotes'] == {}
        assert len(calls) == 1
