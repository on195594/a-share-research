"""Release-order simulations only: no Docker, live service or production acceptance."""
import io
import json
import sqlite3
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest

from a_share_research import deploy


@pytest.fixture
def operations(monkeypatch, tmp_path):
    """Model only host commands, container state and readiness."""
    def setup(*, existing=True, failure=None, paused=False, previous_config=False):
        data = tmp_path / 'data'
        data.mkdir()
        state = SimpleNamespace(data=data, evidence=tmp_path / 'release-evidence', events=[], paused=paused,
                                in_flight=0, env={'METHOD_CARDS_DIR': '/method-cards'},
                                tags={'web': 'old-web', 'worker': 'old-worker'})
        state.containers = dict(state.tags)
        if existing:
            state.containers['tools'] = 'old-worker'
        state.running = dict.fromkeys(state.containers, True)
        state.declared_hashes = {s: f'config-{s}' for s in state.containers}
        running_hashes = state.declared_hashes.copy()
        if previous_config:
            running_hashes['web'] = 'previous-web-config'

        def describe(service):
            return dict(image=state.containers[service], running=state.running[service],
                        config_hash=running_hashes[service])

        def run(*args):
            if args[:4] == ('docker', 'compose', '--profile', 'tools'):
                args = (*args[:2], *args[4:])
            state.events.append(('run', *args))
            if args[:2] == ('git', 'rev-parse'):
                return 'previous' if args[-1] == 'previous^{commit}' else 'revision'
            if args[:2] == ('git', 'merge-base'):
                return args[2]
            if args[:2] == ('git', 'status'):
                return ''
            if args[:2] == ('git', 'ls-tree'):
                return args[-1] if args[-2] == 'revision' else ''
            if args[:3] == ('docker', 'exec', 'a-share-research-worker'):
                return json.dumps({'ready': True})
            if args[:2] == ('docker', 'ps'):
                return 'tools' if existing else ''
            if args[:2] == ('docker', 'inspect'):
                return str(data)
            if args[:3] == ('docker', 'compose', 'config'):
                if '--hash' in args:
                    return f'{args[-1]} {state.declared_hashes[args[-1]]}'
                return json.dumps({'services': {'tools': {'environment': state.env}}})
            if args[:2] == ('docker', 'tag'):
                if args[-1].endswith(':latest'):
                    service = args[-1].removeprefix('a-share-research-').removesuffix(':latest')
                    state.tags[service] = args[2]
                return ''
            if args[:3] == ('docker', 'compose', 'stop'):
                for service in args[5:]:
                    state.running[service] = False
                return ''
            if args[:3] == ('docker', 'compose', 'start'):
                for service in args[3:]:
                    state.running[service] = True
                return ''
            if args[:3] == ('docker', 'compose', 'up'):
                for service in args[args.index('--no-deps') + 1:]:
                    state.containers[service] = state.tags['worker' if service == 'tools' else service]
                    state.running[service] = True
                    running_hashes[service] = state.declared_hashes[service]
                if 'tools' in args and (data / '.tools-paused').exists():
                    state.paused = True
                return ''
            raise AssertionError(f'unexpected operation: {args!r}')

        def control(action):
            state.events.append(('control', action))
            if action == 'pause':
                state.paused = True
                if failure == 'drain_timeout':
                    raise RuntimeError('injected drain timeout')
                state.in_flight = int(failure == 'not_drained')
            if action == 'resume':
                state.paused = False
                if failure == 'resume_lost':
                    raise RuntimeError('injected lost resume reply')
            return dict(paused=state.paused, in_flight=state.in_flight)

        def ready(*, paused=False):
            state.events.append(('tools_ready', paused))
            assert state.paused == paused
            return dict(paused=paused, in_flight=state.in_flight, database_ready=True)

        def backup(*args):
            state.events.append(('backup',))
            assert not any(state.running.values())
            if failure == 'backup':
                raise RuntimeError('injected backup failure')
            return {'files': 1}

        monkeypatch.setattr(deploy, 'release_lock', nullcontext)
        monkeypatch.setattr(deploy, 'run', run)
        monkeypatch.setattr(deploy, 'describe', describe)
        def release_images(code):
            version = 'old' if code == 'previous' else 'new'
            return {service: f'{version}-{service}' for service in deploy.SERVICES}
        monkeypatch.setattr(deploy, 'release_images', release_images)
        monkeypatch.setattr(deploy, 'tools_control', control)
        monkeypatch.setattr(deploy, 'wait_tools_ready', ready)
        monkeypatch.setattr(deploy, 'wait_ready', lambda: state.events.append(('web_ready',)))
        monkeypatch.setattr(deploy, 'idle', lambda database: state.events.append(('idle',)))
        monkeypatch.setattr(deploy, 'freeze_policy', lambda *a, **k: dict(required=False))
        monkeypatch.setattr(deploy, 'policy_probe', lambda **k: dict(applicable=False))
        monkeypatch.setattr(deploy, 'create_release_backup', backup)
        monkeypatch.setattr(deploy, 'prune_backups', lambda data: [])
        monkeypatch.setattr(deploy, 'prune_images', lambda images: {})
        return state
    return setup


def commands(state,verb):
    return [event for event in state.events if event[:4]==('run','docker','compose',verb)]


def index(state,prefix):
    return next(i for i,event in enumerate(state.events) if event[:len(prefix)]==prefix)


def test_existing_tools_is_automatically_included_and_not_resumed_before_verification(operations):
    state=operations()
    deploy.deploy('revision',state.evidence)
    assert state.containers=={'web':'new-web','worker':'new-worker','tools':'new-worker'}
    assert not state.paused
    assert index(state,('control','pause')) < index(state,('run','docker','compose','stop'))
    assert index(state,('backup',)) < index(state,('run','docker','tag','new-web'))
    assert index(state,('tools_ready',True))<index(state,('control','resume'))
    assert json.loads((state.evidence / 'worker-preflight.json').read_text())['ready']
    tags=[event for event in state.events if event[:3]==('run','docker','tag')]
    assert not any('a-share-research-tools:' in event[-1] for event in tags)


@pytest.mark.parametrize('failure',['drain_timeout','not_drained'])
def test_failed_drain_never_stops_services_or_resumes_unverified_work(operations,failure):
    state=operations(failure=failure)
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence)
    assert not commands(state,'stop')
    assert ('backup',) not in state.events
    assert state.paused
    assert ('control','resume') not in state.events
    assert state.containers=={'web':'old-web','worker':'old-worker','tools':'old-worker'}
    assert all(state.running.values())


def test_preexisting_pause_is_not_owned_by_release(operations):
    state=operations(paused=True)
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence)
    assert state.paused
    assert not commands(state,'stop')
    assert ('control','pause') not in state.events
    assert ('control','resume') not in state.events


def test_existing_tools_must_share_old_worker_image(operations):
    state=operations()
    state.containers['tools']='unrelated-old-image'
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence)
    assert not commands(state,'stop')
    assert ('control','pause') not in state.events


@pytest.mark.parametrize('allow_config', [False, True])
def test_tools_configuration_refuses_model_credentials_without_printing_values(operations,caplog,capsys,allow_config):
    state=operations()
    state.env['MODEL_API_KEY']='do-not-expose-private-value'
    with pytest.raises(RuntimeError) as error:
        deploy.deploy('revision',state.evidence,allow_config=allow_config)
    assert not commands(state,'stop')
    assert 'do-not-expose-private-value' not in str(error.value)+caplog.text+capsys.readouterr().out


@pytest.mark.parametrize('environment', [None, {'METHOD_CARDS_DIR': '/wrong-directory'}])
def test_tools_requires_the_configured_method_cards_directory_before_pause(operations, environment):
    state = operations()
    state.env = environment
    with pytest.raises(RuntimeError, match='METHOD_CARDS_DIR'):
        deploy.deploy('revision', state.evidence, allow_config=True)
    assert ('control', 'pause') not in state.events
    assert not commands(state, 'stop')


@pytest.mark.parametrize('offline', [False, True])
def test_republish_previous_commit_uses_the_restored_database_or_keeps_existing_pause(operations, local_policy, offline):
    state = operations()
    local = local_policy(state)
    backup = state.data / 'pre-release.db'
    if offline:
        with closing(sqlite3.connect(local.database)) as source, closing(sqlite3.connect(backup)) as copy:
            source.backup(copy)
    deploy.deploy('revision', state.evidence, approval_path=local.approve())
    if offline:
        # The pre-release backup contains the predecessor activation. The failed
        # worker must never open it before the previous image is started.
        with closing(sqlite3.connect(backup)) as copy, closing(sqlite3.connect(local.database)) as restored:
            copy.backup(restored)
        state.running = dict.fromkeys(state.running, False)
    else:
        state.paused = True
        local.approval.update(identity=local.old['identity'], expected_id=2, request_id='republish-previous')
    deploy.deploy('previous', state.evidence.parent / 'republished', approval_path=local.approve())
    assert state.containers == {'web': 'old-web', 'worker': 'old-worker', 'tools': 'old-worker'}
    assert state.paused is (not offline)
    assert local.count() == (1 if offline else 3)


def test_backup_failure_restarts_existing_services_without_retagging(operations):
    state=operations(failure='backup', previous_config=True)
    with pytest.raises(RuntimeError,match='backup failure'):
        deploy.deploy('revision',state.evidence,allow_config=True)
    assert state.containers == {'web':'old-web','worker':'old-worker','tools':'old-worker'}
    assert all(state.running.values()) and not state.paused
    assert state.tags=={'web':'old-web','worker':'old-worker'}


def test_without_tools_container_ordinary_release_keeps_two_services(operations):
    state=operations(existing=False)
    deploy.deploy('revision',state.evidence)
    assert state.containers=={'web':'new-web','worker':'new-worker'}
    assert not any(event[0]=='control' for event in state.events)


@pytest.mark.parametrize('allow_config', [False, True])
def test_changed_configuration_requires_authorization(operations, allow_config):
    state = operations(previous_config=True)
    if allow_config:
        deploy.deploy('revision', state.evidence, allow_config=True)
        assert not state.paused and state.containers['tools'] == 'new-worker'
    else:
        with pytest.raises(RuntimeError, match='Compose 或环境配置发生变化'):
            deploy.deploy('revision', state.evidence)
        assert not commands(state, 'stop')


def test_same_image_with_only_a_changed_service_hash_recreates_before_resuming(operations, monkeypatch):
    state = operations()
    monkeypatch.setattr(deploy, 'release_images', lambda _: dict(state.tags))
    state.declared_hashes['tools'] = 'changed-env'
    deploy.deploy('revision', state.evidence, allow_config=True)
    assert index(state, ('control', 'pause')) < index(state, ('run', 'docker', 'compose', 'up'))
    assert index(state, ('run', 'docker', 'compose', 'up')) < index(state, ('control', 'resume'))
    checkpoint = json.loads((state.evidence/'release-state.json').read_text())
    assert checkpoint['status'] == 'succeeded' and not state.paused


@pytest.mark.parametrize('approved', [False, 'standing', True])
def test_real_policy_release_approval_precedes_pause_and_gate_precedes_resume(operations, local_policy, approved, tmp_path, monkeypatch):
    state = operations()
    local = local_policy(state)
    if not approved:
        with pytest.raises(RuntimeError, match='常设 owner 凭据'):
            deploy.deploy('revision', state.evidence)
        assert not commands(state, 'stop') and ('control', 'pause') not in state.events
        assert local.count() == 1
        return
    if approved == 'standing':
        credential = tmp_path / 'owner.json'
        credential.write_text(json.dumps({'owner_token': local.token}))
        credential.chmod(0o600)
        monkeypatch.setattr(deploy, 'OWNER_CREDENTIAL', credential)
    deploy.deploy('revision', state.evidence, approval_path=local.approve() if approved is True else None)
    assert local.count() == 2 and not state.paused
    checkpoint = json.loads((state.evidence/'release-state.json').read_text())
    assert checkpoint['status'] == 'succeeded' and checkpoint['actual_policy']['installed'] is True
    assert checkpoint['actual_policy']['activation']['id'] == 2
    assert index(state, ('tools_ready', True)) < index(state, ('control', 'resume'))
    assert (state.evidence/'policy-gate.json').exists()
    assert local.token not in ''.join(p.read_text() for p in state.evidence.glob('*.json'))


def test_failed_policy_activation_keeps_tools_paused(operations, local_policy):
    state = operations()
    local = local_policy(state)
    local.failed_writes = True
    with pytest.raises(RuntimeError, match='activation failure'):
        deploy.deploy('revision', state.evidence, approval_path=local.approve())
    assert local.count() == 1 and state.paused
    assert ('control', 'resume') not in state.events


@pytest.mark.parametrize("failure,error,message", [
    ("resume_lost", RuntimeError, "lost resume reply"),
    ("evidence", OSError, "evidence failure"),
])
def test_release_failure_after_resume_pauses_tools_again(operations, monkeypatch, failure, error, message):
    state = operations(failure=failure)
    write = deploy.save_evidence

    def save(directory, name, value):
        if failure == "evidence" and name == 'release-state.json' and value['status'] == 'succeeded':
            raise OSError('injected evidence failure')
        return write(directory, name, value)

    monkeypatch.setattr(deploy, 'save_evidence', save)
    with pytest.raises(error, match=message):
        deploy.deploy('revision', state.evidence)
    assert ('control', 'resume') in state.events and state.paused
    assert state.containers['tools'] == 'new-worker'


def test_real_matched_same_image_never_adds_activation_or_pauses(operations, local_policy, monkeypatch):
    state = operations()
    local = local_policy(state)
    monkeypatch.setattr(deploy, 'release_images', lambda _: dict(web='old-web', worker='old-worker'))
    deploy.deploy('revision', state.evidence)
    assert local.count() == 1 and ('control', 'pause') not in state.events
    assert json.loads((state.evidence/'release-state.json').read_text())['status'] == 'succeeded'


def test_same_image_still_refuses_invalid_policy(operations, local_policy, monkeypatch):
    state = operations()
    local_policy(state)
    monkeypatch.setattr(deploy, 'release_images', lambda _: dict(web='old-web', worker='old-worker'))
    original = deploy.policy_probe
    monkeypatch.setattr(deploy, 'policy_probe', lambda **k: original(**k) | dict(valid=False))
    with pytest.raises(RuntimeError): deploy.deploy('revision', state.evidence)
    assert ('control', 'resume') not in state.events


def test_plan_is_read_only(operations, local_policy):
    state = operations()
    local = local_policy(state)
    deploy.deploy('revision', state.evidence, plan_policy=True)
    plan = json.loads((state.evidence/'policy-plan.json').read_text())
    assert plan['status'] == 'planned' and plan['policy']['target_identity'] == local.target['identity']
    assert not commands(state, 'stop') and ('control', 'pause') not in state.events
    assert not (state.evidence/'release-state.json').exists() and local.count() == 1


def test_tools_ready_trusts_the_running_image_schema_check(monkeypatch):
    # The running tools image checks its own database schema.
    answers = [dict(database_ready=False, schema_version=24, paused=True, in_flight=0),
               dict(database_ready=True, schema_version=24, paused=True, in_flight=0)]

    class Opener:
        def open(self, url, timeout):
            return io.BytesIO(json.dumps(answers.pop(0)).encode())
    monkeypatch.setattr(deploy, 'build_opener', lambda *a: Opener())
    monkeypatch.setattr(deploy.time, 'sleep', lambda s: None)
    assert deploy.wait_tools_ready(paused=True)['schema_version'] == 24
    assert not answers
