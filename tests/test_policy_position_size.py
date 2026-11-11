"""Derived-receipt transport tests; account fixtures do not constitute financial evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from a_share_research import api_keys, gate
from a_share_research.policy import evaluation, identity, monitor, position_evaluation, position_sizing, registry
from a_share_research.tools.server import create_app
from test_policy_monitor import setup as monitor_setup

PROPOSAL = dict(code='600900', price=29.5, framework='A')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    yield from monitor_setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def parent(setup):
    path, conn, principals, _, active, _, _ = setup
    return monitor.run(conn, principals['assistant'], path, 'parent-monitor', active['id'])


def run(setup, parent, request='derived', **kwargs):
    path, conn, principals, _, active, _, _ = setup
    return position_sizing.run(conn, principals[kwargs.pop('role', 'assistant')], path, request,
                               kwargs.pop('expected', active['id']), kwargs.pop('reference', parent['id']),
                               kwargs.pop('proposal', PROPOSAL))


def test_saved_reference_hashes_no_network_and_idempotence(setup, parent, monkeypatch):
    monkeypatch.setattr(monitor, '_capture', lambda *a, **k: pytest.fail('sizing cannot acquire inputs'))
    monkeypatch.setattr(monitor, 'capture', lambda *a, **k: pytest.fail('sizing cannot acquire inputs'))
    path, conn, principals, manifest, active, _, _ = setup
    saved = run(setup, parent)
    assert saved['mode'] == 'derived_position_size'
    assert saved['capture_context']['schema_version'] == 6
    assert saved['input']['monitor_evaluation'] == dict(id=parent['id'], input_hash=parent['input_hash'], result_hash=parent['result_hash'])
    assert saved['input']['snapshot'] == parent['result']
    assert saved['input']['proposal'] == PROPOSAL | {'cash': None}
    assert saved['input_hash'] == gate.digest(saved['input'])
    assert saved['result_hash'] == gate.digest(saved['result'])
    assert saved['result'] == position_evaluation.evaluate(saved['input'], manifest['parameters'])
    assert saved['result']['status'] == 'unavailable'
    assert run(setup, parent) == saved
    assert registry.evaluation(conn, principals['assistant'], saved['id']) == saved
    assert registry.inspect(conn) == []
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 2
    for changed in (dict(proposal=PROPOSAL | {'price': 30.}), dict(reference=parent['id']+1), dict(expected=active['id']+1)):
        with pytest.raises(registry.PolicyError, match='不同'):
            run(setup, parent, **changed)


def test_ready_using_captured_snapshot_transport_injection(setup, monkeypatch):
    # Only service transport is injected; sizing uses the existing captured no_action evidence unchanged.
    snapshot = json.loads((Path(__file__).parent / 'policy_v0/fixtures/monitor/agent-behavior-snapshots.json').read_text())['no_action']
    assert snapshot['account']['risk_policy'] == setup[3]['parameters']
    monkeypatch.setattr(evaluation, 'evaluate', lambda *a: deepcopy(snapshot))
    path, conn, principals, _, active, _, _ = setup
    parent = monitor.run(conn, principals['assistant'], path, 'captured-transport', active['id'])
    saved = run(setup, parent)
    assert saved['result']['status'] == 'ready'
    assert saved['result']['sizing']['max_shares'] == 300
    assert registry.inspect(conn) == []


def test_new_source_rejected_but_saved_derivation_survives_changes(setup, parent):
    _, conn, principals, manifest, active, _, _ = setup
    saved = run(setup, parent)
    conn.execute("UPDATE account_source SET updated_at='changed'")
    with pytest.raises(registry.PolicyError, match='来源'):
        run(setup, parent, 'after-source')
    registry.activate(conn, principals['owner'], manifest['identity'], expected_id=active['id'], request_id='reactivate', reason='transport fixture')
    assert run(setup, parent) == saved
    assert registry.evaluation(conn, principals['assistant'], saved['id']) == saved
    assert registry.inspect(conn) == []
    with pytest.raises(registry.PolicyError, match='生效政策'):
        run(setup, parent, 'after-activation')


def test_replay_reference_not_service_monitor(setup):
    _, conn, principals, manifest, _, bundle, _ = setup
    supplied = registry.replay(conn, principals['assistant'], manifest['identity'], 'supplied', bundle)
    with pytest.raises(registry.PolicyError, match='服务端监控'):
        run(setup, supplied)
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1


def test_installed_drift_rejected_without_write(setup, parent, monkeypatch):
    def drift(*args): raise ValueError('injected installed drift')
    monkeypatch.setattr(identity, 'check_installed', drift)
    with pytest.raises(registry.PolicyError, match='当前实现'):
        run(setup, parent)
    assert setup[1].execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1


def test_transaction_fault_does_not_leave_half_receipt(setup, parent):
    conn = setup[1]
    conn.execute("CREATE TRIGGER fail_derived BEFORE INSERT ON policy_evaluations BEGIN SELECT RAISE(ABORT,'injected derived storage fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match='storage fault'):
        run(setup, parent)
    assert not conn.in_transaction
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1
    conn.execute('DROP TRIGGER fail_derived')
    assert run(setup, parent)['mode'] == 'derived_position_size'


@pytest.mark.parametrize('field', ['input_hash', 'result_hash'])
def test_corrupt_parent_hash_prevents_derive_and_breaks_existing_reference(setup, parent, field):
    _, conn, principals, _, _, _, _ = setup
    saved = run(setup, parent)
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    conn.execute(f'UPDATE policy_evaluations SET {field}=? WHERE id=?', ('b'*64, parent['id']))
    with pytest.raises(registry.PolicyError):
        run(setup, parent, 'corrupt-parent')
    with pytest.raises(registry.PolicyError):
        registry.evaluation(conn, principals['assistant'], saved['id'])
    assert registry.inspect(conn)
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 2


def test_http_permissions_validation_pause_and_saved_retry(setup, parent):
    path, conn, _, _, active, _, _ = setup
    tokens = {role: api_keys.create(conn, 'sizing-' + role, role=role) for role in ('assistant', 'research')}
    args = dict(request_id='sizing-http', expected_activation_id=active['id'], monitor_evaluation_id=parent['id'], proposal=PROPOSAL)
    headers = lambda role: {'Authorization': 'Bearer ' + tokens[role]}
    with TestClient(create_app(path)) as client:
        assert client.post('/tools/policy.position_size', headers=headers('assistant'), json=args).status_code == 404
    app = create_app(path, account_shadow_enabled=True)
    with TestClient(app) as client:
        assert client.post('/tools/policy.position_size', headers=headers('research'), json=args).status_code == 403
        for changed in (args | {'extra': True}, args | {'monitor_evaluation_id': True}, args | {'proposal': PROPOSAL | {'price': 0}},
                        args | {'proposal': PROPOSAL | {'cash': -1}}, args | {'proposal': PROPOSAL | {'extra': True}}):
            assert client.post('/tools/policy.position_size', headers=headers('assistant'), json=changed).status_code == 422
        first = client.post('/tools/policy.position_size', headers=headers('assistant'), json=args)
        assert first.status_code == 200, first.text
        assert client.post('/tools/policy.position_size', headers=headers('assistant'), json=args).json() == first.json()
        app.state.gate.pause(1)
        assert client.post('/tools/policy.position_size', headers=headers('assistant'), json=args).status_code == 503


def test_old_parent_rejected_under_new_activation(setup, parent):
    _, conn, principals, manifest, active, _, _ = setup
    new = registry.activate(conn, principals['owner'], manifest['identity'], expected_id=active['id'],
                            request_id='new-activation', reason='transport fixture')
    with pytest.raises(registry.PolicyError, match='服务端监控'):
        run(setup, parent, expected=new['id'])
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1


def test_source_mutation_during_calculation_rolls_back(setup, parent, monkeypatch):
    conn = setup[1]
    before = conn.execute('SELECT updated_at FROM account_source').fetchone()[0]
    original = position_evaluation.evaluate
    def changed(*args):
        result = original(*args)
        conn.execute("UPDATE account_source SET updated_at='changed-in-sizing'")
        return result
    monkeypatch.setattr(position_evaluation, 'evaluate', changed)
    with pytest.raises(registry.PolicyError, match='计算期间账户来源改变'):
        run(setup, parent)
    assert conn.execute('SELECT updated_at FROM account_source').fetchone()[0] == before
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1


def test_missing_derived_snapshot_is_a_readable_integrity_failure(setup, parent):
    _, conn, principals, _, _, _, _ = setup
    saved = run(setup, parent)
    broken = deepcopy(saved['input'])
    del broken['snapshot']
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    conn.execute('UPDATE policy_evaluations SET input=?,input_hash=? WHERE id=?',
                 (gate.canonical(broken), gate.digest(broken), saved['id']))
    with pytest.raises(registry.PolicyError, match='关联记录损坏'):
        registry.evaluation(conn, principals['assistant'], saved['id'])
    assert registry.inspect(conn)
