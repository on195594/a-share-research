"""Persistence/transport contracts with an explicitly synthetic empty account; no market calls."""
from copy import deepcopy
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from a_share_research import api_keys, db, gate
from a_share_research.account import mirror
from a_share_research.policy import monitor, registry
from a_share_research.policy.v0 import risk_budget


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path=tmp_path/'app.db'
    conn=db.connect(path)
    principals={role:api_keys.authenticate(conn,api_keys.create(conn,role,role=role))
                for role in ('owner','assistant','research')}
    parameters=risk_budget.policy()
    manifest=registry.register(conn,principals['owner'],parameters)
    activation=registry.activate(conn,principals['owner'],manifest['identity'],expected_id=0,
                                 request_id='structural-activation',reason='empty-account transport fixture')
    sha=mirror.digest(mirror._read(conn,tmp_path))
    conn.execute('INSERT INTO account_source VALUES (1,?,?,?,?)',('mirror',sha,db.now(),'{}'))
    bundle=dict(local=dict(holdings=[],closed_count=0),quotes={},portfolio_value=None,industry_result=None,
                now=db.now(),risk_policy=parameters,denominator_evidence={},account_scope_required=False,
                research_result={'status':'not_configured','cards':[]})
    captured=[]
    def capture(database, actual_parameters):
        captured.append((database,actual_parameters))
        assert not conn.in_transaction  # Network collection must not retain the caller's database lock.
        return deepcopy(bundle)
    monkeypatch.setattr(monitor,'capture',capture)
    yield path,conn,principals,manifest,activation,bundle,captured
    conn.close()


def run(setup, request_id='monitor-once', expected=None, role='assistant'):
    path,conn,principals,_,activation,_,_=setup
    return monitor.run(conn,principals[role],path,request_id,activation['id'] if expected is None else expected)


def test_capture_receipt_binds_identity_source_inputs_and_is_idempotent(setup):
    path,conn,principals,manifest,activation,bundle,captured=setup
    result=run(setup)
    assert result['mode']=='service_monitor_capture'
    assert result['identity']==manifest['identity'] and result['activation_id']==activation['id']
    assert result['input']==bundle and result['input_hash']==gate.digest(bundle)
    assert result['result_hash']==gate.digest(result['result'])
    context=result['capture_context']
    assert context['source']['source_sha256']==mirror.digest(mirror._read(conn,path.parent))
    assert context['source']['mode']=='mirror'
    assert context['request']=={'action':'policy.monitor','expected_activation_id':activation['id']}
    assert context['capture_finished_at']>=context['capture_started_at']
    assert run(setup)==result
    assert len(captured)==1
    assert registry.evaluation(conn,principals['assistant'],result['id'])==result
    assert registry.inspect(conn)==[]


def test_request_id_cannot_cross_replay_and_service_capture(setup):
    _,conn,principals,manifest,_,bundle,captured=setup
    registry.replay(conn,principals['assistant'],manifest['identity'],'replayed',bundle)
    with pytest.raises(registry.PolicyError,match='不同评估'):
        run(setup,'replayed')
    assert captured==[]
    run(setup,'captured')
    with pytest.raises(registry.PolicyError,match='不同评估'):
        registry.replay(conn,principals['assistant'],manifest['identity'],'captured',bundle)
    with pytest.raises(registry.PolicyError,match='不同评估'):
        run(setup,'captured',expected=999)
    assert len(captured)==1


@pytest.mark.parametrize('case',['missing','ledger','hash','asset-file','wrong-pointer','unauthorized'])
def test_invalid_source_or_access_fails_before_collection(setup,case):
    path,conn,_,_,_,_,captured=setup
    if case=='missing': conn.execute('DELETE FROM account_source')
    if case=='ledger': conn.execute("UPDATE account_source SET mode='ledger'")
    if case=='hash': conn.execute("UPDATE account_source SET source_sha256='tampered'")
    if case=='asset-file':
        # Explicitly malformed transport input; it is never used as financial evidence.
        (path.parent/'account-snapshot.json').write_text('{}')
    with pytest.raises(registry.PolicyError):
        run(setup,expected=999 if case=='wrong-pointer' else None,role='research' if case=='unauthorized' else 'assistant')
    assert captured==[]
    assert conn.execute('SELECT COUNT(*) FROM policy_evaluations').fetchone()[0]==0


def test_no_activation_never_collects(setup):
    _,conn,_,_,_,_,captured=setup
    conn.execute('DROP TRIGGER policy_activations_no_delete')
    conn.execute('DELETE FROM policy_activations')
    with pytest.raises(registry.PolicyError,match='尚未启用'):
        run(setup)
    assert captured==[]


@pytest.mark.parametrize('change',['policy','mirror','revocation'])
def test_changes_during_capture_reject_without_receipt(setup,monkeypatch,change):
    path,conn,principals,manifest,activation,bundle,_=setup
    def capture(*_):
        other=sqlite3.connect(path,isolation_level=None)
        try:
            if change=='policy':
                registry.activate(other,principals['owner'],manifest['identity'],expected_id=activation['id'],
                                  request_id='during-capture',reason='concurrent owner choice')
            elif change=='mirror':
                other.execute("UPDATE account_source SET updated_at='changed-during-capture'")
            else:
                other.execute('DELETE FROM api_keys WHERE id=?',(principals['assistant'].key_id,))
        finally: other.close()
        return deepcopy(bundle)
    monkeypatch.setattr(monitor,'capture',capture)
    with pytest.raises(registry.PolicyError) as error:
        run(setup)
    assert error.value.status==(403 if change=='revocation' else 409)
    assert conn.execute('SELECT COUNT(*) FROM policy_evaluations').fetchone()[0]==0
    assert not conn.in_transaction


def test_collection_failure_and_storage_fault_do_not_save_receipt(setup,monkeypatch):
    _,conn,_,_,_,bundle,_=setup
    def failed(*_): raise registry.PolicyError('structural timeout',504)
    monkeypatch.setattr(monitor,'capture',failed)
    with pytest.raises(registry.PolicyError): run(setup)
    assert not conn.in_transaction
    monkeypatch.setattr(monitor,'capture',lambda *_:deepcopy(bundle))
    conn.execute("CREATE TRIGGER fail_capture BEFORE INSERT ON policy_evaluations BEGIN SELECT RAISE(ABORT,'injected capture storage fault'); END")
    with pytest.raises(sqlite3.IntegrityError,match='injected capture storage fault'): run(setup)
    assert conn.execute('SELECT COUNT(*) FROM policy_evaluations').fetchone()[0]==0
    assert not conn.in_transaction


def test_bad_collector_input_never_saves(setup,monkeypatch):
    _,conn,_,_,_,bundle,_=setup
    monkeypatch.setattr(monitor,'capture',lambda *_:bundle|{'risk_policy':risk_budget.policy(3,9)})
    with pytest.raises(registry.PolicyError) as error: run(setup)
    assert error.value.status==422
    assert conn.execute('SELECT COUNT(*) FROM policy_evaluations').fetchone()[0]==0


def test_context_tamper_is_found_in_read_and_inspection(setup):
    _,conn,principals,_,_,_,_=setup
    result=run(setup)
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    conn.execute("UPDATE policy_evaluations SET capture_context='{}'")
    with pytest.raises(registry.PolicyError,match='哈希'):
        registry.evaluation(conn,principals['assistant'],result['id'])
    assert registry.inspect(conn)


def test_concurrent_same_request_returns_one_committed_receipt(setup,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path,conn,principals,_,activation,bundle,_=setup
    barrier=Barrier(2)
    def capture(*_):
        barrier.wait(timeout=5)
        return deepcopy(bundle)
    monkeypatch.setattr(monitor,'capture',capture)
    def invoke(_):
        other=sqlite3.connect(path,isolation_level=None,timeout=5)
        other.execute('PRAGMA foreign_keys=ON')
        try: return monitor.run(other,principals['assistant'],path,'concurrent',activation['id'])
        finally: other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(invoke,range(2)))
    assert results[0]==results[1]
    assert conn.execute('SELECT COUNT(*) FROM policy_evaluations').fetchone()[0]==1
    assert registry.inspect(conn)==[]


def test_v20_additive_columns_preserve_prior_replay_and_rerun(setup):
    _,conn,principals,manifest,_,bundle,_=setup
    registry.replay(conn,principals['assistant'],manifest['identity'],'before-capture-columns',bundle)
    conn.execute('ALTER TABLE policy_evaluations DROP COLUMN capture_context_hash')
    conn.execute('ALTER TABLE policy_evaluations DROP COLUMN capture_context')
    before=tuple(conn.execute('SELECT * FROM policy_evaluations').fetchone())
    assert len(before)==10
    db._migrate_v20(conn)
    assert tuple(conn.execute('SELECT * FROM policy_evaluations').fetchone())==before+(None,None)
    db._migrate_v20(conn)
    assert tuple(conn.execute('SELECT * FROM policy_evaluations').fetchone())==before+(None,None)
    assert registry.evaluation(conn,principals['assistant'],before[0])['mode']=='supplied_input_replay'


@pytest.mark.parametrize('source',[None,{'mode':'ledger','watermark':{'account_id':'structural-transport-only'}}])
def test_fixed_child_transport_drops_inherited_credentials_and_config(tmp_path,monkeypatch,source):
    for key in ('ASHARE_OWNER_TOKEN','OPENAI_API_KEY','TUSHARE_TOKEN','POLICY_V0_ACCOUNT_SHADOW','A_STOCK_CONFIG_FILE'):
        monkeypatch.setenv(key,'must-not-propagate')
    database=tmp_path/'app.db'
    parameters=risk_budget.policy()
    seen={}
    class Process:
        returncode=0
        def communicate(self,payload=None,timeout=None):
            seen['payload']=json.loads(payload)
            seen['timeout']=timeout
            return b'{"transport_fixture":true}',b''
    def popen(argv,**kwargs):
        seen.update(argv=argv,kwargs=kwargs)
        config=Path(kwargs['env']['A_STOCK_CONFIG_FILE'])
        assert config.read_bytes()==b'' and config.stat().st_mode&0o777==0o600
        return Process()
    monkeypatch.setattr(monitor.subprocess,'Popen',popen)
    assert monitor.capture(database,parameters,source=source)=={'transport_fixture':True}
    assert seen['argv']==[sys.executable,'-m','a_share_research.policy.capture']
    assert seen['payload']==(parameters if source is None else dict(parameters=parameters,source=source))
    assert seen['timeout']==25
    env=seen['kwargs']['env']
    assert set(env)<= {'PATH','HOME','LANG','LC_ALL','POLICY_V0_DB','A_STOCK_CONFIG_FILE'}
    assert env['POLICY_V0_DB']==str(database.resolve())
    assert seen['kwargs']['start_new_session'] is True
    assert not Path(env['A_STOCK_CONFIG_FILE']).exists()


@pytest.mark.parametrize('already_exited',[False,True])
def test_timeout_kills_only_collector_process_group_and_waits(tmp_path,monkeypatch,already_exited):
    calls=[]
    class Process:
        pid=123456789  # Never signalled: os.killpg is intercepted below.
        def communicate(self,payload=None,timeout=None):
            calls.append((payload,timeout))
            if len(calls)==1: raise subprocess.TimeoutExpired('fixed-collector',timeout)
            return b'',b''
    monkeypatch.setattr(monitor.subprocess,'Popen',lambda *a,**kw:Process())
    killed=[]
    def terminate(pid,sig):
        killed.append((pid,sig))
        if already_exited: raise ProcessLookupError('child finished at deadline')
    monkeypatch.setattr(monitor.os,'killpg',terminate)
    with pytest.raises(registry.PolicyError) as error:
        monitor.capture(tmp_path/'app.db',risk_budget.policy())
    assert error.value.status==504
    assert killed==[(Process.pid,signal.SIGKILL)] and len(calls)==2 and calls[1]==(None,None)


@pytest.mark.parametrize('output',[b'[]',b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":1e999}',b'\xff',b'not-json'])
def test_collector_invalid_json_never_returns_input(tmp_path,monkeypatch,output):
    class Process:
        returncode=0
        def communicate(self,*a,**kw): return output,b'private-provider-detail'
    monkeypatch.setattr(monitor.subprocess,'Popen',lambda *a,**kw:Process())
    with pytest.raises(registry.PolicyError) as error:
        monitor.capture(tmp_path/'app.db',risk_budget.policy())
    assert error.value.status==503
    assert 'private-provider-detail' not in str(error.value)


def test_collector_failure_does_not_echo_provider_output(tmp_path,monkeypatch):
    class Process:
        returncode=1
        def communicate(self,*a,**kw): return b'private-response',b'private-token'
    monkeypatch.setattr(monitor.subprocess,'Popen',lambda *a,**kw:Process())
    with pytest.raises(registry.PolicyError) as error:
        monitor.capture(tmp_path/'app.db',risk_budget.policy())
    assert error.value.status==503
    assert 'private' not in str(error.value)


def test_real_capture_process_with_explicit_empty_account_needs_no_market_request(tmp_path):
    # Structural transport proof only: zero securities means the shipping quote
    # function returns before any network access. It is not financial evidence.
    path=tmp_path/'app.db'
    conn=db.connect(path)
    conn.close()
    before=path.read_bytes()
    bundle=monitor.capture(path,risk_budget.policy())
    assert bundle['local']=={'holdings':[],'closed_count':0}
    assert bundle['quotes']=={} and bundle['industry_result'] is None
    assert bundle['portfolio_value'] is None
    assert path.read_bytes()==before
