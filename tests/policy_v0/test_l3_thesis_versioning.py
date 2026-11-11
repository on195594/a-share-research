from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO

import pytest

from a_share_research.policy.v0 import (
    cache,
    commands_admin,
    commands_holdings,
    commands_monitor,
    db,
)


def _add_holding_with_l3(
    code: str, conditions: tuple[str, ...] = ("旧条件",)
) -> list[int]:
    commands_holdings.cmd_add_holding([code, "10", "800", "--date", "2026-01-02"])
    for condition in conditions:
        commands_monitor.cmd_l3_add([code, "original", condition, "触发后清仓"])
    with db.db_session() as conn:
        return [
            row[0]
            for row in conn.execute(
                """SELECT l.id FROM holding_l3_conditions l
                   JOIN holdings h ON h.id=l.holding_id
                   WHERE h.code=? ORDER BY l.id""",
                (code,),
            )
        ]


def _payload(
    retire_l3_ids: list[int], *, scope: str = "core_driver", action: str = "reduce"
) -> dict:
    return {
        "l1": "工业与创新商业化是主要利润引擎",
        "l2": "创新替代速度决定当前赔率",
        "rewrite_reason": "旧论文错误放大非核心分部",
        "retire_l3_ids": retire_l3_ids,
        "new_l3": [
            {
                "condition": "连续两个完整披露期核心利润恶化",
                "scope": scope,
                "action": action,
                "materiality_basis": "医药工业是主要利润和现金流来源；需连续两个完整披露期确认",
                "temporary_exit_rule": "首次确认减持初始股数1/3，下一完整披露期复核",
            }
        ],
    }


def _rewrite(monkeypatch: pytest.MonkeyPatch, code: str, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload, ensure_ascii=False)))
    commands_monitor.cmd_thesis_rewrite([code])


def test_schema_migration_is_idempotent_for_empty_fixture(monkeypatch) -> None:
    for _ in range(2):
        monkeypatch.setattr(db, "_SCHEMA_INITIALIZED", False)
        with db.db_session() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert conn.execute(
                "SELECT COUNT(*) FROM holding_thesis_versions"
            ).fetchone() == (0,)


def test_partial_unique_index_allows_only_one_active_thesis() -> None:
    commands_holdings.cmd_add_holding(["000963", "30", "800"])
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


def test_thesis_rewrite_retires_old_l3_without_touching_position_ledger(
    monkeypatch, capsys
) -> None:
    old_ids = _add_holding_with_l3("000963", ("旧医美条件", "旧现金流条件"))
    with db.db_session() as conn:
        holding_before = conn.execute(
            """SELECT shares, cost_price, buy_date, reference_cost
               FROM holdings WHERE code='000963'"""
        ).fetchone()
        events_before = conn.execute(
            """SELECT holding_id, code, event_type, event_date, shares, price,
                      fees, tax, cash_amount, realized_pnl, notes, inferred, created_at
               FROM holding_events ORDER BY id"""
        ).fetchall()

    payload = _payload(old_ids)
    payload["new_l3"] *= 3
    payload["new_l3"][1] = {
        **payload["new_l3"][1],
        "condition": "集团现金盈利恶化",
        "scope": "aggregate",
    }
    payload["new_l3"][2] = {
        **payload["new_l3"][2],
        "condition": "重大治理破坏",
        "scope": "governance",
        "action": "exit",
    }
    _rewrite(monkeypatch, "000963", payload)

    with db.db_session() as conn:
        assert conn.execute(
            "SELECT version, status FROM holding_thesis_versions"
        ).fetchall() == [(1, "active")]
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_l3_conditions WHERE is_active=1"
        ).fetchone() == (3,)
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_l3_conditions WHERE is_active=0"
        ).fetchone() == (2,)
        assert (
            conn.execute(
                "SELECT shares, cost_price, buy_date, reference_cost FROM holdings WHERE code='000963'"
            ).fetchone()
            == holding_before
        )
        assert (
            conn.execute(
                """SELECT holding_id, code, event_type, event_date, shares, price,
                      fees, tax, cash_amount, realized_pnl, notes, inferred, created_at
               FROM holding_events ORDER BY id"""
            ).fetchall()
            == events_before
        )

    capsys.readouterr()
    commands_monitor.cmd_l3_list(["000963"])
    active_output = capsys.readouterr().out
    assert "旧医美条件" not in active_output
    assert "thesis:v1 active" in active_output
    assert "L1:工业与创新商业化是主要利润引擎" in active_output
    assert "L2:创新替代速度决定当前赔率" in active_output
    commands_monitor.cmd_l3_list(["000963", "--all"])
    all_output = capsys.readouterr().out
    assert "旧医美条件" in all_output
    assert "retired" in all_output
    assert "thesis:v1 active" in all_output
    assert "L1:工业与创新商业化是主要利润引擎" in all_output


def test_thesis_rewrite_cli_gate_precedes_database_and_confirmed_dispatch(
    isolated_cache_database, monkeypatch
) -> None:
    payload = _payload([])
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    assert cache.main(["thesis-rewrite", "000963"]) == 3
    assert not isolated_cache_database.exists()

    commands_holdings.cmd_add_holding(["000963", "30", "800"])
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    assert cache.main(["--confirm-write", "thesis-rewrite", "000963"]) == 0
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT version, status FROM holding_thesis_versions"
        ).fetchone() == (1, "active")


@pytest.mark.parametrize("bad_payload", ["not-json", "[]", "{}"])
def test_thesis_rewrite_rejects_invalid_stdin_without_mutation(
    monkeypatch, bad_payload
) -> None:
    _add_holding_with_l3("000963")
    monkeypatch.setattr(sys, "stdin", StringIO(bad_payload))
    with pytest.raises(SystemExit):
        commands_monitor.cmd_thesis_rewrite(["000963"])
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_thesis_versions"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT is_active FROM holding_l3_conditions"
        ).fetchone() == (1,)


def test_thesis_rewrite_rejects_cross_holding_and_incomplete_retirement(
    monkeypatch,
) -> None:
    first = _add_holding_with_l3("000963", ("旧条件1", "旧条件2"))
    other = _add_holding_with_l3("600036")

    for ids in ([first[0], other[0]], [first[0]]):
        with pytest.raises(SystemExit):
            _rewrite(monkeypatch, "000963", _payload(ids))
        with db.db_session() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM holding_thesis_versions"
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT COUNT(*) FROM holding_l3_conditions WHERE is_active=1"
            ).fetchone() == (3,)


@pytest.mark.parametrize("action", ["reduce", "exit"])
def test_non_core_cannot_authorize_reduce_or_exit(monkeypatch, action) -> None:
    old_ids = _add_holding_with_l3("000963")
    with pytest.raises(SystemExit):
        _rewrite(
            monkeypatch, "000963", _payload(old_ids, scope="non_core", action=action)
        )
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_thesis_versions"
        ).fetchone() == (0,)


def test_any_write_failure_rolls_back_entire_rewrite(monkeypatch) -> None:
    old_ids = _add_holding_with_l3("000963")
    with db.db_session() as conn:
        conn.execute(
            """CREATE TRIGGER fail_new_l3 BEFORE INSERT ON holding_l3_conditions
               WHEN NEW.thesis_version_id IS NOT NULL
               BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END"""
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        _rewrite(monkeypatch, "000963", _payload(old_ids))
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_thesis_versions"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT is_active, retired_at FROM holding_l3_conditions"
        ).fetchone() == (1, None)


def test_l3_update_rejects_retired_row(monkeypatch) -> None:
    old_ids = _add_holding_with_l3("000963")
    _rewrite(monkeypatch, "000963", _payload(old_ids))
    with pytest.raises(SystemExit):
        commands_monitor.cmd_l3_update(
            [str(old_ids[0]), "triggered", "2026-08-27", "旧证据"]
        )


@pytest.mark.parametrize(
    "as_of,evidence",
    [("", "有效证据"), ("2026-08-27", "")],
)
def test_l3_update_rejects_empty_evidence_contract(as_of, evidence) -> None:
    condition_id = _add_holding_with_l3("000963")[0]
    with pytest.raises(SystemExit):
        commands_monitor.cmd_l3_update(
            [str(condition_id), "triggered", as_of, evidence]
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
    old_ids = _add_holding_with_l3("000963")
    _rewrite(monkeypatch, "000963", _payload(old_ids))
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


def test_second_rewrite_supersedes_prior_version_and_l3_add_is_blocked(
    monkeypatch,
) -> None:
    old_ids = _add_holding_with_l3("000963")
    _rewrite(monkeypatch, "000963", _payload(old_ids))
    with db.db_session() as conn:
        replacement_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM holding_l3_conditions WHERE is_active=1"
            )
        ]
    with pytest.raises(SystemExit):
        commands_monitor.cmd_l3_add(["000963", "new_monitoring", "旁路条件"])

    _rewrite(monkeypatch, "000963", _payload(replacement_ids))
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT version, status FROM holding_thesis_versions ORDER BY version"
        ).fetchall() == [(1, "superseded"), (2, "active")]
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_thesis_versions WHERE status='active'"
        ).fetchone() == (1,)


def test_legacy_holding_keeps_old_behavior_with_warning(capsys) -> None:
    old_ids = _add_holding_with_l3("000963")
    commands_monitor.cmd_l3_update([str(old_ids[0]), "triggered", "2026-08-27", "证据"])
    commands_monitor.cmd_l3_list(["000963"])
    output = capsys.readouterr().out
    assert "legacy contract" in output
    assert "triggered" in output
    assert "触发后清仓" in output


def test_rewritten_holding_filters_active_legacy_unclassified(capsys) -> None:
    commands_holdings.cmd_add_holding(["000963", "30", "800"])
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


def test_remove_holding_and_cleanup_cover_thesis_versions(capsys) -> None:
    _add_holding_with_l3("000963")
    with db.db_session() as conn:
        holding_id = conn.execute("SELECT id FROM holdings").fetchone()[0]
        conn.execute(
            """INSERT INTO holding_thesis_versions
               (holding_id, version, l1, l2, rewrite_reason, status, created_at)
               VALUES (?, 1, 'L1', 'L2', 'reason', 'active', 'now')""",
            (holding_id,),
        )
        conn.commit()
    commands_holdings.cmd_remove_holding(["000963"])
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_thesis_versions"
        ).fetchone() == (0,)

    orphan_ids = _add_holding_with_l3("600036")
    with db.db_session() as conn:
        holding_id = conn.execute(
            "SELECT id FROM holdings WHERE code='600036'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO holding_thesis_versions
               (holding_id, version, l1, l2, rewrite_reason, status, created_at)
               VALUES (?, 1, 'L1', 'L2', 'reason', 'active', 'now')""",
            (holding_id,),
        )
        conn.execute("DELETE FROM holding_l3_conditions WHERE id=?", (orphan_ids[0],))
        conn.execute("DELETE FROM holding_events WHERE holding_id=?", (holding_id,))
        conn.execute("DELETE FROM holdings WHERE id=?", (holding_id,))
        conn.commit()
    commands_admin.cmd_cleanup([])
    assert "孤儿持仓关联记录" in capsys.readouterr().out
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM holding_thesis_versions"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "cadence", ["quarterly", "semiannual", "annual", "event", "unspecified"]
)
def test_l3_evidence_cadence_round_trip_and_update_preserves_it(
    cadence, capsys
) -> None:
    commands_holdings.cmd_add_holding(["000963", "30", "800"])
    assert (
        cache.main(
            [
                "--confirm-write",
                "l3-add",
                "000963",
                "original",
                "条件",
                "临时规则",
                "--evidence-cadence",
                cadence,
            ]
        )
        == 0
    )
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT evidence_cadence FROM holding_l3_conditions"
        ).fetchone() == (cadence,)
        condition_id = str(
            conn.execute("SELECT id FROM holding_l3_conditions").fetchone()[0]
        )
    assert (
        cache.main(
            [
                "--confirm-write",
                "l3-update",
                condition_id,
                "watch",
                "2026-10-30",
                "官方证据",
                "2027-04-30",
                "--evidence-cadence",
                "semiannual",
            ]
        )
        == 0
    )
    commands_monitor.cmd_l3_update([condition_id, "watch", "2026-10-30", "保留频率"])
    capsys.readouterr()
    commands_monitor.cmd_l3_list(["000963", "--json"])
    assert (
        json.loads(capsys.readouterr().out)["conditions"][0]["evidence_cadence"]
        == "semiannual"
    )
    commands_monitor.cmd_l3_list(["000963"])
    assert "evidence_cadence:semiannual" in capsys.readouterr().out


@pytest.mark.parametrize(
    "command,args",
    [
        ("l3-add", ["000963", "original", "条件"]),
        ("l3-update", ["1", "watch", "2026-10-30", "证据"]),
    ],
)
def test_l3_cadence_write_gate_and_invalid_values(
    isolated_cache_database, command, args
) -> None:
    assert cache.main([command, *args, "--evidence-cadence", "annual"]) == 3
    assert not isolated_cache_database.exists()
    assert (
        cache.main(["--confirm-write", command, *args, "--evidence-cadence", "monthly"])
        == 1
    )
    assert not isolated_cache_database.exists()


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


def test_thesis_rewrite_preserves_explicit_cadence_and_defaults_missing(
    monkeypatch,
) -> None:
    old_ids = _add_holding_with_l3("000963")
    payload = _payload(old_ids)
    payload["new_l3"][0]["evidence_cadence"] = "semiannual"
    payload["new_l3"].append({**payload["new_l3"][0], "condition": "第二条件"})
    del payload["new_l3"][1]["evidence_cadence"]
    _rewrite(monkeypatch, "000963", payload)
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT evidence_cadence FROM holding_l3_conditions WHERE is_active=1 ORDER BY id"
        ).fetchall() == [("semiannual",), ("unspecified",)]


def test_thesis_rewrite_rejects_invalid_cadence_before_mutation(monkeypatch) -> None:
    old_ids = _add_holding_with_l3("000963")
    payload = _payload(old_ids)
    payload["new_l3"][0]["evidence_cadence"] = "monthly"
    with pytest.raises(SystemExit):
        _rewrite(monkeypatch, "000963", payload)
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT is_active FROM holding_l3_conditions"
        ).fetchall() == [(1,)]
