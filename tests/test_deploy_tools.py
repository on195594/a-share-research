"""Release-order simulations only: no Docker, live service or production acceptance."""
import json
from types import SimpleNamespace

import pytest

from a_share_research import deploy
from a_share_research.tools.control import Gate


@pytest.fixture
def operations(monkeypatch,tmp_path):
    """Model container images and local admission without mocking release decisions."""
    def setup(*, existing=True, failure=None, paused=False, compatible=True):
        data=tmp_path/'data'
        data.mkdir(exist_ok=True)
        state=SimpleNamespace(events=[], existing=existing, failure=failure, paused=paused,
            in_flight=0, data=data, env={'DATA_DIR':'/data','TUSHARE_TOKEN':'private-test-value',
                'ASHARE_TOOLS_EXTERNAL_ENABLED':'0','ASHARE_TOOLS_EXTERNAL_REVIEW_ENABLED':'0',
                'ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED':'0'},
            tags={'web':'old-web','worker':'old-worker'},
            containers={'web':'old-web','worker':'old-worker'},
            running={'web':True,'worker':True})
        if existing:
            state.containers['tools']='old-worker'
            state.running['tools']=True

        def record(name,*args):
            state.events.append((name,*args))

        def describe(service):
            record('describe',service,state.containers[service])
            return {'image':state.containers[service],'running':state.running[service],
                    'config_hash':f'config-{service}','cpus':0,'ports':{},
                    'mounts':{'/data':[str(data),True]}} if service!='tools' else {
                    'image':state.containers[service],'running':state.running[service],
                    'config_hash':'config-tools','cpus':400_000_000,
                    'ports':{'8621/tcp':[{'HostIp':'127.0.0.1','HostPort':'8621'}]},
                    'mounts':{'/data':[str(data),True],'/run/ashare-tools':['tmpfs',True]}}

        def run(*args):
            if args[:2]==('docker','compose') and '--hash' in args and args[-1]=='tools':
                # Real Compose 5.7.0 rejects this profiled service without explicitly enabling its profile.
                assert args[2:4]==('--profile','tools')
            if args[:4]==('docker','compose','--profile','tools'):
                args=(*args[:2],*args[4:])
            record('run',*args)
            if args[:2]==('git','rev-parse'):
                return 'revision'
            if args[:2]==('git','status'):
                return ''
            if args[:2]==('docker','ps'):
                return 'existing-tools-container' if 'tools' in state.containers else ''
            if args[:2]==('docker','tag'):
                source,destination=args[2:]
                if destination.endswith(':latest'):
                    service=destination.removeprefix('a-share-research-').removesuffix(':latest')
                    state.tags[service]=source
                return ''
            if args[:3]==('docker','compose','config'):
                if '--hash' in args:
                    return f'{args[-1]} config-{args[-1]}'
                return json.dumps({'services':{'tools':{'image':'a-share-research-worker',
                    'environment':state.env,'volumes':[{'source':str(data),'target':'/data','type':'bind'}]}}})
            if args[:3]==('docker','compose','stop'):
                for service in args[5:]:
                    state.running[service]=False
                return ''
            if args[:3]==('docker','compose','up'):
                services=args[args.index('--no-deps')+1:]
                for service in services:
                    state.containers[service]=state.tags['worker' if service=='tools' else service]
                    state.running[service]=True
                if 'tools' in services and not existing:
                    state.paused=Gate(state.data/'.tools-paused').status()['paused']
                    record('first_start_marker',state.paused)
                return ''
            if args[:3]==('docker','compose','start'):
                for service in args[3:]:
                    state.running[service]=True
                return ''
            if args[:3]==('docker','compose','rm') or args[:2]==('docker','rm'):
                state.containers.pop('tools',None)
                state.running.pop('tools',None)
                return ''
            if args[:3]==('docker','exec','a-share-research-worker'):
                if 'a_share_research.integrity' in args:
                    record('integrity')
                    return json.dumps({'ok':failure!='integrity'})
                record('preflight')
                if failure=='preflight':
                    raise RuntimeError('injected preflight failure')
                return json.dumps({'ok':True})
            raise AssertionError(f'unexpected operation: {args!r}')

        def control(action,*args,**kwargs):
            record('control',action)
            if action=='pause':
                state.paused=True
                if failure=='resume_lost_redrain_failed' and getattr(state,'resume_lost',False):
                    raise RuntimeError('injected rollback drain timeout')
                state.in_flight=0
                if failure=='drain_timeout':
                    state.in_flight=1
                    raise RuntimeError('injected drain timeout')
                if failure=='not_drained':
                    state.in_flight=1
            elif action=='resume':
                state.paused=False
                if (state.data/'.tools-paused').exists():
                    Gate(state.data/'.tools-paused').resume()
                if failure in ('resume_lost','resume_lost_redrain_failed') and not getattr(state,'resume_lost',False):
                    state.resume_lost=True
                    state.in_flight=1
                    raise RuntimeError('injected lost resume reply')
            return {'paused':state.paused,'in_flight':state.in_flight}

        def ready(*,paused=False):
            record('tools_ready',paused)
            assert state.paused==paused
            return {'paused':paused,'in_flight':state.in_flight,'ok':True}

        def backup(*args):
            record('backup')
            if failure=='backup':
                raise RuntimeError('injected backup failure')
            return {'rows':0}

        def assess(images,database,evidence_directory):
            record('rollback_probe')
            assert images=={'web':'old-web','worker':'old-worker'}
            assert database==data/'app.db'
            assert evidence_directory==state.evidence/'rollback-probe'
            assert not any(state.running.values())
            if 'tools' in state.containers:
                assert state.paused and state.in_flight==0
            return {'ok':compatible,'scope':'read_only','images':{
                service:{'ok':compatible,'error':None if compatible else 'incompatible_records'}
                for service in images}}

        monkeypatch.setattr(deploy,'run',run)
        monkeypatch.setattr(deploy,'describe',describe)
        monkeypatch.setattr(deploy,'verify_images',lambda code:{'web':'new-web','worker':'new-worker'})
        monkeypatch.setattr(deploy,'tools_control',control)
        monkeypatch.setattr(deploy,'wait_tools_ready',ready)
        monkeypatch.setattr(deploy,'wait_ready',lambda:record('web_ready'))
        monkeypatch.setattr(deploy,'idle',lambda database:record('idle'))
        monkeypatch.setattr(deploy,'fingerprints',lambda database:{'rows':'unchanged'})
        monkeypatch.setattr(deploy,'_source_version',lambda database:21)
        monkeypatch.setattr(deploy,'create_release_backup',backup)
        monkeypatch.setattr(deploy.rollback,'assess',assess)
        monkeypatch.setattr(deploy,'prune_backups',lambda data:[])
        monkeypatch.setattr(deploy,'prune_images',lambda images:{})
        state.evidence=tmp_path/'release-evidence'
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
    assert index(state,('control','status')) < index(state,('control','pause'))
    assert index(state,('control','pause')) < index(state,('run','docker','compose','stop'))
    stops=commands(state,'stop')
    assert {s for event in stops for s in event[6:]}=={'web','worker','tools'}
    assert max(state.events.index(event) for event in stops)<index(state,('backup',))
    assert index(state,('backup',))<index(state,('run','docker','tag','new-web'))
    assert any(event[-3:]==('web','worker','tools') for event in commands(state,'up'))
    assert index(state,('tools_ready',True))<index(state,('control','resume'))
    assert index(state,('preflight',))<index(state,('control','resume'))
    assert index(state,('integrity',))<index(state,('control','resume'))
    assert index(state,('describe','tools','new-worker'))<index(state,('control','resume'))
    tags=[event for event in state.events if event[:3]==('run','docker','tag')]
    assert not any('a-share-research-tools:' in event[-1] for event in tags)


@pytest.mark.parametrize('failure',['drain_timeout','not_drained'])
def test_failed_drain_never_stops_services_and_restores_admission(operations,failure):
    state=operations(failure=failure)
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence)
    assert not commands(state,'stop')
    assert ('backup',) not in state.events
    assert not state.paused
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


@pytest.mark.parametrize('failure',['preflight','integrity'])
def test_existing_tools_failure_rebuilds_old_services_and_restores_admission(operations,failure):
    state=operations(failure=failure)
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence)
    assert state.tags=={'web':'old-web','worker':'old-worker'}
    assert state.containers=={'web':'old-web','worker':'old-worker','tools':'old-worker'}
    assert not state.paused and all(state.running.values())
    assert not commands(state,'rm')
    assert commands(state,'up')[-1][-3:]==('web','worker','tools')
    assert index(state,('control','resume'))>index(state,('integrity',) if failure=='integrity' else ('preflight',))
    probe=index(state,('rollback_probe',))
    rollback_tag=index(state,('run','docker','tag','old-web','a-share-research-web:latest'))
    assert probe<rollback_tag<state.events.index(commands(state,'up')[-1],probe)<index(state,('control','resume'))
    assert json.loads((state.evidence/'rollback-compatibility.json').read_text())['ok'] is True


def test_first_tools_requires_explicit_configuration_authorization(operations):
    state=operations(existing=False)
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence,with_tools=True)
    assert not commands(state,'stop') and not commands(state,'up')
    assert 'tools' not in state.containers


def test_first_tools_failure_removes_only_new_container_and_own_marker(operations):
    state=operations(existing=False,failure='integrity')
    before=set(state.data.iterdir())
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence,allow_config=True,with_tools=True)
    assert state.tags=={'web':'old-web','worker':'old-worker'}
    assert state.containers=={'web':'old-web','worker':'old-worker'}
    assert all(state.running.values())
    removals=commands(state,'rm')
    assert len(removals)==1 and removals[0][-1]=='tools'
    assert set(state.data.iterdir())==before
    assert commands(state,'up')[-1][-2:]==('web','worker')
    assert index(state,('rollback_probe',))<state.events.index(removals[0])


def test_existing_tools_must_share_old_worker_image(operations):
    state=operations()
    state.containers['tools']='unrelated-old-image'
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence)
    assert not commands(state,'stop')
    assert ('control','pause') not in state.events


def test_tools_configuration_refuses_model_credentials_without_printing_values(operations,caplog,capsys):
    state=operations()
    state.env['MODEL_API_KEY']='do-not-expose-private-value'
    with pytest.raises(RuntimeError) as error:
        deploy.deploy('revision',state.evidence)
    assert not commands(state,'stop')
    assert 'do-not-expose-private-value' not in str(error.value)+caplog.text+capsys.readouterr().out


def test_lost_resume_reply_drains_again_before_rolling_back(operations):
    state=operations(failure='resume_lost')
    with pytest.raises(RuntimeError,match='lost resume reply'):
        deploy.deploy('revision',state.evidence)
    resumes=[i for i,event in enumerate(state.events) if event==('control','resume')]
    pauses=[i for i,event in enumerate(state.events) if event==('control','pause')]
    assert len(resumes)==len(pauses)==2
    assert pauses[0]<resumes[0]<pauses[1]<resumes[1]
    rollback_tag=next(i for i,event in enumerate(state.events) if i>resumes[0]
                      and event==('run','docker','tag','old-web','a-share-research-web:latest'))
    rollback_up=state.events.index(commands(state,'up')[-1],resumes[0])
    assert pauses[1]<index(state,('rollback_probe',))<rollback_tag<rollback_up<resumes[1]
    assert state.containers=={'web':'old-web','worker':'old-worker','tools':'old-worker'}
    assert not state.paused and state.in_flight==0


@pytest.mark.parametrize('first',[False,True])
def test_failed_redrain_after_lost_resume_never_replaces_or_removes_running_tools(operations,first):
    state=operations(existing=not first,failure='resume_lost_redrain_failed')
    with pytest.raises(RuntimeError,match='rollback drain timeout'):
        deploy.deploy('revision',state.evidence,allow_config=first,with_tools=first)
    resume=index(state,('control','resume'))
    remaining=state.events[resume+1:]
    assert remaining==[('control','pause')]
    assert not commands(state,'rm')
    assert len(commands(state,'up'))==1
    assert state.tags=={'web':'new-web','worker':'new-worker'}
    assert state.containers=={'web':'new-web','worker':'new-worker','tools':'new-worker'}
    assert state.paused and state.in_flight==1


def test_backup_failure_restarts_existing_services_without_retagging(operations):
    state=operations(failure='backup')
    with pytest.raises(RuntimeError,match='backup failure'):
        deploy.deploy('revision',state.evidence)
    assert commands(state,'start')[-1][-3:]==('web','worker','tools')
    assert not commands(state,'up')
    assert all(state.running.values()) and not state.paused
    assert state.tags=={'web':'old-web','worker':'old-worker'}
    assert not any(event[0]=='rollback_probe' for event in state.events)


def test_first_tools_backup_failure_cleans_marker_without_removing_any_container(operations):
    state=operations(existing=False,failure='backup')
    with pytest.raises(RuntimeError,match='backup failure'):
        deploy.deploy('revision',state.evidence,allow_config=True,with_tools=True)
    assert not commands(state,'rm')
    assert not commands(state,'up')
    assert not (state.data/'.tools-paused').exists()
    assert state.containers=={'web':'old-web','worker':'old-worker'}
    assert all(state.running.values())
    assert not any(event[0]=='rollback_probe' for event in state.events)


@pytest.mark.parametrize('first',[False,True])
@pytest.mark.parametrize('failure',['integrity','resume_lost'])
def test_incompatible_old_reader_keeps_current_images_and_admission_paused(operations,first,failure):
    state=operations(existing=not first,failure=failure,compatible=False)
    with pytest.raises(RuntimeError,match='旧镜像读取兼容检查未通过'):
        deploy.deploy('revision',state.evidence,allow_config=first,with_tools=first)
    assert state.tags=={'web':'new-web','worker':'new-worker'}
    assert state.containers=={'web':'new-web','worker':'new-worker','tools':'new-worker'}
    assert not any(state.running.values())
    assert state.paused and state.in_flight==0
    assert not commands(state,'rm') and not commands(state,'start')
    assert len(commands(state,'up'))==1
    assert state.events[-1]==('rollback_probe',)
    report=json.loads((state.evidence/'rollback-compatibility.json').read_text())
    assert report=={'ok':False,'scope':'read_only','images':{
        service:{'ok':False,'error':'incompatible_records'} for service in ('web','worker')}}
    resumes=[i for i,event in enumerate(state.events) if event==('control','resume')]
    if failure=='resume_lost':
        assert len(resumes)==1
        assert state.events[resumes[0]+1]==('control','pause')
    else:
        assert not resumes
        if first:
            assert (state.data/'.tools-paused').exists()


def test_two_service_rollback_also_requires_old_reader_compatibility(operations):
    state=operations(existing=False,failure='integrity',compatible=False)
    with pytest.raises(RuntimeError,match='旧镜像读取兼容检查未通过'):
        deploy.deploy('revision',state.evidence)
    assert state.tags=={'web':'new-web','worker':'new-worker'}
    assert state.containers=={'web':'new-web','worker':'new-worker'}
    assert not any(state.running.values())
    assert not any(event[0]=='control' for event in state.events)
    assert not commands(state,'rm') and not commands(state,'start')
    assert len(commands(state,'up'))==1
    assert state.events[-1]==('rollback_probe',)


def test_first_tools_never_removes_a_preexisting_pause_marker(operations):
    state=operations(existing=False)
    marker=state.data/'.tools-paused'
    marker.write_text('unrelated previous release marker')
    with pytest.raises(RuntimeError):
        deploy.deploy('revision',state.evidence,allow_config=True,with_tools=True)
    assert marker.read_text()=='unrelated previous release marker'
    assert not commands(state,'stop')


def test_without_tools_container_ordinary_release_keeps_two_services(operations):
    state=operations(existing=False)
    deploy.deploy('revision',state.evidence)
    assert state.containers=={'web':'new-web','worker':'new-worker'}
    assert not any(event[0]=='control' for event in state.events)
    assert commands(state,'up')[-1][-2:]==('web','worker')


def test_first_tools_starts_paused_and_only_resumes_after_checks(operations):
    state=operations(existing=False)
    deploy.deploy('revision',state.evidence,allow_config=True,with_tools=True)
    assert ('first_start_marker',True) in state.events
    assert state.containers=={'web':'new-web','worker':'new-worker','tools':'new-worker'}
    assert not state.paused and not (state.data/'.tools-paused').exists()
    assert index(state,('first_start_marker',True))<index(state,('tools_ready',True))
    assert index(state,('integrity',))<index(state,('control','resume'))
    assert index(state,('describe','tools','new-worker'))<index(state,('control','resume'))
    assert ('control','pause') not in state.events
    assert commands(state,'up')[-1][-3:]==('web','worker','tools')


@pytest.mark.parametrize('control_mount', [['tmpfs', False], ['/private/bind-control', True]])
def test_release_rejects_non_writable_or_non_tmpfs_control_mount(operations, monkeypatch, control_mount):
    state = operations()
    original = deploy.describe
    def describe(service):
        value = original(service)
        if service == 'tools':
            value['mounts']['/run/ashare-tools'] = control_mount
        return value
    monkeypatch.setattr(deploy, 'describe', describe)
    with pytest.raises(RuntimeError, match='tools 镜像、资源、挂载或回环端口与约定不符'):
        deploy.deploy('revision', state.evidence)
    assert state.tags == {'web': 'old-web', 'worker': 'old-worker'}
    assert ('rollback_probe',) in state.events
