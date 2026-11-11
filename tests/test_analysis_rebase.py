"""Structural mirror variants only; fixtures are not broker evidence or investment judgments."""
import json
import sqlite3

import pytest
from pydantic import ValidationError

from a_share_research import api_keys, gate
from a_share_research.account import analysis, analysis_projection, analysis_rebase, evidence, ledger
from test_account_analysis import branch as _branch_fixture, request, append, legacy_rows
from test_analysis_transitions import open_alert

branch = _branch_fixture


def seed(branch):
    conn, principal, *_ = branch
    return [analysis.initialize(conn, principal, hid) for hid in (1, 2)]


def choice(branch, hid=1, select='legacy', **overrides):
    preview = analysis_rebase.preview(branch[0], branch[1], hid)
    return dict(holding_id=hid, expected_version=preview['version'], legacy_hash=preview['legacy_hash'], select=select) | overrides


def rebase_request(branch, choices=None, **overrides):
    return analysis_rebase.Rebase.model_validate(dict(choices=choices or [choice(branch)],
        evidence_hashes=[branch[3]], reason='Structural mirror reconciliation') | overrides)


def rebase(branch, value):
    return analysis_rebase.rebase(branch[0], branch[1], value, branch[2])


def mirror_change(conn):
    conn.execute("UPDATE holding_l3_conditions SET evidence='Structural refreshed mirror',status='not_triggered' WHERE id=1")


def test_preview_shows_three_states_on_readonly_connection(branch):
    conn, principal, *_ = branch
    originals = seed(branch)
    shadow = append(branch, request(branch))
    mirror_change(conn)
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    ro = sqlite3.connect(f'file:{path}?mode=ro', uri=True, isolation_level=None)
    ro.row_factory = sqlite3.Row
    try:
        preview = analysis_rebase.preview(ro, principal, 1)
        assert preview['baseline'] == originals[0]['snapshot']
        assert preview['shadow'] == shadow['snapshot']
        assert preview['legacy'] == analysis._capture(conn, 1)
        assert preview['legacy_changed'] and preview['shadow_changed']
        assert preview['legacy_hash'] == gate.digest(preview['legacy'])
        assert not ro.in_transaction
    finally:
        ro.close()


@pytest.mark.parametrize('select', ['legacy', 'shadow'])
def test_explicit_choice_preserves_history_and_allows_updates_and_projection(branch, select):
    conn, principal, *_ = branch
    originals = seed(branch)
    shadow = append(branch, request(branch))
    mirror_change(conn)
    mirrored = analysis._capture(conn, 1)
    legacy = legacy_rows(conn)
    value = rebase_request(branch, [choice(branch, select=select)])
    result = rebase(branch, value)['revisions'][0]
    assert result['source'] == 'legacy_rebase' and result['version'] == 2
    assert result['snapshot'] == (mirrored if select == 'legacy' else shadow['snapshot'])
    assert analysis_rebase.baseline(conn, 1) == mirrored
    assert analysis.history(conn, principal, 1)['revisions'][:2] == [originals[0], shadow]
    assert legacy_rows(conn) == legacy
    assert analysis.inspect(conn) == []
    continued = append(branch, request(branch, version=2, reason='Structural post-rebase update'))
    assert continued['version'] == 3
    assert analysis_rebase.baseline(conn, 1) == mirrored
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    ro = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        analysis_projection.install(ro)
        assert ro.execute('SELECT status FROM holding_l3_conditions WHERE id=1').fetchone()[0] == 'watch'
        assert ro.execute('SELECT status FROM main.holding_l3_conditions WHERE id=1').fetchone()[0] == 'not_triggered'
    finally:
        ro.close()


def test_retry_returns_original_receipt_even_after_further_mirror_change(branch):
    conn, *_ = branch
    seed(branch)
    mirror_change(conn)
    value = rebase_request(branch)
    result = rebase(branch, value)
    conn.execute("UPDATE holding_l3_conditions SET evidence='Another structural mirror' WHERE id=1")
    assert rebase(branch, value) == result
    assert len(analysis.history(conn, branch[1], 1)['revisions']) == 2


@pytest.mark.parametrize('drift', ['version', 'mirror'])
def test_preview_drift_rejected_without_appending(branch, drift):
    conn, principal, *_ = branch
    seed(branch)
    value = rebase_request(branch)
    if drift == 'version':
        append(branch, request(branch))
    else:
        mirror_change(conn)
    before = analysis.history(conn, principal, 1)
    with pytest.raises(ledger.LedgerError, match='版本冲突|镜像已变化'):
        rebase(branch, value)
    assert analysis.history(conn, principal, 1) == before


def test_multi_holding_rebase_sql_failure_rolls_back_all_revisions(branch):
    conn, principal, *_ = branch
    originals = seed(branch)
    value = rebase_request(branch, [choice(branch, 2), choice(branch, 1)])
    conn.execute("CREATE TRIGGER fail_second_rebase BEFORE INSERT ON account_analysis_history WHEN NEW.holding_id=1 AND NEW.source='legacy_rebase' BEGIN SELECT RAISE(ABORT,'structural second failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='second failure'):
        rebase(branch, value)
    assert [analysis.current(conn, principal, hid) for hid in (1, 2)] == originals
    assert not conn.in_transaction


def test_cross_holding_collision_requires_joint_reconciliation(branch):
    conn, principal, *_ = branch
    seed(branch)
    shadow = append(branch, request(branch, [open_alert()]))
    generated = shadow['snapshot']['holding_alerts'][-1]['id']
    conn.execute("INSERT INTO holding_alerts(id,holding_id,code,level,category,reason_code,reason,status,opened_at,updated_at) VALUES (?,2,'600002','yellow','unverified','mirror-import','Structural mirror','active','2026-11-12','2026-11-12')", (generated,))
    with pytest.raises(ledger.LedgerError, match='同批协调'):
        rebase(branch, rebase_request(branch, [choice(branch, 2)]))
    result = rebase(branch, rebase_request(branch, [choice(branch, 2), choice(branch, 1)]))
    assert [r['holding_id'] for r in result['revisions']] == [1, 2]
    assert all(r['snapshot'] == analysis._capture(conn, r['holding_id']) for r in result['revisions'])
    assert analysis.inspect(conn) == []


@pytest.mark.parametrize('role', ['research', 'owner'])
def test_other_roles_cannot_preview_or_rebase(branch, role):
    conn, _, directory, _ = branch
    seed(branch)
    value = rebase_request(branch)
    denied = api_keys.authenticate(conn, api_keys.create(conn, role, role=role))
    for action in (lambda: analysis_rebase.preview(conn, denied, 1),
                   lambda: analysis_rebase.rebase(conn, denied, value, directory)):
        with pytest.raises(ledger.LedgerError) as caught:
            action()
        assert caught.value.status == 403


def test_missing_evidence_and_duplicate_holdings_rejected(branch):
    seed(branch)
    with pytest.raises(evidence.EvidenceError):
        rebase(branch, rebase_request(branch, evidence_hashes=['0'*64]))
    with pytest.raises(ValidationError, match='不能重复'):
        rebase_request(branch, [choice(branch), choice(branch)])


@pytest.mark.parametrize('change,match', [("UPDATE holdings SET code='600003' WHERE id=1", '公司身份'),
                                        ("UPDATE holdings SET exit_date='2026-11-12' WHERE id=1", '已关闭')])
def test_identity_or_closed_cycle_cannot_rebase(branch, change, match):
    conn, *_ = branch
    seed(branch)
    value = rebase_request(branch)
    conn.execute(change)
    with pytest.raises(ledger.LedgerError, match=match):
        rebase(branch, value)


def test_integrity_detects_tampered_rebase_even_if_envelope_hash_is_recomputed(branch):
    conn, *_ = branch
    seed(branch)
    result = rebase(branch, rebase_request(branch))['revisions'][0]
    assert analysis.inspect(conn) == []
    row = conn.execute('SELECT request FROM account_analysis_history WHERE id=?', (result['revision_id'],)).fetchone()
    envelope = json.loads(row[0])
    envelope['legacy_snapshot']['holding_l3_conditions'][0]['evidence'] = 'Structural corruption'
    conn.execute('DROP TRIGGER account_analysis_no_update')
    conn.execute('UPDATE account_analysis_history SET request=?,request_hash=? WHERE id=?',
                 (gate.canonical(envelope), gate.digest(envelope), result['revision_id']))
    assert any('基线协调' in problem for problem in analysis.inspect(conn))


def test_incomplete_multi_holding_receipt_is_detected(branch):
    conn, *_ = branch
    seed(branch)
    value = rebase_request(branch, [choice(branch, 1), choice(branch, 2)])
    result = rebase(branch, value)
    conn.execute('DROP TRIGGER account_analysis_no_delete')
    conn.execute('DELETE FROM account_analysis_history WHERE id=?', (result['revisions'][1]['revision_id'],))
    assert any('缺少持仓周期回执' in item for item in analysis.inspect(conn))
    with pytest.raises(ledger.LedgerError, match='回执不完整'):
        rebase(branch, value)


def test_corrupted_acknowledged_baseline_cannot_be_used_for_update(branch):
    conn, *_ = branch
    seed(branch)
    result = rebase(branch, rebase_request(branch))['revisions'][0]
    conn.execute('DROP TRIGGER account_analysis_no_update')
    row = conn.execute('SELECT request FROM account_analysis_history WHERE id=?', (result['revision_id'],)).fetchone()
    envelope = json.loads(row[0])
    envelope['legacy_snapshot']['holding_l3_conditions'][0]['evidence'] = 'Structural tampering'
    conn.execute('UPDATE account_analysis_history SET request=?,request_hash=? WHERE id=?',
                 (gate.canonical(envelope), gate.digest(envelope), result['revision_id']))
    with pytest.raises(ledger.LedgerError, match='基线记录损坏'):
        append(branch, request(branch, version=1))
