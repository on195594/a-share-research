"""Owner-only presentation reads stored policy facts; fixtures are not investment policies."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from a_share_research import api_keys,db,gate
from a_share_research.policy import identity,presentation,registry


NOW=datetime.fromisoformat('2026-11-11T12:00:00+08:00')


@pytest.fixture
def book(tmp_path):
    path=tmp_path/'app.db'
    conn=db.connect(path)
    principal=api_keys.authenticate(conn,api_keys.create(conn,'structural-owner',role='owner'))
    yield path,conn,principal
    conn.close()


def install(book,**changes):
    _,conn,principal=book
    parameters=dict(policy_id='structural-policy',source='policy_file',account_scope='structural-account',
        currency='CNY',effective_from='2026-01-01T00:00:00+08:00',effective_to=None,
        confirmed_at='2026-01-01T00:00:00+08:00',confirmation_ref='structural-test',
        max_position_risk_pct=2.,max_portfolio_risk_pct=8.,
        field_sources=dict(max_position_risk_pct='policy_file',max_portfolio_risk_pct='policy_file'))|changes
    manifest=identity.build(parameters)
    conn.execute('INSERT INTO policy_versions VALUES (?,?,?,?,?)',
        (manifest['identity'],'v0',db.now(),principal.key_id,gate.canonical(manifest)))
    # Direct fixture construction permits testing historical/future pointers without changing activation's gate.
    conn.execute('INSERT INTO policy_activations(policy,activated_at,activated_by,identity,request_id,expected_id,reason) '
        'VALUES(?,?,?,?,?,?,?)',('v0',db.now(),'key:'+str(principal.key_id),manifest['identity'],'fixture',0,'structural activation'))
    return manifest


def view(book):return presentation.owner_view(book[1],now=NOW)


def test_no_activation_or_source_is_explicit_and_read_only(book):
    conn=book[1]
    writes=conn.total_changes
    result=view(book)
    assert result['status']=='inactive' and result['manifest'] is None and result['activation'] is None
    assert result['source']['status']=='missing'
    assert conn.total_changes==writes and not conn.in_transaction
    assert conn.execute('SELECT count(*) FROM api_keys').fetchone()[0]==1


def test_active_policy_reuses_registry_state_and_filters_manifest_payload(book):
    manifest=install(book)
    conn,owner=book[1:]
    conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',('mirror','a'*64,db.now(),'{"private_path":"DO_NOT_EXPOSE"}'))
    writes=conn.total_changes
    result=view(book)
    common=registry.current(conn,owner)
    assert result['status']=='active' and result['installed'] is True
    assert result['manifest']==common['manifest'] and result['activation']==common['activation']
    assert result['manifest']['identity']==manifest['identity']
    assert not {'files','guides'}&result['manifest'].keys()
    assert result['source']['status']=='mirror' and 'DO_NOT_EXPOSE' not in json.dumps(result)
    assert conn.total_changes==writes


def test_old_unregistered_pointer_does_not_infer_v0_parameters(book):
    conn=book[1]
    conn.execute('INSERT INTO policy_activations(policy,activated_at,activated_by,identity) VALUES(?,?,?,?)',
        ('v0',db.now(),'legacy-owner','v0:legacy'))
    result=view(book)
    assert result['status']=='unregistered' and result['manifest'] is None and result['installed'] is False


def test_invalid_manifest_is_hidden_without_echoing_stored_content(book):
    install(book)
    conn=book[1]
    conn.execute('DROP TRIGGER policy_versions_no_update')
    conn.execute("UPDATE policy_versions SET manifest='PRIVATE BROKEN STORAGE CONTENT'")
    result=view(book)
    assert result['status']=='invalid' and result['manifest'] is None
    assert 'PRIVATE BROKEN' not in json.dumps(result)


@pytest.mark.parametrize('field,value',[('policy','wrong-policy'),('activated_at','not-a-timestamp')])
def test_pointer_metadata_must_match_the_verified_policy(book,field,value):
    install(book)
    conn=book[1]
    conn.execute('DROP TRIGGER policy_activations_no_update')
    conn.execute(f'UPDATE policy_activations SET {field}=?',(value,))
    result=view(book)
    assert result['status']=='invalid' and result['manifest'] is None


def test_runtime_drift_keeps_saved_parameters_visible_but_not_active(book,monkeypatch):
    manifest=install(book)
    def changed(_):raise identity.IdentityError('private filesystem detail')
    monkeypatch.setattr(identity,'check_installed',changed)
    result=view(book)
    assert result['status']=='implementation_changed' and result['installed'] is False
    assert result['manifest']['parameters']==manifest['parameters']
    assert 'private filesystem' not in json.dumps(result)


@pytest.mark.parametrize('changes,status',[
    ({'effective_from':'2027-01-01T00:00:00+08:00'},'not_effective'),
    ({'confirmed_at':'2027-01-01T00:00:00+08:00'},'not_effective'),
    ({'effective_to':'2026-10-31T00:00:00+08:00'},'expired'),
    ({'effective_to':'2025-01-01T00:00:00+08:00'},'invalid'),
    ({'effective_from':'2026-01-01'},'invalid'),
])
def test_current_period_is_distinct_from_historical_activation(book,changes,status):
    install(book,**changes)
    assert view(book)['status']==status


@pytest.mark.parametrize('case',['bound','different-scope','missing-book','bad-binding'])
def test_ledger_binding_is_explicit_without_reading_holdings(book,case):
    install(book)
    conn=book[1]
    binding=dict(schema_version=1,account_id='structural',account_scope='structural-account',mirror_watermark={})
    if case=='different-scope':binding['account_scope']='other'
    if case=='bad-binding':binding['unknown']='do not silently ignore'
    if case!='missing-book':
        conn.execute('INSERT INTO account_books VALUES(?,?,?,?,?,?,?,?)',('structural',0,db.now(),db.now(),'evidence','0','{}','{}'))
    conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',('ledger','a'*64,db.now(),gate.canonical(binding)))
    queries=[]
    conn.set_trace_callback(queries.append)
    result=view(book)['source']
    conn.set_trace_callback(None)
    expected={'bound':'ledger','different-scope':'scope_mismatch','missing-book':'invalid','bad-binding':'invalid'}
    assert result['status']==expected[case]
    assert not any('FROM holdings' in sql or 'account_journal' in sql or 'account_observations' in sql for sql in queries)
    if case=='bound':assert result['account_id']=='structural' and result['scope_matches'] is True


def test_research_key_still_cannot_read_policy(book):
    install(book)
    conn=book[1]
    principal=api_keys.authenticate(conn,api_keys.create(conn,'structural-research',role='research'))
    with pytest.raises(registry.PolicyError) as error:registry.current(conn,principal)
    assert error.value.status==403


def test_presentation_and_active_owner_read_do_not_import_runtime_or_market(book):
    install(book)
    root=Path(__file__).resolve().parents[1]
    script='''import sqlite3,sys
from a_share_research.policy.presentation import owner_view
conn=sqlite3.connect(sys.argv[1],isolation_level=None)
owner_view(conn)
for name in ('pandas','numpy','tushare','akshare','a_share_research.policy.v0','a_share_research.policy.evaluation','a_share_research.core.providers'):
    assert name not in sys.modules,name
conn.close()
'''
    process=subprocess.run([sys.executable,'-c',script,str(book[0])],cwd=root,capture_output=True,text=True,timeout=20)
    assert process.returncode==0,process.stderr


def test_owner_view_works_on_read_only_connection(book):
    install(book)
    with sqlite3.connect(book[0].as_uri()+'?mode=ro',uri=True,isolation_level=None) as conn:
        assert presentation.owner_view(conn,now=NOW)['status']=='active'
