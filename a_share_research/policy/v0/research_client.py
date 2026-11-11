"""Small research-v1 HTTP client; never opens the investment-state database."""

import argparse
import json
import re
import shlex
import sys
from urllib.parse import urlsplit

import requests

from a_share_research.policy.v0 import paths


def endpoint_url(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
    ):
        raise ValueError(
            "Research URL 须为 HTTPS（本机允许 HTTP），不能包含凭据、查询或片段"
        )
    return value.rstrip("/")


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
    start = actions.add_parser("start", help="发起研究或关联追问，返回任务编号")
    start.add_argument("code", type=_code)
    start.add_argument("--question", required=True)
    start.add_argument("--parent-report-id", type=_positive_id)
    feedback = actions.add_parser("feedback", help="针对报告段落发起核查，保留原报告")
    feedback.add_argument("id", type=_positive_id)
    feedback.add_argument("--location", required=True)
    feedback.add_argument("--text", required=True)
    for action in (start, feedback):
        action.add_argument(
            "--request-id", required=True, help="同一请求重试沿用同一编号"
        )


class ResearchNotConfigured(ValueError):
    """The optional Research connection has not been configured."""


def request_json(
    path: str, *, body=None, params=None, new_endpoint=False, timeout=(10, 30)
) -> dict:
    """One versioned request, with shared credential and transport safeguards."""
    base, token = paths.research_connection()
    if not base or not token:
        raise ResearchNotConfigured(
            "请配置 A_STOCK_RESEARCH_URL 和 A_STOCK_RESEARCH_TOKEN；不会自动读取其他项目的凭据"
        )
    base = endpoint_url(base)
    response = requests.request(
        "POST" if body is not None else "GET",
        base + path,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Research-Version": "research-v1",
        },
        params=params,
        json=body,
        timeout=timeout,
        allow_redirects=False,
    )
    if response.status_code == 401:
        raise ValueError(
            "Research HTTP 401：研究凭据无效、已撤销或临时令牌已过期；"
            "请由 Research owner 更新 A_STOCK_RESEARCH_TOKEN，检查环境变量是否覆盖 runtime.env。"
            "更新前不要重试；更新后沿用原 request-id，不要重复创建任务"
        )
    if not 200 <= response.status_code < 300:
        detail = ""
        try:
            failure = response.json()
        except ValueError:
            failure = None
        if (
            isinstance(failure, dict)
            and failure.get("version") == "research-v1"
            and isinstance(failure.get("error"), str)
        ):
            # Only the public error field, bounded and stripped of control characters/secrets.
            public_error = failure["error"].replace(token, "[REDACTED]")
            detail = (
                "："
                + "".join(char for char in public_error if char.isprintable())[:600]
            )
        elif response.status_code == 404 and new_endpoint:
            detail = "；服务可能尚未部署此读取接口，请核对配套服务版本"
        raise ValueError(
            f"Research HTTP {response.status_code}{detail}；不会自动重试或重开研究，勿换编号重复提交"
        )
    try:
        payload = response.json()
    except ValueError:
        raise ValueError("Research 响应不是有效 JSON") from None
    if not isinstance(payload, dict) or payload.get("version") != "research-v1":
        raise ValueError("Research 响应版本不兼容")
    return payload


def cmd_research(argv: list[str]) -> int:
    """通过 Research 服务复用报告、研究、追问及纠错；不修改账户或评分。"""
    parser = argparse.ArgumentParser(prog="a-stock-cache research")
    configure_parser(parser)
    args = parser.parse_args(argv)
    try:
        body, params = None, None
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
        elif args.action == "status":
            path = f"/api/runs/{args.id}"
        elif args.action == "start":
            path = "/api/research"
            body = {
                "code": args.code,
                "question": args.question,
                "request_id": args.request_id,
            }
            if args.parent_report_id is not None:
                body["parent_report_id"] = args.parent_report_id
        else:
            path = f"/api/reports/{args.id}/feedback"
            body = {
                "request_id": args.request_id,
                "location": args.location,
                "text": args.text,
            }
        payload = request_json(
            path,
            body=body,
            params=params,
            new_endpoint=args.action in {"resume", "budget", "cards", "card"}
            or args.action == "report"
            and args.compact,
        )
        if (
            args.action == "report"
            and args.compact
            and payload.get("view") != "compact"
        ):
            raise ValueError(
                "Research 服务尚未支持精简视图；需要原文时显式去掉 --compact，不自动重开研究"
            )
    except requests.RequestException as exc:
        print(
            f"Research 连接失败：{type(exc).__name__}；"
            + (
                f"提交结果可能未知。先只读找回任务：a-stock-cache research resume "
                f"--request-id {shlex.quote(args.request_id)}；勿换编号重复提交"
                if args.action in {"start", "feedback"}
                else "读取失败；不会自动重提研究"
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0
