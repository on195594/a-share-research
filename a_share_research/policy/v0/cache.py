#!/usr/bin/env python3
"""A股投研数据缓存 CLI；命令和参数以生成的 --help 为准。

W1 命令必须在子命令前提供 --confirm-write，否则返回 3 且不打开写事务。
R0 只读；R1 可读取外部数据并刷新缓存，但不授权投资状态写入。
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
    "set-flag": commands_monitor.cmd_set_flag,
    "clear-flag": commands_monitor.cmd_clear_flag,
    "alert-open": commands_monitor.cmd_alert_open,
    "alert-pending": commands_monitor.cmd_alert_pending,
    "alert-resolve": commands_monitor.cmd_alert_resolve,
    "alerts": commands_monitor.cmd_alerts,
    "l3-add": commands_monitor.cmd_l3_add,
    "l3-update": commands_monitor.cmd_l3_update,
    "l3-list": commands_monitor.cmd_l3_list,
    "thesis-rewrite": commands_monitor.cmd_thesis_rewrite,
    "tier-config": commands_monitor.cmd_tier_config,
    "tier-update": commands_monitor.cmd_tier_update,
    "holding-framework": commands_monitor.cmd_holding_framework,
    "add-holding": commands_holdings.cmd_add_holding,
    "buy-holding": commands_holdings.cmd_buy_holding,
    "sell-holding": commands_holdings.cmd_sell_holding,
    "record-dividend": commands_holdings.cmd_record_dividend,
    "corporate-action": commands_holdings.cmd_corporate_action,
    "close-holding": commands_holdings.cmd_close_holding,
    "retro-add": commands_holdings.cmd_retro_add,
    "retro-pending": commands_holdings.cmd_retro_pending,
    "retro-stats": commands_holdings.cmd_retro_stats,
    "retro-outliers": commands_holdings.cmd_retro_outliers,
    "holdings": commands_holdings.cmd_holdings,
    "remove-holding": commands_holdings.cmd_remove_holding,
    "update-return": commands_holdings.cmd_update_return,
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
    "cleanup": commands_admin.cmd_cleanup,
    "clear": commands_admin.cmd_clear,
}

# One source of truth for the side-effect boundary.  R0 is local read-only,
# R1 may refresh/read external data or cache it, and W1 changes investment
# state.  Unknown commands are rejected rather than silently treated as safe.
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
    **dict.fromkeys(
        (
            "set-flag",
            "clear-flag",
            "alert-open",
            "alert-pending",
            "alert-resolve",
            "l3-add",
            "l3-update",
            "thesis-rewrite",
            "tier-config",
            "tier-update",
            "holding-framework",
            "add-holding",
            "buy-holding",
            "sell-holding",
            "record-dividend",
            "corporate-action",
            "close-holding",
            "retro-add",
            "remove-holding",
            "update-return",
            "cleanup",
            "clear",
        ),
        "W1",
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
    "set-flag": (("代码", None), ("级别", None), ("原因", None)),
    "clear-flag": (("代码", None),),
    "alert-open": (
        ("代码", None),
        ("级别", None),
        ("类别", None),
        ("reason_code", None),
        ("复核日期", None),
        ("原因", None),
        ("证据", "?"),
    ),
    "alert-pending": (("代码", None), ("reason_code", None), ("说明", None)),
    "alert-resolve": (("代码", None), ("reason_code", None), ("证据", None)),
    "alerts": (("代码", None),),
    "l3-add": (("代码", None), ("来源", None), ("条件", None), ("临时规则", "?")),
    "l3-update": (
        ("条件id", None),
        ("状态", None),
        ("as-of", None),
        ("证据", None),
        ("下次复核", "?"),
    ),
    "l3-list": (("代码", None),),
    "thesis-rewrite": (("代码", None),),
    "tier-config": (
        ("代码", None),
        ("路径", None),
        ("目标涨幅", "?"),
        ("豁免框架", "?"),
    ),
    "tier-update": (("代码", None), ("Tier", None), ("状态", None)),
    "holding-framework": (("代码", None), ("框架", None)),
    "add-holding": (("代码", None), ("成交价", None), ("股数", "?")),
    "buy-holding": (("代码", None), ("买入价", None), ("股数", None)),
    "sell-holding": (("代码", None), ("卖出价", None), ("股数或all", None)),
    "record-dividend": (("代码", None), ("现金总额", None), ("日期", "?")),
    "corporate-action": (
        ("代码", None),
        ("每股现金分红", None),
        ("转增比例", None),
        ("日期", "?"),
    ),
    "close-holding": (("代码", None), ("卖出价", None), ("日期", "?")),
    "retro-add": (("代码", None), ("error_tags", None)),
    "retro-pending": (),
    "retro-stats": (("框架", "?"),),
    "retro-outliers": (),
    "holdings": (("代码", "?"),),
    "remove-holding": (("代码", None),),
    "update-return": (("代码", None), ("回报率", None)),
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
    "cleanup": (),
    "clear": (("代码", "?"),),
}

_CLI_VALUE_OPTIONS = {
    "l3-add": ("--evidence-cadence",),
    "l3-update": ("--evidence-cadence",),
    "add-holding": ("--notes", "--fee", "--date", "--report-id"),
    "buy-holding": ("--fee", "--date", "--report-id"),
    "sell-holding": ("--fee", "--tax", "--date", "--report-id"),
    "retro-add": ("--note", "--thesis", "--gap"),
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
        epilog="W1（需 --confirm-write）："
        + ", ".join(
            name for name, level in COMMAND_CLASSIFICATION.items() if level == "W1"
        ),
    )
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="确认执行会修改本地投资状态的 W1 子命令（必须位于子命令前）",
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
    confirm_write = bool(args and args[0] == "--confirm-write")
    if confirm_write:
        args.pop(0)
    if not confirm_write and args and args[0].startswith("--") and args != ["--help"]:
        print("错误：仅支持位于子命令前的全局 --confirm-write", file=sys.stderr)
        return 2
    if not args:
        parser.print_help()
        return 0
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
    if classification == "W1" and not confirm_write:
        print(
            f"需要明确确认：{command} 将修改本地投资状态；"
            "请在子命令前提供 --confirm-write。",
            file=sys.stderr,
        )
        return 3
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
