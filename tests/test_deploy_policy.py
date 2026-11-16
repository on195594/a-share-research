"""Real registry + full identities in local installations and temporary SQLite.

Docker orchestration remains simulated by test_deploy_tools; not production acceptance.
"""
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from a_share_research import api_keys, db, deploy
from a_share_research.account import mirror
from a_share_research.policy.v0 import risk_budget


@pytest.fixture
def local_policy(tmp_path, monkeypatch):
    original_freeze, original_probe = deploy.freeze_policy, deploy.policy_probe
    old = tmp_path / 'old-install'
    old.mkdir()
    shutil.copytree(deploy.ROOT / 'a_share_research', old / 'a_share_research',
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(deploy.ROOT / 'uv.lock', old / 'uv.lock')
    with (old / 'a_share_research/deploy.py').open('a') as stream:
        stream.write('\n# Synthetic predecessor installation, with full identity scope.\n')
    config = tmp_path / 'empty.env'
    config.touch(mode=0o600)

    def setup(state=None, *, parameters=None):
        monkeypatch.setattr(deploy, 'freeze_policy', original_freeze)
        monkeypatch.setattr(deploy, 'policy_probe', original_probe)
        data = state.data if state else tmp_path / 'data'
        data.mkdir(exist_ok=True)
        database = data / 'app.db'
        with closing(db.connect(database)) as conn:
            token = api_keys.create(conn, 'isolated-owner', role='owner')
            conn.execute('INSERT INTO account_source VALUES (1,?,?,?,?)',
                         ('mirror', mirror.digest(mirror._read(conn, data)), db.now(), '{}'))
        local = SimpleNamespace(current='old-worker', events=[], database=database,
                                token=token, lose_reply=False, failed_writes=False, after_activate=None, probe_time=None)
        def command(script, payload, *, image=None):
            selected = image or (state.containers['worker'] if state else local.current)
            root = old if selected == 'old-worker' else deploy.ROOT
            action = 'activate' if script == deploy.POLICY_ACTIVATE else payload['action']
            local.events.append((action, selected))
            if action == 'activate' and local.failed_writes:
                raise RuntimeError('injected activation failure')
            script = script.replace("Path('/data/app.db')", f'Path({str(database)!r})')
            script = script.replace("sqlite3.connect('/data/app.db',", f'sqlite3.connect({str(database)!r},')
            if local.probe_time is not None:
                script = script.replace('now = datetime.fromisoformat(db.now())',
                                        f'now = datetime.fromisoformat({local.probe_time!r})')
            result = subprocess.run([sys.executable, '-c', script], input=json.dumps(payload), text=True,
                capture_output=True, cwd=root, timeout=10,
                env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'PYTHONPATH': str(root),
                     'A_STOCK_CONFIG_FILE': str(config)})
            if result.returncode:
                raise RuntimeError('local policy command failed')
            if action == 'activate' and local.lose_reply:
                local.lose_reply = False
                raise RuntimeError('injected lost activation receipt')
            if action == 'activate' and local.after_activate:
                hook, local.after_activate = local.after_activate, None
                hook()
            return json.loads(result.stdout)
        monkeypatch.setattr(deploy, 'policy_command', command)
        local.command = command
        params = parameters or risk_budget.policy()
        candidate = command(deploy.POLICY_PROBE, dict(action='candidate', parameters=params), image='old-worker')
        local.old = candidate
        local.activation = command(deploy.POLICY_ACTIVATE, dict(identity=candidate['identity'], parameters=params,
            owner_token=token, expected_id=0, request_id='predecessor', reason='synthetic initial fixture'))
        local.target = command(deploy.POLICY_PROBE, dict(action='candidate', parameters=params), image='new-worker')
        local.approval = dict(identity=local.target['identity'], expected_id=local.activation['id'],
                             request_id='approved-release-once', reason='isolated exact release approval', owner_token=token)
        local.approval_path = tmp_path / 'approval.json'
        def approve(*, restore=False):
            if restore:
                local.approval['rollback'] = dict(identity=local.old['identity'], request_id='approved-restore-once',
                                                  reason='isolated exact predecessor restoration')
            local.approval_path.write_text(json.dumps(local.approval))
            local.approval_path.chmod(0o600)
            return local.approval_path
        local.approve = approve
        def count():
            with closing(sqlite3.connect(database)) as conn:
                return conn.execute('SELECT COUNT(*) FROM policy_activations').fetchone()[0]
        local.count = count
        if not state:
            monkeypatch.setattr(deploy, 'describe', lambda service: dict(image=local.current, running=True))
        return local
    return setup


def freeze(local):
    return deploy.freeze_policy({'worker': 'new-worker'}, {'worker': 'old-worker'},
                                approval=local.approval, not_applicable=False)


def test_exact_approval_and_matched_gate_are_idempotent(local_policy):
    local = local_policy()
    frozen = freeze(local)
    assert frozen['parameters'] == local.old['parameters'] == local.target['parameters']
    assert frozen['diff']['parameters_changed'] is False
    assert frozen['diff']['code'] == ['a_share_research/deploy.py']
    local.current = 'new-worker'
    result = deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=local.approval)
    assert result['installed'] is True and local.count() == 2
    assert deploy.policy_gate({'worker': 'new-worker'}, frozen) == result
    assert local.count() == 2
    # A release of a fully matched actual installation needs no owner token or append.
    frozen = deploy.freeze_policy({'worker': 'new-worker'}, {}, approval=None, not_applicable=False)
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
        deploy.freeze_policy({'worker': 'new-worker'}, {'worker': 'old-worker'},
                             approval=approval, not_applicable=False)
    assert local.count() == 1


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
    assert local.events[-4:] == [('activate', 'new-worker'), ('activate', 'new-worker'),
                                 ('probe', 'new-worker'), ('probe', 'new-worker')]


@pytest.mark.parametrize('restore', [False, True])
def test_append_only_rollback_requires_this_release_approval(local_policy, restore):
    local = local_policy()
    if restore: local.approve(restore=True)
    frozen = freeze(local)
    local.current = 'new-worker'
    deploy.policy_gate({'worker': 'new-worker'}, frozen, approval=local.approval)
    local.current = 'old-worker'
    if not restore:
        with pytest.raises(RuntimeError, match='未明确批准'):
            deploy.policy_gate({'worker': 'old-worker'}, frozen, approval=local.approval, restoring=True)
        assert local.count() == 2
    else:
        current = deploy.policy_gate({'worker': 'old-worker'}, frozen, approval=local.approval, restoring=True)
        assert current['activation']['id'] == 3 and current['identity'] == local.old['identity']
        assert current['activation']['expected_id'] == 2 and local.count() == 3
        deploy.policy_gate({'worker': 'old-worker'}, frozen, restoring=True)
        assert local.count() == 3


def test_rollback_before_activation_preserves_original_pointer(local_policy):
    local = local_policy()
    frozen = freeze(local)
    deploy.policy_gate({'worker': 'old-worker'}, frozen, restoring=True)
    assert local.count() == 1


@pytest.mark.parametrize('output', [None, {}, {'applicable': False}, {'installed': True}])
def test_required_unknown_state_is_not_not_applicable(local_policy, monkeypatch, output):
    local_policy()
    monkeypatch.setattr(deploy, 'policy_command', lambda *a, **k: output)
    with pytest.raises((RuntimeError, AttributeError)):
        deploy.freeze_policy({'worker': 'new-worker'}, {}, approval=None, not_applicable=False)


def test_host_flock_serializes_release_and_completion(tmp_path, monkeypatch):
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(deploy, 'ROOT', tmp_path)
    with deploy.release_lock():
        with pytest.raises(RuntimeError, match='正在运行'):
            deploy.complete_held(tmp_path)
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
        deploy.freeze_policy({'worker':'new-worker'}, {}, approval=local.approval, not_applicable=False)
    with pytest.raises(RuntimeError, match='参数'):
        deploy.policy_gate({'worker':'old-worker'}, frozen, restoring=True)
    local.probe_time = None
    with closing(sqlite3.connect(local.database)) as conn:
        conn.execute("UPDATE account_source SET mode='ledger',watermark=?", (json.dumps({'account_scope':'other-account'}),))
        conn.commit()
    with pytest.raises(RuntimeError, match='账户'):
        deploy.policy_gate({'worker':'old-worker'}, frozen, restoring=True)
    assert local.count() == 1


def test_real_activation_insert_failure_does_not_move_pointer(local_policy):
    local = local_policy()
    frozen = freeze(local)
    with closing(sqlite3.connect(local.database)) as conn:
        conn.execute("CREATE TRIGGER reject_new_activation BEFORE INSERT ON policy_activations BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    local.current = 'new-worker'
    with pytest.raises(RuntimeError): deploy.policy_gate({'worker':'new-worker'}, frozen, approval=local.approval)
    assert local.count() == 1


def test_historical_success_receipt_is_not_latest_and_other_switch_cannot_be_compensated(local_policy):
    local = local_policy()
    local.approve(restore=True)
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
    local.current = 'old-worker'
    with pytest.raises(RuntimeError, match='不是本次'):
        deploy.policy_gate({'worker':'old-worker'}, frozen, approval=local.approval, restoring=True)
    assert local.count() == 3


def test_killed_process_leaves_durable_pause_and_held_checkpoint(tmp_path):
    from a_share_research.tools.control import Gate, ControlError
    marker = tmp_path/'data'/'.tools-paused'
    evidence = tmp_path/'held'
    evidence.mkdir()
    script = """
import os
from pathlib import Path
from a_share_research.deploy import save_evidence
from a_share_research.tools.control import Gate
save_evidence(Path(EVIDENCE), 'release-state.json', dict(format='ashare-release-v1', status='held', phase='pausing'))
Gate(Path(MARKER)).pause(0)
os._exit(77)
""".replace('EVIDENCE', repr(str(evidence))).replace('MARKER', repr(str(marker)))
    process = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=10)
    assert process.returncode == 77
    assert json.loads((evidence/'release-state.json').read_text())['status'] == 'held'
    rebuilt = Gate(marker)
    assert rebuilt.status() == {'paused': True, 'in_flight': 0}
    with pytest.raises(ControlError):
        with rebuilt.acquire(): pytest.fail('paused work was admitted')


def test_target_already_matches_pointer_repairs_installation_without_new_activation(local_policy):
    local = local_policy()
    local.current = 'new-worker'  # Simulate an existing incorrectly installed release.
    assert deploy.policy_probe()['installed'] is False
    frozen = deploy.freeze_policy({'worker':'old-worker'}, {}, approval=None, not_applicable=False)
    assert frozen['intent'] is None
    local.current = 'old-worker'
    result = deploy.policy_gate({'worker':'old-worker'}, frozen)
    assert result['installed'] is True and local.count() == 1
