"""Structural projection fixtures only; these are not broker or investment evidence."""
import json
import sqlite3

import pytest

from a_share_research.account import analysis
from a_share_research.policy.v0 import commands_monitor, db as policy_db
from test_account_analysis import branch as branch_fixture, request


@pytest.fixture
def branch(tmp_path):
    yield from branch_fixture.__wrapped__(tmp_path)


def initialize(branch):
    conn, principal, *_ = branch
    for (identity,) in conn.execute('SELECT id FROM holdings').fetchall():
        analysis.initialize(conn, principal, identity)


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv('POLICY_V0_DB', str(tmp_path / 'app.db'))
    monkeypatch.setenv('POLICY_V0_ANALYSIS_SHADOW', '1')


def rows(conn, prefix=''):
    return {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {prefix}{table} ORDER BY 1')]
            for table in analysis.TABLES}


def test_baseline_projection_preserves_columns_rows_indexes_and_monitor_input(branch, tmp_path, monkeypatch):
    conn, *_ = branch
    initialize(branch)
    monkeypatch.setenv('POLICY_V0_DB', str(tmp_path / 'app.db'))
    normal = commands_monitor._load_monitor_local_snapshot()
    configure(monkeypatch, tmp_path)
    with policy_db.read_only_db_session() as projected:
        assert rows(projected) == rows(conn)
        for table in analysis.TABLES:
            assert [tuple(r) for r in projected.execute(f'PRAGMA table_xinfo({table})')] == [
                tuple(r) for r in conn.execute(f'PRAGMA table_xinfo({table})')]
        indexes = list(projected.execute('PRAGMA index_list(holding_thesis_versions)'))
        assert any(r[1] == 'idx_thesis_one_active_per_holding' and r[2] and r[4] for r in indexes)
        assert projected.in_transaction
    assert commands_monitor._load_monitor_local_snapshot() == normal


def test_latest_shadow_visible_with_legacy_unchanged_and_all_connections_readonly(branch, tmp_path, monkeypatch):
    conn, principal, directory, *_ = branch
    initialize(branch)
    before = rows(conn)
    analysis.update(conn, principal, 1, request(branch), directory)
    configure(monkeypatch, tmp_path)
    assert policy_db.is_read_only()
    with policy_db.db_session() as projected:
        assert projected.execute('SELECT status FROM holding_l3_conditions WHERE id=1').fetchone()[0] == 'watch'
        assert projected.execute('SELECT status FROM main.holding_l3_conditions WHERE id=1').fetchone()[0] == 'pending'
        for statement in ('DELETE FROM holdings', 'DELETE FROM main.holding_l3_conditions',
                          'DELETE FROM temp.holding_l3_conditions', 'CREATE TEMP TABLE bypass(id)'):
            with pytest.raises(sqlite3.OperationalError):
                projected.execute(statement)
    assert rows(conn) == before
    monkeypatch.delenv('POLICY_V0_ANALYSIS_SHADOW')
    with policy_db.read_only_db_session() as normal:
        assert normal.execute('SELECT status FROM holding_l3_conditions WHERE id=1').fetchone()[0] == 'pending'
        assert normal.execute('SELECT count(*) FROM sqlite_temp_master').fetchone()[0] == 0


@pytest.mark.parametrize('change,match', [
    ('missing', '缺少'), ('drift', '基线已变化'), ('company', '公司身份'), ('hash', '哈希'),
])
def test_invalid_shadow_refuses_to_return_connection(branch, tmp_path, monkeypatch, change, match):
    conn, principal, *_ = branch
    if change == 'missing':
        analysis.initialize(conn, principal, 1)
    else:
        initialize(branch)
        if change == 'drift':
            conn.execute("UPDATE holding_l3_conditions SET evidence='Changed structural mirror' WHERE id=1")
        elif change == 'company':
            conn.execute("UPDATE holdings SET code='600003' WHERE id=1")
        else:
            conn.execute('DROP TRIGGER account_analysis_no_update')
            conn.execute("UPDATE account_analysis_history SET snapshot_hash='bad' WHERE holding_id=1")
    configure(monkeypatch, tmp_path)
    with pytest.raises(sqlite3.OperationalError, match=match):
        policy_db.get_db()


def test_closed_holdings_keep_legacy_even_if_shadow_history_exists(branch, tmp_path, monkeypatch):
    conn, principal, directory, *_ = branch
    initialize(branch)
    analysis.update(conn, principal, 1, request(branch), directory)
    conn.execute("UPDATE holdings SET exit_date='2026-11-11' WHERE id=1")
    configure(monkeypatch, tmp_path)
    with policy_db.read_only_db_session() as projected:
        assert rows(projected) == rows(conn)


def test_l3_cli_contract_inspection_does_not_treat_projection_as_partial_migration(branch, tmp_path, monkeypatch, capsys):
    initialize(branch)
    monkeypatch.setenv('POLICY_V0_DB', str(tmp_path / 'app.db'))
    commands_monitor.cmd_l3_list(['600001', '--json'])
    original = json.loads(capsys.readouterr().out)
    configure(monkeypatch, tmp_path)
    commands_monitor.cmd_l3_list(['600001', '--json'])
    assert json.loads(capsys.readouterr().out) == original
