"""Policy persistence/authorization contracts; empty account inputs are structural fixtures."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db, gate
from a_share_research.policy import identity, registry
from a_share_research.policy.v0 import risk_budget
from a_share_research.tools.server import create_app


@pytest.fixture
def setup(tmp_path):
    path = tmp_path/'app.db'
    conn = db.connect(path)
    tokens = {role:api_keys.create(conn,role,role=role) for role in ('owner','assistant','research')}
    principals = {role:api_keys.authenticate(conn,token) for role,token in tokens.items()}
    parameters = risk_budget.policy()
    manifest = registry.register(conn,principals['owner'],parameters)
    yield path,conn,tokens,principals,manifest
    conn.close()


def activate(setup, **overrides):
    _,conn,_,principals,manifest = setup
    args = dict(expected_id=0,request_id='first',reason='explicit test activation') | overrides
    return registry.activate(conn,principals['owner'],manifest['identity'],**args)


def test_register_is_immutable_idempotent_and_does_not_activate(setup):
    _,conn,_,principals,manifest = setup
    assert registry.register(conn,principals['owner'],manifest['parameters']) == manifest
    assert registry.current(conn,principals['assistant']) == dict(activation=None,manifest=None,installed=False)
    assert conn.execute('SELECT COUNT(*) FROM policy_versions').fetchone()[0] == 1
    for sql in ('UPDATE policy_versions SET policy="v1"','DELETE FROM policy_versions'):
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):
            conn.execute(sql)


def test_owner_activation_compare_swap_and_historical_retry(setup):
    _,conn,_,principals,manifest = setup
    first = activate(setup)
    second = activate(setup,expected_id=first['id'],request_id='second')
    assert second['id'] > first['id']
    assert activate(setup) == first  # Replay must not move the pointer back.
    assert registry.current(conn,principals['assistant'])['activation'] == second
    with pytest.raises(registry.PolicyError,match='已变化'):
        activate(setup,request_id='stale')
    with pytest.raises(registry.PolicyError,match='不同政策切换'):
        activate(setup,reason='changed request')
    assert registry.inspect(conn) == []
    assert 'guides' not in registry.current(conn,principals['assistant'])['manifest']


def test_guide_comes_from_active_snapshot_and_never_opens_arbitrary_path(setup):
    _,conn,_,principals,manifest = setup
    with pytest.raises(registry.PolicyError,match='尚未启用'):
        registry.guide(conn,principals['assistant'],None)
    activate(setup)
    result = registry.guide(conn,principals['assistant'],None)
    name = result['paths'][0]
    assert registry.guide(conn,principals['assistant'],name)['content'] == manifest['guides'][name]
    with pytest.raises(registry.PolicyError):
        registry.guide(conn,principals['assistant'],'../../../../.env')


def test_code_drift_blocks_new_activation_but_keeps_saved_guide(setup,monkeypatch):
    _,conn,_,principals,manifest = setup
    activate(setup)
    def drift(_):
        raise identity.IdentityError('explicit structural drift')
    monkeypatch.setattr(identity,'check_installed',drift)
    assert registry.current(conn,principals['assistant'])['installed'] is False
    assert registry.guide(conn,principals['assistant'],None)['identity'] == manifest['identity']
    with pytest.raises(registry.PolicyError,match='不一致'):
        activate(setup,expected_id=1,request_id='drift')
    assert registry.inspect(conn)==[]  # Old identities remain verifiable without matching current code.


def test_owner_gate_and_revocation(setup):
    _,conn,_,principals,manifest = setup
    for role in ('assistant','research'):
        with pytest.raises(registry.PolicyError) as error:
            registry.register(conn,principals[role],manifest['parameters'])
        assert error.value.status==403
        with pytest.raises(registry.PolicyError):
            registry.activate(conn,principals[role],manifest['identity'],expected_id=0,request_id=role,reason='no')
    with pytest.raises(registry.PolicyError):
        registry.current(conn,principals['research'])
    conn.execute('DELETE FROM api_keys WHERE id=?',(principals['owner'].key_id,))
    with pytest.raises(registry.PolicyError):
        activate(setup)


def test_no_remote_activation_and_read_tools_require_explicit_shadow_enable(setup):
    path,conn,tokens,principals,_ = setup
    activate(setup)
    with TestClient(create_app(path,account_shadow_enabled=True)) as client:
        for role in tokens:
            headers={'Authorization':'Bearer '+tokens[role]}
            listed=client.get('/tools',headers=headers).json()['tools']
            names={t['name'] for t in listed}
            assert 'policy.activate' not in names and 'policy.register' not in names
            response=client.post('/tools/policy.current',headers=headers,json={})
            assert response.status_code==(403 if role=='research' else 200)
            if role!='research':
                assert response.json()['result']['data']['installed'] is True
            assert client.post('/tools/policy.activate',headers=headers,json={}).status_code==404
    with TestClient(create_app(path)) as client:
        assert client.post('/tools/policy.current',headers={'Authorization':'Bearer '+tokens['owner']},json={}).status_code==404


def test_migration_preserves_legacy_pointer_and_is_reentrant(tmp_path):
    path=tmp_path/'old.db'
    conn=db.connect(path)
    conn.execute('DROP TABLE policy_activations')
    conn.execute('CREATE TABLE policy_activations (id INTEGER PRIMARY KEY AUTOINCREMENT, policy TEXT NOT NULL,'
                 ' activated_at TEXT NOT NULL, activated_by TEXT NOT NULL, identity TEXT NOT NULL)')
    conn.execute('INSERT INTO policy_activations(policy,activated_at,activated_by,identity) VALUES (?,?,?,?)',
                 ('v0','legacy-time','legacy-owner','legacy-profile-only'))
    before=tuple(conn.execute('SELECT * FROM policy_activations').fetchone())
    db._migrate_v19(conn)
    assert tuple(conn.execute('SELECT * FROM policy_activations').fetchone())==before+(None,None,None)
    db._migrate_v19(conn)
    assert tuple(conn.execute('SELECT * FROM policy_activations').fetchone())==before+(None,None,None)
    owner=api_keys.authenticate(conn,api_keys.create(conn,'owner',role='owner'))
    with pytest.raises(registry.PolicyError,match='未登记'):
        registry.current(conn,owner)
    assert registry.inspect(conn)==[]
    assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    conn.close()


def test_inspection_detects_corrupt_manifest_without_executing_it(setup):
    _,conn,_,_,manifest=setup
    conn.execute('DROP TRIGGER policy_versions_no_update')
    manifest['parameters']['max_position_risk_pct']=99
    conn.execute('UPDATE policy_versions SET manifest=?',(gate.canonical(manifest),))
    assert registry.inspect(conn)


@pytest.fixture
def bundle():
    return dict(local=dict(holdings=[],closed_count=0),quotes={},portfolio_value=None,industry_result=None,
                now='2026-11-11T12:00:00+08:00',risk_policy=risk_budget.policy(),denominator_evidence={},
                account_scope_required=False,research_result={'status':'not_configured','cards':[]})


def test_replay_binds_inputs_identity_and_activation_and_is_idempotent(setup,bundle):
    _,conn,_,principals,manifest=setup
    first=registry.replay(conn,principals['assistant'],manifest['identity'],'replay-before',bundle)
    assert first['activation_id'] is None and first['mode']=='supplied_input_replay'
    activation=activate(setup)
    receipt=registry.replay(conn,principals['assistant'],manifest['identity'],'replay-active',bundle)
    assert receipt['activation_id']==activation['id']
    assert receipt['input_hash']==gate.digest(bundle)
    assert receipt['result_hash']==gate.digest(receipt['result'])
    assert registry.replay(conn,principals['assistant'],manifest['identity'],'replay-active',bundle)==receipt
    assert registry.evaluation(conn,principals['assistant'],receipt['id'])==receipt
    assert registry.inspect(conn)==[]
    with pytest.raises(registry.PolicyError,match='不同评估'):
        registry.replay(conn,principals['assistant'],manifest['identity'],'replay-active',bundle|{'portfolio_value':100})
    for sql in ('DELETE FROM policy_evaluations','UPDATE policy_evaluations SET result="{}"'):
        with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)


def test_bad_parameters_or_input_never_save_evaluation(setup,bundle):
    _,conn,_,principals,manifest=setup
    for bad in (bundle|{'risk_policy':risk_budget.policy(3,9)}, bundle|{'now':'not a date'},
                bundle|{'local':{'holdings':[{}],'closed_count':0}}):
        with pytest.raises(registry.PolicyError):
            registry.replay(conn,principals['assistant'],manifest['identity'],'bad',bad)
    assert conn.execute('SELECT COUNT(*) FROM policy_evaluations').fetchone()[0]==0
    with pytest.raises(registry.PolicyError):
        registry.replay(conn,principals['research'],manifest['identity'],'forbidden',bundle)


def test_replay_failure_rolls_back_whole_receipt(setup,bundle):
    _,conn,_,principals,manifest=setup
    conn.execute("CREATE TRIGGER fail_eval BEFORE INSERT ON policy_evaluations BEGIN SELECT RAISE(ABORT,'injected storage fault'); END")
    with pytest.raises(sqlite3.IntegrityError,match='injected storage fault'):
        registry.replay(conn,principals['assistant'],manifest['identity'],'fault',bundle)
    assert conn.execute('SELECT COUNT(*) FROM policy_evaluations').fetchone()[0]==0
    assert not conn.in_transaction


def test_http_replay_uses_same_identity_and_never_grants_activation(setup,bundle):
    path,conn,tokens,principals,manifest=setup
    activate(setup)
    headers={'Authorization':'Bearer '+tokens['assistant']}
    payload={'identity':manifest['identity'],'request_id':'http-replay','bundle':bundle}
    with TestClient(create_app(path,account_shadow_enabled=True)) as client:
        first=client.post('/tools/policy.replay',headers=headers,json=payload)
        assert first.status_code==200
        receipt=first.json()['result']['data']
        assert receipt['identity']==manifest['identity']
        assert client.post('/tools/policy.replay',headers=headers,json=payload).json()==first.json()
        assert client.post('/tools/policy.evaluation',headers=headers,json={'evaluation_id':receipt['id']}).json()==first.json()
        assert client.post('/tools/policy.replay',headers={'Authorization':'Bearer '+tokens['research']},json=payload).status_code==403
        bad=payload|{'request_id':'bad','bundle':bundle|{'now':'invalid'}}
        assert client.post('/tools/policy.replay',headers=headers,json=bad).status_code==422
    assert registry.inspect(conn)==[]


def test_unapplied_parameters_can_be_archived_but_not_activated(setup):
    _,conn,_,principals,_=setup
    marker='not_applied_account_scope_required'
    params=dict(policy_id='unconfirmed-scope',source=marker,status=marker,max_position_risk_pct=None,
                max_portfolio_risk_pct=None,field_sources=dict(max_position_risk_pct=marker,max_portfolio_risk_pct=marker))
    manifest=registry.register(conn,principals['owner'],params)
    with pytest.raises(registry.PolicyError,match='不能启用'):
        registry.activate(conn,principals['owner'],manifest['identity'],expected_id=0,request_id='unapplied',reason='test')


def test_concurrent_owner_switches_allow_only_one_expected_pointer(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path,conn,_,principals,manifest=setup
    barrier=Barrier(2)
    def switch(request_id):
        other=sqlite3.connect(path,isolation_level=None,timeout=5)
        other.execute('PRAGMA foreign_keys=ON')
        try:
            barrier.wait(timeout=5)
            try:
                return registry.activate(other,principals['owner'],manifest['identity'],expected_id=0,
                                         request_id=request_id,reason='concurrent structural test')['id']
            except registry.PolicyError as exc:
                assert exc.status==409
                return None
        finally: other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(switch,('first-owner-choice','competing-owner-choice')))
    assert sum(value is not None for value in results)==1
    assert conn.execute('SELECT COUNT(*) FROM policy_activations').fetchone()[0]==1


def test_activation_checks_current_parameter_validity(setup,monkeypatch):
    _,conn,_,principals,_=setup
    monkeypatch.setattr(db,'now',lambda:'2026-11-11T12:00:00+08:00')
    # Explicit parameter-only structural variant; no trade/market evidence is invented.
    parameters=dict(policy_id='expired-structural',source='policy_file',account_scope='fixture',currency='CNY',
                    effective_from='2026-01-01T00:00:00+08:00',effective_to='2026-02-01T00:00:00+08:00',
                    confirmed_at='2026-01-01T00:00:00+08:00',confirmation_ref='structural-only',
                    max_position_risk_pct=2.,max_portfolio_risk_pct=8.,
                    field_sources=dict(max_position_risk_pct='policy_file',max_portfolio_risk_pct='policy_file'))
    manifest=registry.register(conn,principals['owner'],parameters)
    with pytest.raises(registry.PolicyError,match='到期'):
        registry.activate(conn,principals['owner'],manifest['identity'],expected_id=0,request_id='expired',reason='test')
    assert conn.execute('SELECT COUNT(*) FROM policy_activations').fetchone()[0]==0
