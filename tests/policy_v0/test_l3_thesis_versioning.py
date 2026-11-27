from __future__ import annotations

import json
import sqlite3

import pytest

from a_share_research.policy.v0 import commands_monitor, db
from policy_v0.helpers import seed_position


def _add_holding_with_l3(
    code: str,
    conditions: tuple[str, ...] = ("旧条件",),
    *,
    status: str = "pending",
    evidence: str | None = None,
    cadence: str = "unspecified",
) -> list[int]:
    """Seed a position with legacy (unversioned) L3 rows as the retired l3-add stored them."""
    holding_id = seed_position(code, 10, 800, day="2026-01-02")
    with db.db_session() as conn:
        ids = [
            conn.execute(
                """INSERT INTO holding_l3_conditions
                   (holding_id, condition_text, origin_type, status, evidence, as_of,
                    temporary_exit_rule, evidence_cadence, created_at, updated_at)
                   VALUES (?, ?, 'original', ?, ?, ?, '触发后清仓', ?, 'now', 'now')""",
                (holding_id, condition, status, evidence,
                 "2026-08-27" if evidence else None, cadence),
            ).lastrowid
            for condition in conditions
        ]
        conn.commit()
    return ids


def _add_versioned_holding(code: str) -> int:
    """Seed a position whose active thesis owns one classified L3 condition."""
    holding_id = seed_position(code, 10, 800, day="2026-01-02")
    with db.db_session() as conn:
        thesis_id = conn.execute(
            """INSERT INTO holding_thesis_versions
               (holding_id, version, l1, l2, rewrite_reason, status, created_at)
               VALUES (?, 1, '工业与创新商业化是主要利润引擎', '创新替代速度决定当前赔率',
                       '旧论文错误放大非核心分部', 'active', 'now')""",
            (holding_id,),
        ).lastrowid
        conn.execute(
            """INSERT INTO holding_l3_conditions
               (holding_id, thesis_version_id, condition_text, origin_type, status,
                temporary_exit_rule, condition_scope, action_level, materiality_basis,
                created_at, updated_at)
               VALUES (?, ?, '连续两个完整披露期核心利润恶化', 'original', 'pending',
                       '首次确认减持初始股数1/3，下一完整披露期复核', 'core_driver',
                       'reduce', '医药工业是主要利润和现金流来源', 'now', 'now')""",
            (holding_id, thesis_id),
        )
        conn.commit()
    return holding_id


def test_schema_migration_is_idempotent_for_empty_fixture() -> None:
    for _ in range(2):
        with db.db_session() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert conn.execute(
                "SELECT COUNT(*) FROM holding_thesis_versions"
            ).fetchone() == (0,)


def test_partial_unique_index_allows_only_one_active_thesis() -> None:
    seed_position("000963", 30, 800)
    with db.db_session() as conn:
        holding_id = conn.execute("SELECT id FROM holdings").fetchone()[0]
        values = (holding_id, 1, "L1", "L2", "reason", "active", "now")
        conn.execute(
            """INSERT INTO holding_thesis_versions
               (holding_id, version, l1, l2, rewrite_reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO holding_thesis_versions
                   (holding_id, version, l1, l2, rewrite_reason, status, created_at)
                   VALUES (?, 2, ?, ?, ?, 'active', ?)""",
                (holding_id, "L1b", "L2b", "reason", "now"),
            )


def test_l3_list_rejects_duplicate_open_holdings(capsys) -> None:
    _add_holding_with_l3("000963")
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO holdings (code, cost_price, shares, buy_date, initial_shares)
               VALUES ('000963', 11, 100, '2026-02-02', 100)"""
        )
        conn.commit()
    with pytest.raises(SystemExit):
        commands_monitor.cmd_l3_list(["000963"])
    assert "有2条在仓记录" in capsys.readouterr().err


def test_l3_list_freezes_incomplete_thesis_schema(capsys) -> None:
    _add_holding_with_l3("000963")
    with db.db_session() as conn:
        conn.execute("DROP TABLE holding_thesis_versions")
        conn.commit()
    commands_monitor.cmd_l3_list(["000963"])
    output = capsys.readouterr().out
    assert "论文版本迁移不完整" in output
    assert "冻结交易" in output


def test_l3_list_freezes_without_active_thesis_unique_index(capsys) -> None:
    _add_holding_with_l3("000963")
    with db.db_session() as conn:
        conn.execute("DROP INDEX idx_thesis_one_active_per_holding")
        conn.commit()
    commands_monitor.cmd_l3_list(["000963"])
    output = capsys.readouterr().out
    assert "论文版本迁移不完整" in output
    assert "冻结交易" in output


def test_l3_list_freezes_trigger_without_evidence(capsys, monkeypatch) -> None:
    _add_versioned_holding("000963")
    with db.db_session() as conn:
        conn.execute(
            """UPDATE holding_l3_conditions
               SET status='triggered', as_of=NULL, evidence=NULL WHERE is_active=1"""
        )
        conn.commit()
    commands_monitor.cmd_l3_list(["000963"])
    output = capsys.readouterr().out
    assert "交易契约无效" in output
    assert "连续两个完整披露期核心利润恶化" not in output


def test_legacy_holding_keeps_old_behavior_with_warning(capsys) -> None:
    _add_holding_with_l3("000963", status="triggered", evidence="证据")
    commands_monitor.cmd_l3_list(["000963"])
    output = capsys.readouterr().out
    assert "legacy contract" in output
    assert "triggered" in output
    assert "触发后清仓" in output


def test_rewritten_holding_filters_active_legacy_unclassified(capsys) -> None:
    seed_position("000963", 30, 800)
    with db.db_session() as conn:
        holding_id = conn.execute("SELECT id FROM holdings").fetchone()[0]
        thesis_id = conn.execute(
            """INSERT INTO holding_thesis_versions
               (holding_id, version, l1, l2, rewrite_reason, status, created_at)
               VALUES (?, 1, 'L1', 'L2', 'reason', 'active', 'now')""",
            (holding_id,),
        ).lastrowid
        conn.execute(
            """INSERT INTO holding_l3_conditions
               (holding_id, thesis_version_id, condition_text, status,
                temporary_exit_rule, created_at, updated_at)
               VALUES (?, ?, '非法旧条件', 'triggered', '清仓', 'now', 'now')""",
            (holding_id, thesis_id),
        )
        conn.commit()
    commands_monitor.cmd_l3_list(["000963"])
    output = capsys.readouterr().out
    assert "非法旧条件" not in output
    assert "交易契约无效" in output
    assert "冻结交易" in output


@pytest.mark.parametrize(
    "cadence", ["quarterly", "semiannual", "annual", "event", "unspecified"]
)
def test_l3_evidence_cadence_is_shown_in_both_views(cadence, capsys) -> None:
    _add_holding_with_l3("000963", cadence=cadence)
    commands_monitor.cmd_l3_list(["000963", "--json"])
    assert (
        json.loads(capsys.readouterr().out)["conditions"][0]["evidence_cadence"]
        == cadence
    )
    commands_monitor.cmd_l3_list(["000963"])
    assert f"evidence_cadence:{cadence}" in capsys.readouterr().out


def test_l3_cadence_database_constraint() -> None:
    condition_id = _add_holding_with_l3("000963")[0]
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT evidence_cadence FROM holding_l3_conditions"
        ).fetchone() == ("unspecified",)
        for invalid in (None, "monthly"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE holding_l3_conditions SET evidence_cadence=? WHERE id=?",
                    (invalid, condition_id),
                )


