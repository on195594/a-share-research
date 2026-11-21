"""Structural state transitions only; these invented fixtures are not investment evidence."""
import json
import sqlite3

import pytest
from pydantic import ValidationError

from a_share_research.account import analysis, cycle_analysis, evidence, ledger
from support.account_analysis import append, legacy_rows, request, start_cycle


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
    base = start_cycle(branch)
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
    assert cycle_analysis.current(conn, principal, 'a', 'legacy:1') == result
    assert result['snapshot']['holding_l3_conditions'][0]['status'] == 'watch'
    assert result['snapshot']['holding_alerts'][0]['status'] == 'pending'
    assert result['snapshot']['holding_tier_state'][0]['tier2_status'] == 'completed'
    original = conn.execute('SELECT snapshot FROM account_analysis_history WHERE id=?',(base['revision_id'],)).fetchone()[0]
    assert json.loads(original) == base['snapshot']
    assert legacy_rows(conn) == before
    with pytest.raises(ledger.LedgerError,match='版本冲突'):
        append(branch,request(branch,reason='Different request at old version'))
    assert conn.execute("SELECT count(*) FROM account_analysis_history WHERE account_id='a'").fetchone()[0] == 2


@pytest.mark.parametrize('change',[
    dict(kind='l3',id=2,status='watch',as_of='2026-11-11',evidence='Structural retired condition'),
    dict(kind='l3',id=3,status='watch',as_of='2026-11-11',evidence='Structural other holding'),
    dict(kind='alert',id=3,status='pending',evidence='Structural resolved alert'),
    dict(kind='alert',id=2,status='pending',evidence='Structural already pending'),
])
def test_invalid_second_change_rolls_back_entire_batch(branch,change):
    conn, principal, *_ = branch
    base = start_cycle(branch)
    with pytest.raises(ledger.LedgerError):
        append(branch,request(branch,[dict(kind='tier',tier='tier2',status='completed'),change]))
    assert cycle_analysis.current(conn, principal, 'a', 'legacy:1') == base
    assert conn.execute("SELECT count(*) FROM account_analysis_history WHERE account_id='a'").fetchone()[0] == 1


def test_alert_resolution_is_terminal_and_preserves_prior_evidence(branch):
    conn, principal, *_ = branch
    start_cycle(branch)
    result = append(branch,request(branch,[dict(kind='alert',id=2,status='resolved',evidence='Structural resolution')]))
    row = result['snapshot']['holding_alerts'][1]
    assert row['status'] == 'resolved' and row['resolution_evidence'] == 'Structural resolution'
    assert row['resolved_at'] and row['evidence'] is None
    with pytest.raises(ledger.LedgerError,match='预警当前状态'):
        append(branch,request(branch,[dict(kind='alert',id=2,status='resolved',evidence='Different resolution')],version=1))


def test_entry_exemption_cannot_be_changed_by_status_update(branch):
    conn, principal, *_ = branch
    conn.execute("UPDATE holding_tier_state SET tier1_status='exempted' WHERE holding_id=1")
    base = start_cycle(branch)
    with pytest.raises(ledger.LedgerError,match='豁免'):
        append(branch,request(branch,[dict(kind='tier',tier='tier1',status='pending')]))
    assert cycle_analysis.current(conn, principal, 'a', 'legacy:1') == base
    with pytest.raises(ValidationError):
        request(branch,[dict(kind='tier',tier='tier1',status='exempted')])


def test_evidence_failure_and_sql_failure_do_not_append_partial_revision(branch):
    conn, principal, directory, _ = branch
    base = start_cycle(branch)
    with pytest.raises(evidence.EvidenceError):
        append(branch,request(branch,evidence_hashes=['0'*64]))
    conn.execute("CREATE TRIGGER fail_analysis BEFORE INSERT ON account_analysis_history BEGIN SELECT RAISE(ABORT,'structural failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='structural failure'):
        append(branch,request(branch))
    assert cycle_analysis.current(conn, principal, 'a', 'legacy:1') == base
    assert not conn.in_transaction


@pytest.mark.parametrize('column,value,match', [
    ('parent_id', None, '前序关系'),
    ('snapshot_hash', 'bad', '哈希'),
    ('evidence_hashes', '[]', '证据'),
    ('source', 'unknown', '未知来源'),
])
def test_integrity_detects_corrupt_history(branch, column, value, match):
    conn, principal, *_ = branch
    start_cycle(branch)
    result = append(branch, request(branch))
    assert analysis.inspect(conn) == []
    conn.execute('DROP TRIGGER account_analysis_no_update')
    conn.execute(f'UPDATE account_analysis_history SET {column}=? WHERE id=?', (value, result['revision_id']))
    assert any(match in item for item in analysis.inspect(conn))
