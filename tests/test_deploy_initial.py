"""Initial-release ordering and failure decisions; structural operations, not Docker acceptance."""
import json

import pytest

from a_share_research import deploy, initial_release
from a_share_research.tools.control import Gate, pause_offline
from test_deploy_tools import commands, index, operations as _operations

operations = _operations


@pytest.fixture
def initial(operations, monkeypatch):
    def setup(*, failure='resume_lost', recovery_failure=None, compatible=True,
              preparation_failure=False, migration_failure=False):
        state = operations(existing=False, failure=failure)
        original_run, original_control = deploy.run, deploy.tools_control

        def images(code):
            state.events.append(('verify_images', code))
            prefix = 'recovery' if code == 'baseline' else 'new'
            return {s: f'{prefix}-{s}' for s in ('web', 'worker')}

        def prepare(database, target, recovery, evidence):
            state.events.append(('prepare',))
            assert all(state.running.values())
            assert target == {'web': 'new-web', 'worker': 'new-worker', 'tools': 'new-worker'}
            assert recovery == {'web': 'recovery-web', 'worker': 'recovery-worker'}
            assert database == state.data/'app.db'
            if preparation_failure:
                raise initial_release.InitialReleaseError('preparation refused')
            return {'prepared': True}

        def migrate(database, image, evidence):
            state.events.append(('migrate',))
            assert not any(state.running.values())
            assert Gate(state.data/'.tools-paused').status()['paused']
            assert image == 'new-worker' and database == state.data/'app.db'
            if migration_failure:
                raise initial_release.InitialReleaseError('conservation refused')
            return {'source_version': 9, 'target_version': 21}

        def assess(images, database, evidence):
            state.events.append(('rollback_probe',))
            assert images == {'web': 'recovery-web', 'worker': 'recovery-worker'}
            assert not any(state.running.values()) and state.paused
            return {'ok': compatible, 'scope': 'read_only', 'images': {}}

        def run(*args):
            recovering = state.containers.get('worker') == 'recovery-worker'
            if recovering and args[:3] == ('docker', 'exec', 'a-share-research-worker'):
                action = 'integrity' if 'a_share_research.integrity' in args else 'preflight'
                state.events.append(('recovery_check', action))
                if recovery_failure == action:
                    raise RuntimeError('recovery '+action+' failed')
                return json.dumps({'ok': True})
            return original_run(*args)

        def control(action):
            recovering = state.containers.get('worker') == 'recovery-worker'
            if action == 'pause':
                pause_offline(state.data/'.tools-paused')
            if recovering and action == 'pause' and recovery_failure == 'redrain' and getattr(state, 'recovery_lost', False):
                state.in_flight = 1
                raise RuntimeError('recovery drain unconfirmed')
            result = original_control(action)
            if recovering and action == 'resume' and recovery_failure in ('resume_lost', 'redrain'):
                state.recovery_lost = True
                raise RuntimeError('recovery resume reply lost')
            return result

        monkeypatch.setattr(deploy, 'verify_images', images)
        monkeypatch.setattr(initial_release, 'prepare', prepare)
        monkeypatch.setattr(initial_release, 'migrate', migrate)
        monkeypatch.setattr(deploy.rollback, 'assess', assess)
        monkeypatch.setattr(deploy, 'run', run)
        monkeypatch.setattr(deploy, 'tools_control', control)
        return state
    return setup


def release(state):
    return deploy.deploy('revision', state.evidence, with_tools=True, allow_config=True, initial_recovery='baseline')


def test_cli_passes_the_explicit_initial_recovery_option(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr('sys.argv', ['deploy', 'target', str(tmp_path/'proof'), '--with-tools',
                                    '--allow-config', '--initial-recovery', 'baseline'])
    monkeypatch.setattr(deploy, 'deploy', lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(deploy.os, 'umask', lambda value: 0o077)
    deploy.main()
    assert calls == [(('target', tmp_path/'proof'),
                      dict(with_tools=True, allow_config=True, initial_recovery='baseline'))]


def test_initial_preparation_before_pause_and_stopped_migration_before_start(initial):
    state = initial(failure=None)
    release(state)
    assert index(state, ('prepare',)) < index(state, ('run', 'docker', 'compose', 'stop'))
    assert index(state, ('backup',)) < index(state, ('migrate',))
    assert index(state, ('migrate',)) < index(state, ('run', 'docker', 'tag', 'new-web'))
    assert state.containers == {'web': 'new-web', 'worker': 'new-worker', 'tools': 'new-worker'}
    manifest = json.loads((state.evidence/'manifest.json').read_text())
    assert manifest['initial_recovery'] == {'web': 'recovery-web', 'worker': 'recovery-worker'}
    assert manifest['pruned_backups'] == [] and manifest['pruned_images'] == {}
    assert not state.paused


def test_private_preparation_failure_never_stops_or_changes_tags(initial):
    state = initial(preparation_failure=True)
    with pytest.raises(initial_release.InitialReleaseError, match='preparation refused'):
        release(state)
    assert not commands(state, 'stop')
    assert not any(e[:3] == ('run', 'docker', 'tag') for e in state.events)
    assert not (state.data/'.tools-paused').exists()


def test_failed_actual_migration_holds_old_containers_stopped_without_resuming(initial):
    state = initial(migration_failure=True)
    with pytest.raises(RuntimeError, match='未通过守恒核对'):
        release(state)
    assert not any(state.running.values())
    assert Gate(state.data/'.tools-paused').status()['paused']
    assert ('rollback_probe',) not in state.events
    assert ('control', 'resume') not in state.events
    assert not commands(state, 'up')


@pytest.mark.parametrize('failure', ['preflight', 'integrity', 'resume_lost'])
def test_initial_failure_keeps_tools_and_uses_checked_compatible_baseline(initial, failure):
    state = initial(failure=failure)
    with pytest.raises(RuntimeError, match='injected|发布后完整性'):
        release(state)
    assert state.containers == {'web': 'recovery-web', 'worker': 'recovery-worker', 'tools': 'recovery-worker'}
    assert all(state.running.values()) and not state.paused
    assert not commands(state, 'rm')
    assert ('recovery_check', 'preflight') in state.events and ('recovery_check', 'integrity') in state.events
    assert index(state, ('rollback_probe',)) < index(state, ('recovery_check', 'preflight'))
    result = json.loads((state.evidence/'recovery-result.json').read_text())
    assert result['status'] == 'recovered'
    assert result['images']['tools'] == 'recovery-worker'
    assert not any(e[:4] == ('run', 'docker', 'tag', 'old-web') for e in state.events)


def test_post_failure_incompatible_baseline_holds_new_stopped_services(initial):
    state = initial(compatible=False)
    with pytest.raises(RuntimeError, match='指定恢复镜像读取兼容检查未通过'):
        release(state)
    assert state.containers == {'web': 'new-web', 'worker': 'new-worker', 'tools': 'new-worker'}
    assert not any(state.running.values()) and state.paused
    assert not commands(state, 'rm')


@pytest.mark.parametrize('failure', ['preflight', 'integrity', 'resume_lost'])
def test_failed_recovery_does_not_reopen_admission_or_remove_tools(initial, failure):
    state = initial(failure='integrity', recovery_failure=failure)
    with pytest.raises(RuntimeError, match='recovery '):
        release(state)
    assert state.containers['tools'] == 'recovery-worker'
    assert not any(state.running.values()) and state.paused
    assert Gate(state.data/'.tools-paused').status()['paused']
    assert json.loads((state.evidence/'recovery-result.json').read_text())['status'] == 'held'


def test_unconfirmed_recovery_redrain_never_stops_live_services(initial):
    state = initial(failure='integrity', recovery_failure='redrain')
    with pytest.raises(RuntimeError, match='recovery drain unconfirmed'):
        release(state)
    assert all(state.running.values())
    assert state.containers['tools'] == 'recovery-worker'
    assert state.in_flight == 1


def test_initial_backup_failure_can_restart_unmigrated_old_services(initial):
    state = initial(failure='backup')
    with pytest.raises(RuntimeError, match='injected backup'):
        release(state)
    assert ('migrate',) not in state.events
    assert state.containers == {'web': 'old-web', 'worker': 'old-worker'}
    assert all(state.running.values()) and not Gate(state.data/'.tools-paused').status()['paused']


@pytest.mark.parametrize('existing,with_tools,allow_config', [(True, True, True), (False, False, True), (False, True, False)])
def test_initial_option_cannot_override_existing_deployment_or_configuration_guard(operations, existing, with_tools, allow_config):
    state = operations(existing=existing)
    with pytest.raises(RuntimeError, match='首次迁移恢复入口仅用于'):
        deploy.deploy('revision', state.evidence, initial_recovery='baseline',
                      with_tools=with_tools, allow_config=allow_config)
    assert not commands(state, 'stop') and not commands(state, 'up')
