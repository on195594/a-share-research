"""Read research results in the policy process, including cards for verified holdings."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

from a_share_research import reading, service
from a_share_research.policy.v0 import paths

_RESUME_HELP = "请沿用原凭据调用 research_status，传入原 request_id。"


def _code(value: str) -> str:
    if not re.fullmatch(r"[0-9]{6}", value):
        raise argparse.ArgumentTypeError("股票代码须为六位数字")
    return value


def _positive_id(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("编号须为正整数")
    return number


def configure_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("cards", help="读取扫描范围内的论点卡")
    card = actions.add_parser("card", help="读取公司论点卡")
    card.add_argument("code", type=_code)
    reports = actions.add_parser("reports", help="读取公司历史报告目录")
    reports.add_argument("code", type=_code)
    for name, label in (("report", "读取报告及原文证据"), ("status", "读取任务状态")):
        action = actions.add_parser(name, help=label)
        action.add_argument("id", type=_positive_id)
    actions.add_parser("budget", help="读取研究调用上限；不是费用报价，不启动研究")
    resume = actions.add_parser("resume", help=_RESUME_HELP, description=_RESUME_HELP)
    resume.add_argument("--request-id", required=True)
    # Explicit opt-in preserves the existing full report wire contract.
    actions.choices["report"].add_argument("--compact", action="store_true")


class ResearchNotConfigured(ValueError):
    """There is no application database to read research from."""


def _require_blind(mode: str | None) -> None:
    if mode == "holdings_context":
        raise service.RequestError("知仓研究仅限启用账户工具的助手凭据", 403)


def _report(conn, report_id: int, compact: bool) -> dict:
    _require_blind(service.report_input_mode(conn, report_id))
    return service.report_view(conn, report_id, compact=compact)


def _run(conn, run_id: int) -> dict:
    _require_blind(service.run_input_mode(conn, run_id))
    row = reading.api_run(conn, run_id)
    if row is None:
        raise service.RequestError("任务不存在", 404)
    return {"version": service.VERSION, **dict(row)}


def _read(handler) -> dict:
    """Answer one research-v1 read in process with the function the web service itself calls."""
    database = paths.cache_db_path()
    if not database.exists():
        raise ResearchNotConfigured(f"研究库不存在：{database}")
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        payload = handler(conn)
    except service.RequestError as exc:
        raise ValueError(f"Research {exc.status}：{exc}") from None
    finally:
        conn.close()
    if not isinstance(payload, dict) or payload.get("version") != "research-v1":
        raise ValueError("Research 响应版本不兼容")
    return payload


def read_cards() -> dict:
    """Subscription, frozen L5 and verified-holding cards; raises ValueError with a message that is safe to show."""
    return _read(lambda conn: service.company_cards(conn, holdings=True))


def cmd_research(argv: list[str]) -> int:
    """读取研究报告、论点卡与任务状态；不发起研究，不修改账户或评分。"""
    parser = argparse.ArgumentParser(prog="a-stock-cache research")
    configure_parser(parser)
    args = parser.parse_args(argv)
    try:
        if args.action == "resume":
            raise ValueError(_RESUME_HELP)
        if args.action == "cards":
            payload = read_cards()
        else:
            payload = _read({
                "card": lambda conn: service.company_card(conn, args.code),
                "reports": lambda conn: {"version": service.VERSION,
                                         "reports": [dict(row) for row in reading.api_reports(conn, args.code)]},
                "report": lambda conn: _report(conn, args.id, args.compact),
                "budget": lambda conn: {"version": service.VERSION, "budget": service.run_budget(conn)},
                "status": lambda conn: _run(conn, args.id),
            }[args.action])
    except sqlite3.Error as exc:
        print(f"Research 读取失败：{type(exc).__name__}；不会自动重提研究", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0
