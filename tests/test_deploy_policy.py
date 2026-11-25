"""Real registry + full identities in local installations and temporary SQLite.

Docker orchestration remains simulated by test_deploy_tools; not production acceptance.
"""
from contextlib import closing
from datetime import datetime, timedelta
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from a_share_research import db, deploy
from a_share_research.policy.v0 import risk_budget


def freeze(local):
    return deploy.freeze_policy({'worker': 'new-worker'},
                                approval=local.approval, not_applicable=False)


def test_exact_approval_and_matched_gate_are_idempotent(local_policy):
    local = local_policy()
    frozen = freeze(local)
    assert frozen['parameters'] == local.old['parameters'] == local.target['parameters']
    assert frozen['diff']['parameters_changed'] is False
    assert frozen['diff']['rules'] == [local.old['rules_hash'], local.target['rules_hash']]
    assert local.old['rules_hash'] != local.target['rules_hash']
    local.current = 'new-worker'
    result = deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=local.approval)
    assert result['installed'] is True and local.count() == 2
    assert deploy.policy_gate({'worker': 'new-worker'}, frozen) == result
    assert local.count() == 2
    # A release of a fully matched actual installation needs no owner token or append.
    frozen = deploy.freeze_policy({'worker': 'new-worker'}, approval=None, not_applicable=False)
    deploy.policy_gate({'worker': 'new-worker'}, frozen)
    assert local.count() == 2


@pytest.mark.parametrize('case', ['absent', 'wrong-identity', 'wrong-expected', 'bad-owner', 'private-mode'])
def test_unapproved_or_bad_owner_refuses_before_switch(local_policy, case):
    local = local_policy()
    approval = local.approval
    if case == 'absent': approval = None
    if case == 'wrong-identity': approval['identity'] = local.old['identity']
    if case == 'wrong-expected': approval['expected_id'] = 99
    if case == 'bad-owner': approval['owner_token'] = 'invalid-fixture-token'
    if case == 'private-mode':
        path = local.approve()
        path.chmod(0o644)
        with pytest.raises(RuntimeError): deploy.private_json(path)
        return
    with pytest.raises(RuntimeError):
        deploy.freeze_policy({'worker': 'new-worker'},
                             approval=approval, not_applicable=False)
    assert local.count() == 1


@pytest.mark.parametrize('fields', [
    '"expected_id":1,"expected_id":2', '"expected_id":1e999', '"expected_id":-Infinity',
])
def test_private_approval_rejects_invalid_json_fields(tmp_path, fields):
    path = tmp_path / 'approval.json'
    path.write_text('{' + fields + '}')
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match='^批准输入无效$'):
        deploy.private_json(path)


@pytest.mark.parametrize('fault', ['cas', 'write', 'expired', 'uninstalled', 'unknown', 'historical'])
def test_gate_failures_never_accept_unknown_or_historical_latest(local_policy, monkeypatch, fault):
    local = local_policy()
    frozen = freeze(local)
    local.current = 'new-worker'
    if fault == 'cas':
        local.command(deploy.POLICY_ACTIVATE, dict(identity=local.target['identity'], parameters=local.target['parameters'],
            owner_token=local.token, expected_id=1, request_id='other-owner-choice', reason='do not bypass CAS'))
    if fault == 'write': local.failed_writes = True
    if fault == 'expired':
        original = deploy.policy_probe
        monkeypatch.setattr(deploy, 'policy_probe', lambda **k: original(**k) | dict(valid=False))
    if fault == 'uninstalled':
        # No intent: an installed mismatch is not tolerated by a supposedly matched release.
        frozen['intent'] = None
    if fault == 'unknown': monkeypatch.setattr(deploy, 'policy_probe', lambda **k: None)
    if fault == 'historical':
        deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=local.approval)
        local.command(deploy.POLICY_ACTIVATE, dict(identity=local.target['identity'], parameters=local.target['parameters'],
            owner_token=local.token, expected_id=2, request_id='later-owner', reason='latest differs from receipt'))
    with pytest.raises((RuntimeError, AttributeError)):
        deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=local.approval)


def test_lost_activation_reply_retries_same_request_then_reads_latest(local_policy):
    local = local_policy()
    frozen = freeze(local)
    local.current = 'new-worker'
    local.lose_reply = True
    current = deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=local.approval)
    assert current['activation']['id'] == 2 and local.count() == 2


def standing(local, tmp_path, monkeypatch, token=None):
    credential = tmp_path / 'owner.json'
    credential.write_text(json.dumps({'owner_token': token or local.token}))
    credential.chmod(0o600)
    monkeypatch.setattr(deploy, 'OWNER_CREDENTIAL', credential)
    return credential


def freeze_standing(approval):
    return deploy.freeze_policy({'worker': 'new-worker'},
                                approval=approval, not_applicable=False, standing='abc1234')


def test_standing_owner_credential_activates_without_an_approval_file(local_policy, tmp_path, monkeypatch):
    local = local_policy()
    standing(local, tmp_path, monkeypatch)
    approval = {}
    frozen = freeze_standing(approval)
    assert frozen['intent']['identity'] == local.target['identity']
    assert frozen['intent']['request_id'] == f"release-abc1234-after-{local.activation['id']}"
    assert local.token not in json.dumps(frozen)
    local.current = 'new-worker'
    assert deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=approval)['installed'] is True
    assert local.count() == 2


def test_release_without_activation_never_reads_the_standing_credential(local_policy, tmp_path, monkeypatch):
    local = local_policy()
    frozen = freeze(local)
    local.current = 'new-worker'
    deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=local.approval)
    standing(local, tmp_path, monkeypatch).write_text('not json')  # a broken credential must not matter
    approval = {}
    frozen = deploy.freeze_policy({'worker': 'new-worker'}, approval=approval, not_applicable=False, standing='abc1234')
    assert frozen['intent'] is None and approval == {}
    deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=approval)
    assert local.count() == 2


def test_explicit_approval_is_checked_as_written_even_with_a_standing_credential(local_policy, tmp_path, monkeypatch):
    local = local_policy()
    standing(local, tmp_path, monkeypatch)
    local.approval |= dict(expected_id=99, standing='abc1234')
    with pytest.raises(RuntimeError, match='明确批准'):
        deploy.freeze_policy({'worker': 'new-worker'}, approval=local.approval, not_applicable=False)
    assert local.count() == 1


@pytest.mark.parametrize('case', ['missing', 'bad-owner', 'shared-mode'])
def test_standing_approval_still_needs_the_private_owner_credential(local_policy, tmp_path, monkeypatch, case):
    local = local_policy()
    if case == 'shared-mode':
        standing(local, tmp_path, monkeypatch).chmod(0o644)
    elif case == 'bad-owner':
        standing(local, tmp_path, monkeypatch, token='invalid-fixture-token')
    with pytest.raises(RuntimeError, match={'missing': '常设 owner 凭据', 'shared-mode': '0600', 'bad-owner': None}[case]):
        freeze_standing({})
    assert local.count() == 1


@pytest.mark.parametrize('output', [None, {}, {'applicable': False}, {'installed': True}])
def test_required_unknown_state_is_not_not_applicable(local_policy, monkeypatch, output):
    local_policy()
    monkeypatch.setattr(deploy, 'policy_command', lambda *a, **k: output)
    with pytest.raises((RuntimeError, AttributeError)):
        deploy.freeze_policy({'worker': 'new-worker'}, approval=None, not_applicable=False)


def test_host_flock_serializes_releases(tmp_path, monkeypatch):
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(deploy, 'ROOT', tmp_path)
    with deploy.release_lock():
        with pytest.raises(RuntimeError, match='正在运行'):
            deploy.deploy('not-even-inspected', tmp_path / 'release')
    with deploy.release_lock(): pass


def test_policy_command_hides_child_secrets_and_timeout(monkeypatch):
    for result in (SimpleNamespace(returncode=1, stdout='', stderr='private-owner-token'),
                   SimpleNamespace(returncode=0, stdout='bad-json', stderr='private-owner-token')):
        monkeypatch.setattr(deploy.subprocess, 'run', lambda *a, **k: result)
        with pytest.raises(RuntimeError) as error:
            deploy.policy_command('script', {'owner_token': 'private-owner-token'})
        assert 'private-owner-token' not in str(error.value)
    def timeout(*a, **k): raise subprocess.TimeoutExpired('safe', 40, stderr='private-owner-token')
    monkeypatch.setattr(deploy.subprocess, 'run', timeout)
    with pytest.raises(RuntimeError) as error: deploy.policy_probe()
    assert 'private-owner-token' not in str(error.value)


def test_real_expiry_and_account_scope_reject_without_altering_parameters(local_policy):
    now = datetime.now(db.SHANGHAI)
    params = risk_budget.policy() | dict(source='policy_file', account_scope='isolated-account', currency='CNY',
        effective_from=(now-timedelta(days=1)).isoformat(), effective_to=(now+timedelta(hours=1)).isoformat(),
        confirmed_at=(now-timedelta(hours=2)).isoformat(), confirmation_ref='isolated-owner-confirmation')
    local = local_policy(parameters=params)
    frozen = freeze(local)
    local.probe_time = (now+timedelta(hours=2)).isoformat()
    with pytest.raises(RuntimeError, match='参数'):
        deploy.freeze_policy({'worker':'new-worker'}, approval=local.approval, not_applicable=False)
    local.current = 'new-worker'
    with pytest.raises(RuntimeError, match='参数'):
        deploy.policy_gate({'worker':'new-worker'}, frozen, approval=local.approval)
    local.probe_time = None
    with closing(sqlite3.connect(local.database)) as conn:
        conn.execute("UPDATE account_source SET mode='ledger',watermark=?", (json.dumps({'account_scope':'other-account'}),))
        conn.commit()
    with pytest.raises(RuntimeError, match='账户'):
        deploy.policy_gate({'worker':'new-worker'}, frozen, approval=local.approval)
    assert local.count() == 1


def test_real_activation_insert_failure_does_not_move_pointer(local_policy):
    local = local_policy()
    frozen = freeze(local)
    with closing(sqlite3.connect(local.database)) as conn:
        conn.execute("CREATE TRIGGER reject_new_activation BEFORE INSERT ON policy_activations BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    local.current = 'new-worker'
    with pytest.raises(RuntimeError): deploy.policy_gate({'worker':'new-worker'}, frozen, approval=local.approval)
    assert local.count() == 1


def test_historical_success_receipt_is_not_latest(local_policy):
    local = local_policy()
    frozen = freeze(local)
    local.current = 'new-worker'
    def intervening_choice():
        local.command(deploy.POLICY_ACTIVATE, dict(identity=local.target['identity'], parameters=local.target['parameters'],
            owner_token=local.token, expected_id=2, request_id='different-choice', reason='must not be undone'))
    local.after_activate = intervening_choice
    with pytest.raises(RuntimeError, match='latest'):
        deploy.policy_gate({'worker':'new-worker'}, frozen, approval=local.approval)
    assert local.count() == 3
    # The exact idempotent receipt is still returned by registry but never accepted as latest.
    receipt = local.command(deploy.POLICY_ACTIVATE, dict(identity=local.target['identity'], parameters=local.target['parameters'],
        owner_token=local.token, expected_id=1, request_id=local.approval['request_id'], reason=local.approval['reason']))
    assert receipt['id'] == 2


def test_target_already_matches_pointer_repairs_installation_without_new_activation(local_policy):
    local = local_policy()
    local.current = 'new-worker'  # Simulate an existing incorrectly installed release.
    assert deploy.policy_probe()['installed'] is False
    frozen = deploy.freeze_policy({'worker':'old-worker'}, approval=None, not_applicable=False)
    assert frozen['intent'] is None
    local.current = 'old-worker'
    result = deploy.policy_gate({'worker':'old-worker'}, frozen)
    assert result['installed'] is True and local.count() == 1
