from __future__ import annotations

import sqlite3

from a_share_research.policy.v0 import cache

CLI_ARGUMENT_CASES = {
    "research": (
        ["reports", "600519"],
        ["report", "9", "--compact"],
    ),
    "alerts": (["600000"], ["600000", "--active", "--json"]),
    "l3-list": (["600000"], ["600000", "--active", "--json"]),
    "retro-pending": ([], []),
    "retro-stats": ([], ["A通用"]),
    "retro-outliers": ([], ["--loss", "10"]),
    "holdings": ([], ["600000", "--compact", "--json", "--active-only"]),
    "position-return": (["600000"], ["600000", "10"]),
    "dividend-check": ([], ["600000"]),
    "benchmark-report": ([], ["--end", "2026-10-21"]),
    "report-trades": ([], ["--end", "2026-10-21"]),
    "daily-brief": ([], []),
    "stop-alert": ([], []),
    "portfolio-scenario": (
        ["--snapshot", "/tmp/snapshot.json", "--sell", "600000:100", "--fees", "0"],
        ["--snapshot", "/tmp/snapshot.json", "--sell", "600000:100", "--sell", "600001:200", "--fees", "5", "--cash", "100"],
    ),
    "position-size": (
        ["--snapshot", "/tmp/snapshot.json", "--code", "600000", "--price", "10", "--framework", "B"],
        ["--snapshot", "/tmp/snapshot.json", "--code", "600000", "--price", "10", "--framework", "B", "--cash", "1000"],
    ),
    "portfolio-risk": (
        [],
        [
            "--portfolio-value",
            "100000",
            "--max-position-risk-pct",
            "2",
            "--max-portfolio-risk-pct",
            "8",
        ],
    ),
    "check-holdings": ([], []),
    "monitor-snapshot": (
        ["--json"],
        [
            "--portfolio-value",
            "100000",
            "--max-position-risk-pct",
            "2",
            "--max-portfolio-risk-pct",
            "8",
            "--json",
        ],
    ),
    "watchlist": ([], ["--json", "--breakdown"]),
    "list": ([], []),
}


def test_public_mains_return_int() -> None:
    assert isinstance(cache.main(["--help"]), int)


def test_cli_help_does_not_create_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cache.main(["--help"]) == 0
    assert not (tmp_path / ".local").exists()


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


def test_cli_argument_matrix_covers_every_command(monkeypatch) -> None:
    assert set(CLI_ARGUMENT_CASES) == set(cache.COMMANDS)
    cache.paths.cache_db_path().touch()
    called: list[tuple[str, list[str]]] = []
    for command, cases in CLI_ARGUMENT_CASES.items():
        monkeypatch.setitem(
            cache.COMMANDS,
            command,
            lambda args, name=command: called.append((name, args)),
        )
        for case in cases:
            called.clear()
            assert cache.main([command, *case]) == 0, command
            assert called == [(command, case)], command


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
    for command, (_, maximum) in CLI_ARGUMENT_CASES.items():
        monkeypatch.setitem(cache.COMMANDS, command, fail_if_called)
        assert cache.main([command, *maximum, "EXTRA_POSITIONAL"]) == 2, command
        assert cache.main([command, "--unknown-option"]) == 2, command


def test_every_subcommand_has_help(capsys) -> None:
    for command in cache.COMMANDS:
        assert cache.main([command, "--help"]) == 0, command
        assert command in capsys.readouterr().out


def test_sina_parser_preserves_price_when_previous_close_is_malformed() -> None:
    from a_share_research.policy.v0 import market_quotes

    for previous_close, expected in (("bad", None), ("", None), ("99", 99.0)):
        fields = ["fixture", "100", previous_close, "101"] + [""] * 26
        fields += ["2026-09-08", "10:00:00"]
        line = 'var hq_str_sh600036="' + ",".join(fields) + '";'
        expected_quote = ("600036", 101.0, "2026-09-08", "10:00:00", expected)
        assert market_quotes.parse_sina_quote_line(line, {"600036"}) == expected_quote
        fields[3] = "bad"
        line = 'var hq_str_sh600036="' + ",".join(fields) + '";'
        assert market_quotes.parse_sina_quote_line(line, {"600036"}) is None
