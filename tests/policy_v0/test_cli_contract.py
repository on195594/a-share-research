from __future__ import annotations

import sqlite3

from a_share_research.policy.v0 import cache, fetcher

CLI_ARGUMENT_CASES = {
    "research": (
        ["reports", "600519"],
        ["report", "9", "--compact"],
    ),
    "check": ([], ["600000"]),
    "get": ([], ["600000"]),
    "set": (["600000", "名称", "行业", "{}"], ["600000", "名称", "行业", "{}", "24"]),
    "get-analysis": ([], ["600000"]),
    "set-analysis": ([], []),
    "set-score": (["600000", "60"], ["600000", "60"]),
    "set-score-breakdown": (["600000", "{}"], ["600000", "{}"]),
    "set-flag": (["600000", "yellow", "原因"], ["600000", "yellow", "原因"]),
    "clear-flag": (["600000"], ["600000"]),
    "alert-open": (
        ["600000", "yellow", "unverified", "code", "none", "原因"],
        ["600000", "yellow", "unverified", "code", "none", "原因", "证据"],
    ),
    "alert-pending": (["600000", "code", "说明"], ["600000", "code", "说明"]),
    "alert-resolve": (["600000", "code", "证据"], ["600000", "code", "证据"]),
    "alerts": (["600000"], ["600000", "--active", "--json"]),
    "l3-add": (
        ["600000", "original", "条件"],
        ["600000", "original", "条件", "临时规则"],
    ),
    "l3-update": (
        ["1", "pending", "2026-08-11", "证据"],
        ["1", "pending", "2026-08-11", "证据", "2026-08-12"],
    ),
    "l3-list": (["600000"], ["600000", "--active", "--json"]),
    "thesis-rewrite": (["600000"], ["600000"]),
    "tier-config": (["600000", "none"], ["600000", "none", "none", "none"]),
    "tier-update": (
        ["600000", "tier1", "pending"],
        ["600000", "tier1", "pending"],
    ),
    "holding-framework": (["600000", "A"], ["600000", "A"]),
    "add-holding": (
        ["600000", "10"],
        [
            "600000",
            "10",
            "100",
            "--notes",
            "备注",
            "--fee",
            "1",
            "--date",
            "2026-08-11",
        ],
    ),
    "buy-holding": (
        ["600000", "10", "100"],
        ["600000", "10", "100", "--fee", "1", "--date", "2026-08-11"],
    ),
    "sell-holding": (
        ["600000", "10", "all"],
        [
            "600000",
            "10",
            "all",
            "--fee",
            "1",
            "--tax",
            "1",
            "--date",
            "2026-08-11",
        ],
    ),
    "record-dividend": (["600000", "1"], ["600000", "1", "2026-08-11"]),
    "corporate-action": (
        ["600000", "1", "0"],
        ["600000", "1", "0", "2026-08-11"],
    ),
    "close-holding": (["600000", "10"], ["600000", "10", "2026-08-11"]),
    "retro-add": (
        ["600000", "无错误"],
        [
            "600000",
            "无错误",
            "--note",
            "备注",
            "--thesis",
            "理由",
            "--gap",
            "改进",
        ],
    ),
    "retro-pending": ([], []),
    "retro-stats": ([], ["A通用"]),
    "retro-outliers": ([], ["--loss", "10"]),
    "holdings": ([], ["600000", "--compact", "--json", "--active-only"]),
    "remove-holding": (["600000"], ["600000"]),
    "update-return": (["600000", "10"], ["600000", "10"]),
    "position-return": (["600000"], ["600000", "10"]),
    "dividend-check": ([], ["600000"]),
    "benchmark-report": ([], ["--end", "2026-10-21"]),
    "report-trades": ([], ["--end", "2026-10-21"]),
    "daily-brief": ([], []),
    "stop-alert": ([], []),
    "regulatory-evidence": (["600000"], ["600000"]),
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
    "cleanup": ([], []),
    "clear": ([], ["600000"]),
    "checklist": (["600000", "A"], ["600000", "A"]),
    "score-fundamentals": (
        ["600000", "A", "{}"],
        ["600000", "A", '{"gross_margin_stable":true}'],
    ),
    "performance-report": (
        ["--input", "account.json"],
        [
            "--input",
            "account.json",
            "--benchmark",
            "benchmark.json",
            "--check-ledger",
            "--allow-eod-flow-assumption",
            "--json",
        ],
    ),
}


def test_public_mains_return_int() -> None:
    assert isinstance(cache.main(["--help"]), int)
    assert isinstance(fetcher.main(["--help"]), int)


def test_w1_requires_prefix_confirmation_without_opening_database(
    tmp_path, monkeypatch, capsys
) -> None:
    database = tmp_path / "cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    assert cache.main(["add-holding", "000001", "10", "100"]) == 3
    assert not database.exists()
    assert "--confirm-write" in capsys.readouterr().err


def test_confirmed_fixture_write_is_transactional(tmp_path, monkeypatch) -> None:
    database = tmp_path / "cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    assert cache.main(["--confirm-write", "add-holding", "000001", "10", "100"]) == 0
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT shares FROM holdings").fetchone() == (100,)


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
        prefix = (
            ["--confirm-write"] if cache.COMMAND_CLASSIFICATION[command] == "W1" else []
        )
        for case in cases:
            called.clear()
            assert cache.main([*prefix, command, *case]) == 0, command
            assert called == [(command, case)], command


def test_every_w1_command_requires_prefix_confirmation(monkeypatch, capsys) -> None:
    def fail_if_called(_args) -> None:
        raise AssertionError("write command dispatched without confirmation")

    for command, classification in cache.COMMAND_CLASSIFICATION.items():
        if classification != "W1":
            continue
        monkeypatch.setitem(cache.COMMANDS, command, fail_if_called)
        assert cache.main([command, *CLI_ARGUMENT_CASES[command][0]]) == 3, command
        assert "--confirm-write" in capsys.readouterr().err


def test_confirmation_position_and_unknown_global_option(monkeypatch) -> None:
    called: list[list[str]] = []
    monkeypatch.setitem(cache.COMMANDS, "set-flag", called.append)
    args = ["600000", "yellow", "原因"]

    assert cache.main(["set-flag", "--confirm-write", *args]) == 3
    assert cache.main(["--bogus", "set-flag", *args]) == 2
    assert cache.main(["--confirm-write", "set-flag", *args]) == 0
    assert called == [args]


def test_argparse_rejects_extra_positionals_and_unknown_options(monkeypatch) -> None:
    def fail_if_called(_args) -> None:
        raise AssertionError("invalid argv reached command handler")

    cache.paths.cache_db_path().touch()
    for command, (_, maximum) in CLI_ARGUMENT_CASES.items():
        monkeypatch.setitem(cache.COMMANDS, command, fail_if_called)
        prefix = (
            ["--confirm-write"] if cache.COMMAND_CLASSIFICATION[command] == "W1" else []
        )
        assert cache.main([*prefix, command, *maximum, "EXTRA_POSITIONAL"]) == 2, (
            command
        )
        assert cache.main([*prefix, command, "--unknown-option"]) == 2, command


def test_every_subcommand_has_help(capsys) -> None:
    for command in cache.COMMANDS:
        assert cache.main([command, "--help"]) == 0, command
        assert command in capsys.readouterr().out


def test_phase4_structured_command_help_and_schema(capsys) -> None:
    assert cache._CLI_POSITIONALS["set-analysis"] == ()
    assert cache.main(["set-analysis", "--help"]) == 0
    set_analysis_help = capsys.readouterr().out
    assert "usage: a-stock-cache set-analysis [-h]" in set_analysis_help
    assert "代码" not in set_analysis_help
    assert "框架" not in set_analysis_help

    assert cache._CLI_POSITIONALS["score-fundamentals"] == (
        ("代码", None),
        ("框架", None),
        ("评分输入JSONv1", None),
    )
    assert cache.main(["score-fundamentals", "--help"]) == 0
    scoring_help = capsys.readouterr().out
    assert "代码 框架 评分输入JSONv1" in scoring_help

    assert "decision-v1 JSON" in set_analysis_help
    assert "stdin" in set_analysis_help
    assert "set-analysis <代码> <框架> [得分]" not in set_analysis_help
    assert "报告正文从stdin读取" not in set_analysis_help


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
