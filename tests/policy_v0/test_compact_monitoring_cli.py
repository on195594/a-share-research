from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from a_share_research.policy.v0 import cache


PROJECT_ROOT = Path(__file__).resolve().parents[2] / "a_share_research/policy/v0/guide"


def _add_holding(tmp_path, monkeypatch, capsys, *, notes: str = "long private note"):
    database = tmp_path / "cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    assert (
        cache.main(
            [
                "--confirm-write",
                "add-holding",
                "600036",
                "10",
                "100",
                "--notes",
                notes,
            ]
        )
        == 0
    )
    capsys.readouterr()
    return database


def test_holdings_json_is_compact_and_omits_notes(
    tmp_path, monkeypatch, capsys
) -> None:
    _add_holding(tmp_path, monkeypatch, capsys)

    assert cache.main(["holdings", "--compact", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == [
        {
            "id": 1,
            "code": "600036",
            "name": None,
            "status": "active",
            "shares": 100,
            "cost_price": 10.0,
            "reference_cost": 10.0,
            "buy_date": payload[0]["buy_date"],
            "buy_score": None,
            "framework": "A通用",
            "framework_confident": False,
            "initial_shares": 100,
            "main_entry_date": payload[0]["main_entry_date"],
            "stop_loss_15": 8.5,
            "stop_loss_20": 8.0,
            "exit_price": None,
            "exit_date": None,
        }
    ]
    assert "long private note" not in captured.out


def test_holdings_compact_text_omits_notes(tmp_path, monkeypatch, capsys) -> None:
    _add_holding(tmp_path, monkeypatch, capsys)

    assert cache.main(["holdings", "600036", "--compact"]) == 0

    out = capsys.readouterr().out
    assert "600036" in out
    assert "A通用" in out
    assert "long private note" not in out


def test_holdings_active_only_excludes_closed_history(
    tmp_path, monkeypatch, capsys
) -> None:
    _add_holding(tmp_path, monkeypatch, capsys)
    assert cache.main(["--confirm-write", "add-holding", "601088", "20", "200"]) == 0
    assert cache.main(["--confirm-write", "sell-holding", "601088", "21", "all"]) == 0
    capsys.readouterr()

    assert cache.main(["holdings", "--compact", "--json", "--active-only"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [(item["code"], item["status"]) for item in payload] == [
        ("600036", "active")
    ]


def test_holdings_active_only_returns_reopened_lifecycle(
    tmp_path, monkeypatch, capsys
) -> None:
    _add_holding(tmp_path, monkeypatch, capsys)
    assert cache.main(["--confirm-write", "sell-holding", "600036", "51", "all"]) == 0
    assert cache.main(["--confirm-write", "add-holding", "600036", "52", "100"]) == 0
    capsys.readouterr()

    assert (
        cache.main(["holdings", "600036", "--compact", "--json", "--active-only"]) == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["status"] == "active"
    assert payload[0]["shares"] == 100


def test_alerts_active_json_excludes_resolved(tmp_path, monkeypatch, capsys) -> None:
    database = _add_holding(tmp_path, monkeypatch, capsys)
    with sqlite3.connect(database) as conn:
        holding_id = conn.execute(
            "SELECT id FROM holdings WHERE code='600036' AND exit_date IS NULL"
        ).fetchone()[0]
        now = "2026-09-01T00:00:00+00:00"
        conn.executemany(
            """INSERT INTO holding_alerts
               (holding_id, code, level, category, reason_code, reason, status,
                evidence, opened_at, updated_at)
               VALUES (?, '600036', 'yellow', 'unverified', ?, ?, ?, ?, ?, ?)""",
            [
                (holding_id, "active-one", "active reason", "active", "e1", now, now),
                (
                    holding_id,
                    "pending-one",
                    "pending reason",
                    "pending",
                    "e2",
                    now,
                    now,
                ),
                (
                    holding_id,
                    "resolved-one",
                    "resolved reason",
                    "resolved",
                    "e3",
                    now,
                    now,
                ),
            ],
        )

    assert cache.main(["alerts", "600036", "--active", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [item["reason_code"] for item in payload] == ["active-one", "pending-one"]
    assert all(item["status"] != "resolved" for item in payload)


def test_l3_active_json_preserves_legacy_boundary(
    tmp_path, monkeypatch, capsys
) -> None:
    _add_holding(tmp_path, monkeypatch, capsys)
    assert (
        cache.main(
            [
                "--confirm-write",
                "l3-add",
                "600036",
                "original",
                "capital ratio below threshold",
                "review only",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert cache.main(["l3-list", "600036", "--active", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "600036"
    assert payload["contract"] == "legacy"
    assert payload["warning"]
    assert len(payload["conditions"]) == 1
    assert payload["conditions"][0]["condition"] == "capital ratio below threshold"
    assert payload["conditions"][0]["active"] is True


def test_l3_active_json_exposes_valid_versioned_contract(
    tmp_path, monkeypatch, capsys
) -> None:
    database = _add_holding(tmp_path, monkeypatch, capsys)
    with sqlite3.connect(database) as conn:
        holding_id = conn.execute(
            "SELECT id FROM holdings WHERE code='600036' AND exit_date IS NULL"
        ).fetchone()[0]
        thesis_id = conn.execute(
            """INSERT INTO holding_thesis_versions
               (holding_id, version, l1, l2, rewrite_reason, status, created_at)
               VALUES (?, 1, 'quality', 'timing', 'fixture', 'active', '2026-09-01')""",
            (holding_id,),
        ).lastrowid
        conn.execute(
            """INSERT INTO holding_l3_conditions
               (holding_id, thesis_version_id, condition_text, origin_type, status,
                is_active, condition_scope, action_level, materiality_basis,
                temporary_exit_rule, created_at, updated_at)
               VALUES (?, ?, 'earnings fail', 'new_monitoring', 'watch', 1,
                       'core_driver', 'review', 'main earnings driver',
                       'review next report', '2026-09-01', '2026-09-01')""",
            (holding_id, thesis_id),
        )

    assert cache.main(["l3-list", "600036", "--active", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["contract"] == "versioned"
    assert payload["thesis"]["version"] == 1
    assert payload["conditions"][0]["valid_for_action"] is True
    assert payload["conditions"][0]["status"] == "watch"


def test_l3_rejects_conflicting_lifecycle_filters(
    tmp_path, monkeypatch, capsys
) -> None:
    _add_holding(tmp_path, monkeypatch, capsys)

    assert cache.main(["l3-list", "600036", "--all", "--active"]) == 1
    assert "不能同时使用" in capsys.readouterr().err


def test_monitor_skill_uses_compact_views_and_preserves_uncertainty() -> None:
    skill = (PROJECT_ROOT / "a-stock-monitor/SKILL.md").read_text(
        encoding="utf-8"
    )
    daily = (
        PROJECT_ROOT / "a-stock-monitor/references/daily-monitoring-and-l3.md"
    ).read_text(encoding="utf-8")

    assert "a-stock-cache monitor-snapshot --json" in skill
    assert "默认使用系统保存的最新账户总资产" in skill
    assert "解析前不得额外调用 `holdings`" in skill
    assert "缺少 required 数据时不得输出确定性无动作" in skill
    assert "不得补造触发、股数或成交" in skill
    assert "L3 全量核查强制要求" in daily
    assert '其余条件若本次无数据可判断，须明确标注"待观察' in daily
    assert "holdings --compact --json" not in skill
    assert "l3-list <代码> --active --json" not in skill
