#!/usr/bin/env python3
"""A股投研数据缓存 CLI；命令和参数以生成的 --help 为准。

R0 只读；R1 可读取外部数据并刷新缓存，但不授权投资状态写入。账户写入只走账本工具，
v0 的记账写命令已于 2026-11-19 删除。
"""

import argparse
import sqlite3
import sys

from a_share_research.policy.v0 import (
    commands_admin,
    commands_holdings,
    commands_monitor,
    db,
    paths,
    research_client,
    portfolio_scenario,
    position_size,
)

COMMANDS = {
    "research": research_client.cmd_research,
    "portfolio-scenario": portfolio_scenario.cmd_portfolio_scenario,
    "position-size": position_size.cmd_position_size,
    "alerts": commands_monitor.cmd_alerts,
    "l3-list": commands_monitor.cmd_l3_list,
    "retro-pending": commands_holdings.cmd_retro_pending,
    "retro-stats": commands_holdings.cmd_retro_stats,
    "retro-outliers": commands_holdings.cmd_retro_outliers,
    "holdings": commands_holdings.cmd_holdings,
    "position-return": commands_holdings.cmd_position_return,
    "dividend-check": commands_holdings.cmd_dividend_check,
    "benchmark-report": commands_holdings.cmd_benchmark_report,
    "report-trades": commands_holdings.cmd_report_trades,
    "portfolio-risk": commands_holdings.cmd_portfolio_risk,
    "check-holdings": commands_holdings.cmd_check_holdings,
    "stop-alert": commands_holdings.cmd_stop_alert,
    "monitor-snapshot": commands_monitor.cmd_monitor_snapshot,
    "daily-brief": commands_monitor.cmd_daily_brief,
    "watchlist": commands_admin.cmd_watchlist,
    "list": commands_admin.cmd_list,
}

# One source of truth for the side-effect boundary.  R0 is local read-only and
# R1 may refresh/read external data or cache it.  Unknown commands are rejected.
COMMAND_CLASSIFICATION = {
    **dict.fromkeys(
        (
            "holdings",
            "position-return",
            "retro-pending",
            "retro-stats",
            "retro-outliers",
            "alerts",
            "l3-list",
            "watchlist",
            "list",
            "portfolio-scenario",
            "position-size",
        ),
        "R0",
    ),
    **dict.fromkeys(
        (
            "research",
            "check-holdings",
            "portfolio-risk",
            "monitor-snapshot",
            "dividend-check",
            "benchmark-report",
            "report-trades",
            "daily-brief",
            "stop-alert",
        ),
        "R1",
    ),
}


class _CLIParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"参数错误：{message}", file=sys.stderr)
        raise SystemExit(2)


_CLI_POSITIONALS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "research": (),
    "portfolio-scenario": (),
    "position-size": (),
    "alerts": (("代码", None),),
    "l3-list": (("代码", None),),
    "retro-pending": (),
    "retro-stats": (("框架", "?"),),
    "retro-outliers": (),
    "holdings": (("代码", "?"),),
    "position-return": (("代码", None), ("当前价", "?")),
    "dividend-check": (("代码", "?"),),
    "benchmark-report": (),
    "report-trades": (),
    "daily-brief": (),
    "stop-alert": (),
    "portfolio-risk": (),
    "check-holdings": (),
    "monitor-snapshot": (),
    "watchlist": (),
    "list": (),
}

_CLI_VALUE_OPTIONS = {
    "retro-outliers": ("--loss",),
    "portfolio-risk": (
        "--portfolio-value",
        "--max-position-risk-pct",
        "--max-portfolio-risk-pct",
        "--policy-file",
        "--account-scope",
        "--portfolio-value-as-of",
    ),
    "monitor-snapshot": (
        "--portfolio-value",
        "--max-position-risk-pct",
        "--max-portfolio-risk-pct",
        "--policy-file",
        "--account-scope",
        "--portfolio-value-as-of",
    ),
    "benchmark-report": ("--end",),
    "report-trades": ("--end",),
}


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = _CLIParser(
        prog="a-stock-cache",
        description="A股投研数据缓存管理器",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<子命令>")
    for command, positionals in _CLI_POSITIONALS.items():
        handler_doc = COMMANDS[command].__doc__ or ""
        command_parser = subparsers.add_parser(
            command,
            description=handler_doc,
            help=handler_doc.splitlines()[0].replace("%", "%%")
            if handler_doc
            else None,
        )
        if command == "research":
            research_client.configure_parser(command_parser)
        if command == "portfolio-scenario":
            portfolio_scenario.configure_parser(command_parser)
        if command == "position-size":
            position_size.configure_parser(command_parser)
        for index, (metavar, nargs) in enumerate(positionals, start=1):
            if nargs is None:
                command_parser.add_argument(f"arg{index}", metavar=metavar)
            else:
                command_parser.add_argument(f"arg{index}", metavar=metavar, nargs=nargs)
        for option in _CLI_VALUE_OPTIONS.get(command, ()):
            command_parser.add_argument(option, metavar="值")
        if command in {"watchlist", "monitor-snapshot"}:
            command_parser.add_argument("--json", action="store_true")
        if command == "monitor-snapshot":
            command_parser.add_argument("--no-research", action="store_true")
        if command == "watchlist":
            command_parser.add_argument("--breakdown", action="store_true")
        if command == "holdings":
            command_parser.add_argument("--compact", action="store_true")
            command_parser.add_argument("--json", action="store_true")
            command_parser.add_argument("--active-only", action="store_true")
        if command == "alerts":
            command_parser.add_argument("--active", action="store_true")
            command_parser.add_argument("--json", action="store_true")
        if command == "l3-list":
            command_parser.add_argument("--all", action="store_true")
            command_parser.add_argument("--active", action="store_true")
            command_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_cli_parser()
    if not args:
        parser.print_help()
        return 0
    if args[0].startswith("--") and args != ["--help"]:
        print("错误：不支持全局选项", file=sys.stderr)
        return 2
    if args == ["--help"]:
        try:
            parser.parse_args(args)
        except SystemExit as exc:
            return int(exc.code or 0)
    command, remaining = args[0], args[1:]
    classification = COMMAND_CLASSIFICATION.get(command)
    if command not in COMMANDS or classification is None:
        print(f"错误：未知命令 {command}", file=sys.stderr)
        parser.print_help(sys.stderr)
        return 1
    if remaining == ["--help"]:
        try:
            parser.parse_args(args)
        except SystemExit as exc:
            return int(exc.code or 0)
    try:
        parser.parse_args(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    if command in {"research", "portfolio-scenario", "position-size"}:
        return int(COMMANDS[command](remaining) or 0)
    try:
        database_path = paths.cache_db_path()
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"运行配置不可用：{type(exc).__name__}", file=sys.stderr)
        return 1
    if classification == "R0" and not database_path.exists():
        machine_view_missing = (
            command in {"alerts", "l3-list"} and "--json" in remaining
        )
        if command == "holdings" or machine_view_missing:
            marker = {
                "holdings": "HOLDINGS_UNAVAILABLE",
                "alerts": "ALERTS_UNAVAILABLE",
                "l3-list": "L3_LIST_UNAVAILABLE",
            }[command]
            print(
                f"{marker} database_missing:{database_path}",
                file=sys.stderr,
            )
            return 1
        print(f"状态数据库不存在：{database_path}", file=sys.stderr)
        return 0
    print(f"[a-stock-cache] 操作数据库: {database_path}", file=sys.stderr)
    try:
        with db.read_only_scope(classification == "R0"):
            COMMANDS[command](remaining)
    except SystemExit as exc:
        return int(exc.code or 0)
    except sqlite3.Error as exc:
        print(f"数据库查询失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
