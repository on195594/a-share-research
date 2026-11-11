"""Structural state transitions only; these invented fixtures are not investment evidence."""
import json
import sqlite3

import pytest
from pydantic import ValidationError

from a_share_research import api_keys, db
from a_share_research.account import analysis, evidence, ledger


@pytest.fixture
def branch(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    analysis.migrate(conn)
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'structural', role='assistant'))
    directory = tmp_path / 'evidence'
    sha = evidence.store(directory, b'Structural analysis evidence; no broker or investment assertion')
    conn.execute("INSERT INTO holdings(id,code,name) VALUES (1,'600001','Structural fixture')")
    conn.execute("INSERT INTO holdings(id,code,name) VALUES (2,'600002','Other structural fixture')")
    conn.execute("INSERT INTO holding_thesis_versions(id,holding_id,version,l1,l2,rewrite_reason,status,created_at)"
                 " VALUES (1,1,1,'Structural L1','Structural L2','fixture','active','2026-11-01')")
    for identity, holding, active in ((1,1,1),(2,1,0),(3,2,1)):
        conn.execute("INSERT INTO holding_l3_conditions(id,holding_id,condition_text,created_at,updated_at,is_active)"
                     " VALUES (?,?, 'Structural condition','2026-11-01','2026-11-01',?)", (identity,holding,active))
    for identity, status in ((1,'active'),(2,'pending'),(3,'resolved')):
        conn.execute("INSERT INTO holding_alerts(id,holding_id,code,level,category,reason_code,reason,status,opened_at,updated_at)"
                     " VALUES (?,1,'600001','yellow','unverified',?,'Structural alert',?,'2026-11-01','2026-11-01')",
                     (identity,f'structural-{identity}',status))
    conn.execute("INSERT INTO holding_tier_state(holding_id,updated_at) VALUES (1,'2026-11-01')")
    yield conn, principal, directory, sha
    conn.close()


def legacy_rows(conn):
    return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY 1')]
            for t in (*analysis.TABLES,'holdings','holding_events')}


def request(branch, changes=None, version=0, **extra):
    return analysis.Update.model_validate(dict(expected_version=version, evidence_hashes=[branch[3]],
        reason='Structural transition', changes=changes or [dict(kind='l3',id=1,status='watch',
            as_of='2026-11-11',evidence='Structural evidence')]) | extra)


def append(branch, value):
    return analysis.update(branch[0],branch[1],1,value,branch[2])


def test_baseline_retains_missing_legacy_fields_and_is_idempotent(branch):
    conn, principal, *_ = branch
    before = legacy_rows(conn)
    result = analysis.initialize(conn,principal,1)
    assert result['source'] == 'legacy_baseline' and result['version'] == 0
    assert result['parent_id'] is None and result['evidence_hashes'] == []
    l3 = result['snapshot']['holding_l3_conditions'][0]
    assert l3['evidence'] is None and l3['thesis_version_id'] is None
    assert l3['condition_scope'] == 'legacy_unclassified'
    assert analysis.initialize(conn,principal,1) == result
    assert legacy_rows(conn) == before


def test_append_is_atomic_idempotent_and_keeps_original_and_legacy(branch):
    conn, principal, *_ = branch
    base = analysis.initialize(conn,principal,1)
    before = legacy_rows(conn)
    value = request(branch,[dict(kind='l3',id=1,status='watch',as_of='2026-11-11',
        evidence='Structural evidence',evidence_cadence='quarterly'),
        dict(kind='alert',id=1,status='pending',evidence='Structural pending explanation'),
        dict(kind='tier',tier='tier2',status='completed')])
    result = append(branch,value)
    assert result['version'] == 1 and result['parent_id'] == base['revision_id']
    assert result['source'] == 'assistant' and result['actor_key_id'] == principal.key_id
    assert result['evidence_hashes'] == [branch[3]]
    assert append(branch,value) == result
    assert analysis.current(conn,principal,1) == result
    assert result['snapshot']['holding_l3_conditions'][0]['status'] == 'watch'
    assert result['snapshot']['holding_alerts'][0]['status'] == 'pending'
    assert result['snapshot']['holding_tier_state'][0]['tier2_status'] == 'completed'
    original = conn.execute('SELECT snapshot FROM account_analysis_history WHERE id=?',(base['revision_id'],)).fetchone()[0]
    assert json.loads(original) == base['snapshot']
    assert legacy_rows(conn) == before
    with pytest.raises(ledger.LedgerError,match='版本冲突'):
        append(branch,request(branch,reason='Different request at old version'))
    assert conn.execute('SELECT count(*) FROM account_analysis_history').fetchone()[0] == 2


@pytest.mark.parametrize('change',[
    dict(kind='l3',id=2,status='watch',as_of='2026-11-11',evidence='Structural retired condition'),
    dict(kind='l3',id=3,status='watch',as_of='2026-11-11',evidence='Structural other holding'),
    dict(kind='alert',id=3,status='pending',evidence='Structural resolved alert'),
    dict(kind='alert',id=2,status='pending',evidence='Structural already pending'),
])
def test_invalid_second_change_rolls_back_entire_batch(branch,change):
    conn, principal, *_ = branch
    base = analysis.initialize(conn,principal,1)
    with pytest.raises(ledger.LedgerError):
        append(branch,request(branch,[dict(kind='tier',tier='tier2',status='completed'),change]))
    assert analysis.current(conn,principal,1) == base
    assert conn.execute('SELECT count(*) FROM account_analysis_history').fetchone()[0] == 1


def test_alert_resolution_is_terminal_and_preserves_prior_evidence(branch):
    conn, principal, *_ = branch
    analysis.initialize(conn,principal,1)
    result = append(branch,request(branch,[dict(kind='alert',id=2,status='resolved',evidence='Structural resolution')]))
    row = result['snapshot']['holding_alerts'][1]
    assert row['status'] == 'resolved' and row['resolution_evidence'] == 'Structural resolution'
    assert row['resolved_at'] and row['evidence'] is None
    with pytest.raises(ledger.LedgerError,match='预警当前状态'):
        append(branch,request(branch,[dict(kind='alert',id=2,status='resolved',evidence='Different resolution')],version=1))


def test_entry_exemption_cannot_be_changed_by_status_update(branch):
    conn, principal, *_ = branch
    conn.execute("UPDATE holding_tier_state SET tier1_status='exempted' WHERE holding_id=1")
    base = analysis.initialize(conn,principal,1)
    with pytest.raises(ledger.LedgerError,match='豁免'):
        append(branch,request(branch,[dict(kind='tier',tier='tier1',status='pending')]))
    assert analysis.current(conn,principal,1) == base
    with pytest.raises(ValidationError):
        request(branch,[dict(kind='tier',tier='tier1',status='exempted')])


def test_mirror_baseline_changes_are_not_silently_merged(branch):
    conn, principal, *_ = branch
    base = analysis.initialize(conn,principal,1)
    conn.execute("UPDATE holding_l3_conditions SET evidence='Later mirrored evidence' WHERE id=1")
    with pytest.raises(ledger.LedgerError,match='基线已变化'):
        append(branch,request(branch))
    assert analysis.current(conn,principal,1) == base
    assert conn.execute('SELECT evidence FROM holding_l3_conditions WHERE id=1').fetchone()[0] == 'Later mirrored evidence'


def test_evidence_failure_and_sql_failure_do_not_append_partial_revision(branch):
    conn, principal, directory, _ = branch
    base = analysis.initialize(conn,principal,1)
    with pytest.raises(evidence.EvidenceError):
        append(branch,request(branch,evidence_hashes=['0'*64]))
    conn.execute("CREATE TRIGGER fail_analysis BEFORE INSERT ON account_analysis_history BEGIN SELECT RAISE(ABORT,'structural failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='structural failure'):
        append(branch,request(branch))
    assert analysis.current(conn,principal,1) == base
    assert not conn.in_transaction


@pytest.mark.parametrize('role',['research','owner'])
def test_other_roles_cannot_read_or_write(branch,role):
    conn, principal, directory, _ = branch
    analysis.initialize(conn,principal,1)
    denied = api_keys.authenticate(conn,api_keys.create(conn,role,role=role))
    for action in (lambda: analysis.initialize(conn,denied,1),lambda: analysis.current(conn,denied,1),
                   lambda: analysis.history(conn,denied,1),lambda: analysis.update(conn,denied,1,request(branch),directory)):
        with pytest.raises(ledger.LedgerError) as caught:
            action()
        assert caught.value.status == 403


def test_history_pagination_and_append_only_triggers(branch):
    conn, principal, *_ = branch
    base = analysis.initialize(conn,principal,1)
    result = append(branch,request(branch))
    first = analysis.history(conn,principal,1,limit=1)
    assert first['revisions'] == [base]
    second = analysis.history(conn,principal,1,after_id=first['next_after_id'],limit=1)
    assert second['revisions'] == [result]
    assert analysis.history(conn,principal,1,after_id=second['next_after_id'])['revisions'] == []
    assert analysis.history(conn,principal,2)['revisions'] == []
    for args in ({'after_id':-1},{'limit':0},{'limit':1001},{'after_id':True}):
        with pytest.raises(ledger.LedgerError):
            analysis.history(conn,principal,1,**args)
    for sql in ('UPDATE account_analysis_history SET source=source','DELETE FROM account_analysis_history'):
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):
            conn.execute(sql)


def test_missing_or_closed_holding_and_duplicate_targets_are_rejected(branch):
    conn, principal, *_ = branch
    with pytest.raises(ledger.LedgerError) as caught:
        analysis.current(conn,principal,1)
    assert caught.value.status == 404
    with pytest.raises(ledger.LedgerError):
        analysis.initialize(conn,principal,999)
    analysis.initialize(conn,principal,1)
    conn.execute("UPDATE holdings SET exit_date='2026-11-10' WHERE id=1")
    with pytest.raises(ledger.LedgerError,match='已关闭'):
        append(branch,request(branch))
    with pytest.raises(ValidationError,match='重复更新'):
        request(branch,[dict(kind='tier',tier='tier1',status='pending')]*2)


@pytest.mark.parametrize('column,value,match', [
    ('parent_id', None, '前序关系'),
    ('snapshot_hash', 'bad', '哈希'),
    ('evidence_hashes', '[]', '证据'),
    ('source', 'unknown', '未知来源'),
])
def test_integrity_detects_corrupt_history(branch, column, value, match):
    conn, principal, *_ = branch
    analysis.initialize(conn, principal, 1)
    result = append(branch, request(branch))
    assert analysis.inspect(conn) == []
    conn.execute('DROP TRIGGER account_analysis_no_update')
    conn.execute(f'UPDATE account_analysis_history SET {column}=? WHERE id=?', (value, result['revision_id']))
    assert any(match in item for item in analysis.inspect(conn))


def test_changed_company_identity_cannot_reuse_history(branch):
    conn, principal, *_ = branch
    analysis.initialize(conn, principal, 1)
    conn.execute("UPDATE holdings SET code='600003' WHERE id=1")
    with pytest.raises(ledger.LedgerError, match='公司身份'):
        append(branch, request(branch))
