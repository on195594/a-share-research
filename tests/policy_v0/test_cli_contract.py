from __future__ import annotations

import sqlite3

from a_share_research.policy.v0 import cache

def test_cli_help_does_not_create_state(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cache.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert all(name in output for name in cache.COMMANDS)
    assert "--confirm-write" not in output
    assert not (tmp_path / ".local").exists()
    assert cache.COMMANDS.keys() == cache.COMMAND_CLASSIFICATION.keys()
    assert set(cache.COMMAND_CLASSIFICATION.values()) == {"R0", "R1"}


def test_holdings_missing_database_is_unavailable_not_not_held(
    tmp_path, monkeypatch, capsys
) -> None:
    database = tmp_path / "missing.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))

    assert cache.main(["holdings", "603606"]) == 1

    captured = capsys.readouterr()
    assert "HOLDINGS_UNAVAILABLE" in captured.err
    assert "NOT_HELD" not in captured.out
    assert not database.exists()


def test_holdings_invalid_schema_is_unavailable(tmp_path, monkeypatch, capsys) -> None:
    database = tmp_path / "invalid.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE unrelated(value TEXT)")

    assert cache.main(["holdings", "603606"]) == 1

    captured = capsys.readouterr()
    assert "HOLDINGS_UNAVAILABLE" in captured.err
    assert "NOT_HELD" not in captured.out


def test_json_monitoring_views_fail_when_database_is_missing(
    tmp_path, monkeypatch, capsys
) -> None:
    database = tmp_path / "missing.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))

    for argv, marker in (
        (["alerts", "600036", "--active", "--json"], "ALERTS_UNAVAILABLE"),
        (["l3-list", "600036", "--active", "--json"], "L3_LIST_UNAVAILABLE"),
    ):
        assert cache.main(argv) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert marker in captured.err
        assert not database.exists()


def test_global_options_and_removed_write_commands_never_dispatch(monkeypatch) -> None:
    called: list[list[str]] = []
    monkeypatch.setitem(cache.COMMANDS, "holdings", called.append)

    assert cache.main(["--bogus", "holdings"]) == 2
    assert cache.main(["--confirm-write", "holdings"]) == 2
    assert cache.main(["sell-holding", "600000", "10", "100"]) == 1
    assert called == []


def test_argparse_rejects_extra_positionals_and_unknown_options(monkeypatch) -> None:
    def fail_if_called(_args) -> None:
        raise AssertionError("invalid argv reached command handler")

    cache.paths.cache_db_path().touch()
    monkeypatch.setitem(cache.COMMANDS, "holdings", fail_if_called)
    assert cache.main(["holdings", "600000", "EXTRA_POSITIONAL"]) == 2
    assert cache.main(["holdings", "--unknown-option"]) == 2

def test_every_subcommand_has_help(capsys) -> None:
    for command in cache.COMMANDS:
        assert cache.main([command, "--help"]) == 0, command
        assert command in capsys.readouterr().out
