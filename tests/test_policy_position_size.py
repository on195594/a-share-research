"""Derived-receipt transport tests; account fixtures do not constitute financial evidence."""
from copy import deepcopy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from a_share_research import api_keys, gate
from a_share_research.policy import evaluation, monitor, position_evaluation, position_sizing, registry
from a_share_research.tools.server import create_app

PROPOSAL = dict(code='600900', price=29.5, framework='A')


@pytest.fixture
def parent(policy_setup):
    setup = policy_setup
    path, conn, principals, _, active, _, _ = setup
    return monitor.run(conn, principals['assistant'], path, 'parent-monitor', active['id'])


def run(setup, parent, request='derived', **kwargs):
    path, conn, principals, _, active, _, _ = setup
    return position_sizing.run(conn, principals[kwargs.pop('role', 'assistant')], path, request,
                               kwargs.pop('expected', active['id']), kwargs.pop('reference', parent['id']),
                               kwargs.pop('proposal', PROPOSAL))


def test_saved_reference_hashes_no_network_and_idempotence(policy_setup, parent, monkeypatch):
    setup = policy_setup
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


def test_ready_using_captured_snapshot_transport_injection(policy_setup, monkeypatch):
    # Only service transport is injected; sizing uses the existing captured no_action evidence unchanged.
    setup = policy_setup
    snapshot = json.loads((Path(__file__).parent / 'policy_v0/fixtures/monitor/agent-behavior-snapshots.json').read_text())['no_action']
    assert snapshot['account']['risk_policy'] == setup[3]['parameters']
    monkeypatch.setattr(evaluation, 'evaluate', lambda *a: deepcopy(snapshot))
    path, conn, principals, _, active, _, _ = setup
    parent = monitor.run(conn, principals['assistant'], path, 'captured-transport', active['id'])
    saved = run(setup, parent)
    assert saved['result']['status'] == 'ready'
    assert saved['result']['sizing']['max_shares'] == 300
    assert registry.inspect(conn) == []


@pytest.mark.parametrize('field', ['input_hash', 'result_hash'])
def test_corrupt_parent_hash_prevents_derive_and_breaks_existing_reference(policy_setup, parent, field):
    setup = policy_setup
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


def test_http_validates_position_size_arguments(policy_setup, parent):
    path, conn, _, _, active, _, _ = policy_setup
    token = api_keys.create(conn, 'position_size-http', role='assistant')
    args = dict(request_id='sizing-http', expected_activation_id=active['id'], monitor_evaluation_id=parent['id'], proposal=PROPOSAL)
    with TestClient(create_app(path, account_shadow_enabled=True)) as client:
        headers = {'Authorization': 'Bearer ' + token}
        endpoint = '/tools/policy_position_size'
        assert client.post(endpoint, headers=headers, json=args | {'proposal': PROPOSAL | {'price': 0}}).status_code == 422
        response = client.post(endpoint, headers=headers, json=args)
        assert response.status_code == 200, response.text


def test_source_mutation_during_calculation_rolls_back(policy_setup, parent, monkeypatch):
    setup = policy_setup
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


def test_missing_derived_snapshot_is_a_readable_integrity_failure(policy_setup, parent):
    setup = policy_setup
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
