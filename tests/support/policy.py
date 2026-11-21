"""Structural policy accounts, monitor inputs and replay helpers."""

from datetime import datetime

from a_share_research import db, gate
from a_share_research.account import analysis, cycle_analysis, ledger
from a_share_research.policy import evaluation, monitor, registry
from a_share_research.policy.v0 import account_store
from support.positions import policy_account_book as policy_book


def historical_replay(setup, request_id):
    """Seed a v19 receipt to exercise readers after the replay writer was retired."""
    _, conn, principals, manifest, activation, bundle, _ = setup
    result = evaluation.evaluate(bundle, manifest['parameters'])
    row = conn.execute(
        'INSERT INTO policy_evaluations '
        '(identity,activation_id,created_at,created_by,request_id,input_hash,result_hash,input,result) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (manifest['identity'], activation['id'], db.now(), principals['assistant'].key_id, request_id,
         gate.digest(bundle), gate.digest(result), gate.canonical(bundle), gate.canonical(result)),
    )
    return registry.evaluation(conn, principals['assistant'], row.lastrowid)


def run_monitor(configured,request_id='ledger-capture'):
    book,_,active,_=configured
    path,conn,principal,_,_=book
    return monitor.run(conn,principal,path,request_id,active['id'])


NOW = datetime.fromisoformat('2026-11-11T12:00:00+08:00')


def scoped_parameters(**changes):
    return dict(policy_id='structural-policy', source='policy_file', account_scope='structural-scope', currency='CNY',
                effective_from='2026-01-01T00:00:00+08:00', effective_to=None,
                confirmed_at='2026-01-01T00:00:00+08:00', confirmation_ref='structural-only',
                max_position_risk_pct=2., max_portfolio_risk_pct=8.,
                field_sources={'max_position_risk_pct':'policy_file','max_portfolio_risk_pct':'policy_file'}) | changes


def policy_input_book(tmp_path):
    for conn,principal,old_directory,sha in policy_book(tmp_path):
        directory=tmp_path/'account-evidence'
        old_directory.rename(directory)
        account_store.initialize(conn,principal,'a',directory,[sha])
        baseline=analysis.initialize(conn,principal,1)
        cycle_analysis.initialize(conn,principal,'a','legacy:1',cycle_analysis.Initialize(
            expected_account_version=0,evidence_hashes=[sha],legacy_revision_id=baseline['revision_id']),directory)
        binding=dict(schema_version=1,account_id='a',account_scope='structural-scope',mirror_watermark={})
        conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',('ledger','a'*64,db.now(),gate.canonical(binding)))
        yield tmp_path/'app.db',conn,principal,directory,sha


def observe(book,total='11200'):
    _,conn,principal,directory,sha=book
    return ledger.observe(conn,principal,'a',ledger.Observation(observed_at='2026-11-01T15:00:00+08:00',total_assets=total),directory,sha)
