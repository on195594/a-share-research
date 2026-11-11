"""Derived scenario transport contracts; fixture scenarios are NOT financial evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from a_share_research import api_keys, gate
from a_share_research.policy import evaluation, monitor, portfolio_scenario, position_sizing, registry
from a_share_research.policy.v0.portfolio_scenario import compare
from a_share_research.tools.server import create_app
from test_policy_monitor import setup as monitor_setup

PROPOSAL = dict(sales={'600000': 100}, fees=5.0)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    yield from monitor_setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def parent(setup):
    path, conn, principals, _, active, _, _ = setup
    return monitor.run(conn, principals['assistant'], path, 'scenario-parent', active['id'])


def run(setup, parent, request='scenario', **kwargs):
    path, conn, principals, _, active, _, _ = setup
    return portfolio_scenario.run(conn, principals['assistant'], path, request,
                                  kwargs.pop('expected', active['id']), kwargs.pop('reference', parent['id']),
                                  kwargs.pop('proposal', PROPOSAL))


def account_rows(conn):
    return {row[0]: [tuple(item) for item in conn.execute('SELECT * FROM "' + row[0] + '"')]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'account_%'")}


def test_unavailable_reference_hashes_idempotence_no_capture_or_account_write(setup, parent, monkeypatch):
    monkeypatch.setattr(monitor, '_capture', lambda *a, **k: pytest.fail('scenario cannot capture inputs'))
    monkeypatch.setattr(monitor, 'capture', lambda *a, **k: pytest.fail('scenario cannot capture inputs'))
    _, conn, principals, _, active, _, _ = setup
    before = account_rows(conn)
    original = deepcopy(parent)
    saved = run(setup, parent)
    assert saved['mode'] == 'derived_portfolio_scenario'
    assert saved['capture_context']['schema_version'] == 7
    assert saved['input']['monitor_evaluation'] == dict(id=parent['id'], input_hash=parent['input_hash'], result_hash=parent['result_hash'])
    assert saved['input']['snapshot'] == parent['result']
    assert saved['input']['proposal'] == PROPOSAL | {'cash': None}
    assert saved['input_hash'] == gate.digest(saved['input'])
    assert saved['result_hash'] == gate.digest(saved['result'])
    assert saved['result']['status'] == 'unavailable' and saved['result']['exit_code'] == 1
    assert saved['result']['stderr'] and saved['result']['stdout'] == ''
    assert run(setup, parent) == saved
    assert registry.evaluation(conn, principals['assistant'], saved['id']) == saved
    assert registry.inspect(conn) == []
    assert parent == original and account_rows(conn) == before
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 2
    for changed in (dict(proposal=PROPOSAL | {'fees': 6.0}), dict(reference=parent['id']+1), dict(expected=active['id']+1)):
        with pytest.raises(registry.PolicyError, match='不同'):
            run(setup, parent, **changed)


def test_ready_captured_snapshot_transport_matches_pure_comparison(setup, monkeypatch):
    # Transport injection only: saved no_action fixture remains unchanged; no live account claim.
    snapshot = json.loads((Path(__file__).parent / 'policy_v0/fixtures/monitor/agent-behavior-snapshots.json').read_text())['no_action']
    original = deepcopy(snapshot)
    assert snapshot['account']['risk_policy'] == setup[3]['parameters']
    monkeypatch.setattr(evaluation, 'evaluate', lambda *a: deepcopy(snapshot))
    path, conn, principals, _, active, _, _ = setup
    before = account_rows(conn)
    parent = monitor.run(conn, principals['assistant'], path, 'captured-scenario', active['id'])
    saved = run(setup, parent)
    assert saved['result']['status'] == 'ready' and saved['result']['exit_code'] == 0
    assert json.loads(saved['result']['stdout']) == compare(snapshot, PROPOSAL['sales'], fees=PROPOSAL['fees'])
    assert saved['input']['snapshot'] == original and snapshot == original
    assert account_rows(conn) == before and registry.inspect(conn) == []


def test_cross_operation_request_conflict_and_invalid_parent_kinds(setup, parent):
    path, conn, principals, manifest, active, bundle, _ = setup
    sizing = position_sizing.run(conn, principals['assistant'], path, 'sizing-request', active['id'], parent['id'],
                                 dict(code='600900', price=29.5, framework='A'))
    with pytest.raises(registry.PolicyError, match='不同'):
        run(setup, parent, 'sizing-request')
    with pytest.raises(registry.PolicyError, match='服务端监控'):
        run(setup, sizing, 'derived-parent')
    replay = registry.replay(conn, principals['assistant'], manifest['identity'], 'scenario-replay', bundle)
    with pytest.raises(registry.PolicyError, match='服务端监控'):
        run(setup, replay, 'replay-parent')
    saved = run(setup, parent)
    with pytest.raises(registry.PolicyError, match='不同'):
        position_sizing.run(conn, principals['assistant'], path, saved['request_id'], active['id'], parent['id'],
                            dict(code='600900', price=29.5, framework='A'))


def test_source_and_policy_drift_preserve_saved_retry(setup, parent):
    _, conn, principals, manifest, active, _, _ = setup
    saved = run(setup, parent)
    conn.execute("UPDATE account_source SET updated_at='scenario-drift'")
    with pytest.raises(registry.PolicyError, match='来源'):
        run(setup, parent, 'new-source')
    new = registry.activate(conn, principals['owner'], manifest['identity'], expected_id=active['id'],
                            request_id='scenario-reactivate', reason='transport fixture')
    assert run(setup, parent) == saved
    assert registry.evaluation(conn, principals['assistant'], saved['id']) == saved
    with pytest.raises(registry.PolicyError, match='生效政策'):
        run(setup, parent, 'old-activation')
    with pytest.raises(registry.PolicyError, match='服务端监控'):
        run(setup, parent, 'new-activation', expected=new['id'])
    assert registry.inspect(conn) == []


def test_parent_corruption_blocks_derivation_read_and_inspect(setup, parent):
    _, conn, principals, _, _, _, _ = setup
    saved = run(setup, parent)
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    conn.execute('UPDATE policy_evaluations SET result_hash=? WHERE id=?', ('b'*64, parent['id']))
    with pytest.raises(registry.PolicyError): run(setup, parent, 'corrupt-parent')
    with pytest.raises(registry.PolicyError): registry.evaluation(conn, principals['assistant'], saved['id'])
    assert registry.inspect(conn)
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 2


def test_sql_failure_rolls_back_without_partial_receipt(setup, parent):
    conn = setup[1]
    before = account_rows(conn)
    conn.execute("CREATE TRIGGER fail_scenario BEFORE INSERT ON policy_evaluations BEGIN SELECT RAISE(ABORT,'scenario storage fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match='storage fault'): run(setup, parent)
    assert not conn.in_transaction and account_rows(conn) == before
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1
    conn.execute('DROP TRIGGER fail_scenario')
    assert run(setup, parent)['mode'] == 'derived_portfolio_scenario'


def test_http_permissions_proposal_contract_pause_and_retry(setup, parent):
    path, conn, _, _, active, _, _ = setup
    tokens = {role: api_keys.create(conn, 'scenario-' + role, role=role) for role in ('assistant', 'research')}
    args = dict(request_id='scenario-http', expected_activation_id=active['id'], monitor_evaluation_id=parent['id'], proposal=PROPOSAL)
    headers = lambda role: {'Authorization': 'Bearer ' + tokens[role]}
    endpoint = '/tools/policy.portfolio_scenario'
    with TestClient(create_app(path)) as client:
        assert client.post(endpoint, headers=headers('assistant'), json=args).status_code == 404
    app = create_app(path, account_shadow_enabled=True)
    with TestClient(app) as client:
        assert client.post(endpoint, headers=headers('research'), json=args).status_code == 403
        for proposal in (dict(sales={'600000': 100}), PROPOSAL | {'sales': {}}, PROPOSAL | {'sales': {'600000': True}},
                         PROPOSAL | {'fees': -1}, PROPOSAL | {'cash': -1}, PROPOSAL | {'extra': True}):
            assert client.post(endpoint, headers=headers('assistant'), json=args | {'proposal': proposal}).status_code == 422
        first = client.post(endpoint, headers=headers('assistant'), json=args)
        assert first.status_code == 200, first.text
        assert client.post(endpoint, headers=headers('assistant'), json=args).json() == first.json()
        app.state.gate.pause(1)
        assert client.post(endpoint, headers=headers('assistant'), json=args).status_code == 503
