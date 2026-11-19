from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

MODULES = (
    "cache",
    "framework_catalog",
    "market_quotes",
    "position_ledger",
    "paths",
    "domain",
    "db",
    "store",
    "commands_holdings",
    "commands_monitor",
    "commands_admin",
)


def test_runtime_modules_import_from_package() -> None:
    for module in MODULES:
        importlib.import_module(f"a_share_research.policy.v0.{module}")


def test_domain_routing_is_independent_of_checklist_import_order() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys; from a_share_research.policy.v0 import domain; "
        "assert 'a_share_research.policy.v0.checklist' not in sys.modules; "
        "assert domain.infer_framework('煤炭开采') == ('C资源', True); "
        "assert domain.get_stop_loss_pct('F科技') == (0.80, 0.72)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_commands_monitor_observes_holdings_owner_patches(monkeypatch) -> None:
    from a_share_research.policy.v0 import commands_holdings, commands_monitor, db
    from policy_v0.helpers import seed_position

    def patched_parser(*_args, **_kwargs):
        raise RuntimeError("parser owner patch observed")

    monkeypatch.setattr(commands_holdings, "_parse_portfolio_risk_args", patched_parser)
    with pytest.raises(RuntimeError, match="parser owner patch observed"):
        commands_monitor._parse_monitor_snapshot_args(["--json"])

    holding_id = seed_position("000001", 10, 100)
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO holding_l3_conditions
               (holding_id, condition_text, created_at, updated_at)
               VALUES (?, 'condition', 'now', 'now')""",
            (holding_id,),
        )
        conn.commit()

    def patched_holding(*_args, **_kwargs):
        raise RuntimeError("holding owner patch observed")

    monkeypatch.setattr(commands_holdings, "_single_open_holding", patched_holding)
    with pytest.raises(RuntimeError, match="holding owner patch observed"):
        commands_monitor.cmd_l3_list(["000001"])


def test_unused_cache_facade_does_not_return() -> None:
    from a_share_research.policy.v0 import cache, db, domain, risk_gates

    for owner, name in (
        (db, "read_only_db_session"),
        (domain, "parse_timestamp_utc"),
        (risk_gates, "liquidity_shock_gate"),
        (db, "get_db"),
        (domain, "cst_today"),
    ):
        assert callable(getattr(owner, name))
        assert not hasattr(cache, name)
    for handler in cache.COMMANDS.values():
        assert not hasattr(cache, handler.__name__)
