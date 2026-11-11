"""Reads research results in process from the application database; touches no account table."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

from a_share_research import reading, service
from a_share_research.policy.v0 import paths


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
    resume = actions.add_parser(
        "resume", help="凭原请求编号只读找回任务；不重提、不付费重跑"
    )
    resume.add_argument("--request-id", required=True)
    # Explicit opt-in preserves the existing full report wire contract.
    actions.choices["report"].add_argument("--compact", action="store_true")


class ResearchNotConfigured(ValueError):
    """There is no application database to read research from."""


def _run(conn, match, params) -> dict:
    row = reading.api_run(conn, int(match[1]))
    if row is None:
        raise service.RequestError("任务不存在", 404)
    return {"version": service.VERSION, **dict(row)}


def _reports(conn, match, params) -> dict:
    return {"version": service.VERSION,
            "reports": [dict(row) for row in reading.api_reports(conn, params["code"])]}


# The read routes of the research-v1 interface, answered by the functions the web service itself calls.
_ROUTES = (
    (re.compile(r"/api/cards"), lambda conn, match, params: service.company_cards(conn)),
    (re.compile(r"/api/companies/([0-9]{6})/card"), lambda conn, match, params: service.company_card(conn, match[1])),
    (re.compile(r"/api/reports"), _reports),
    (re.compile(r"/api/reports/([0-9]+)"), lambda conn, match, params: service.report_view(
        conn, int(match[1]), compact=params.get("compact") == "true")),
    (re.compile(r"/api/request"), lambda conn, match, params: service.run_view(conn, request_id=params["request_id"])),
    (re.compile(r"/api/research/budget"),
     lambda conn, match, params: {"version": service.VERSION, "budget": service.run_budget(conn)}),
    (re.compile(r"/api/runs/([0-9]+)"), _run),
)


def _read(path: str, params: dict) -> dict:
    database = paths.cache_db_path()
    if not database.exists():
        raise ResearchNotConfigured(f"研究库不存在：{database}")
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        for pattern, handler in _ROUTES:
            match = pattern.fullmatch(path)
            if match:
                return handler(conn, match, params)
        raise ValueError(f"政策运行时只读取研究结果，没有 {path} 这项读取")
    except service.RequestError as exc:
        raise ValueError(f"Research {exc.status}：{exc}") from None
    finally:
        conn.close()


def request_json(path: str, *, params: dict | None = None) -> dict:
    """One read of the research-v1 interface; raises ValueError with a message that is safe to show."""
    payload = _read(path, params or {})
    if not isinstance(payload, dict) or payload.get("version") != "research-v1":
        raise ValueError("Research 响应版本不兼容")
    return payload


def cmd_research(argv: list[str]) -> int:
    """读取研究报告、论点卡与任务状态；不发起研究，不修改账户或评分。"""
    parser = argparse.ArgumentParser(prog="a-stock-cache research")
    configure_parser(parser)
    args = parser.parse_args(argv)
    try:
        params = None
        if args.action == "cards":
            path = "/api/cards"
        elif args.action == "card":
            path = f"/api/companies/{args.code}/card"
        elif args.action == "reports":
            path, params = "/api/reports", {"code": args.code}
        elif args.action == "report":
            path = f"/api/reports/{args.id}"
            if args.compact:
                params = {"compact": "true"}
        elif args.action == "resume":
            path, params = "/api/request", {"request_id": args.request_id}
        elif args.action == "budget":
            path = "/api/research/budget"
        else:
            path = f"/api/runs/{args.id}"
        payload = request_json(path, params=params)
    except sqlite3.Error as exc:
        print(f"Research 读取失败：{type(exc).__name__}；不会自动重提研究", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0
