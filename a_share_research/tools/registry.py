"""Typed tool discovery and dispatch shared by in-process and HTTP callers."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from a_share_research import api_keys, service
from a_share_research.account import trades


class ToolError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Company(Arguments):
    code: str = Field(pattern=r'^[0-9]{6}$')


class Report(Arguments):
    report_id: int = Field(gt=0)
    compact: bool = True


class PolicyRead(Arguments):
    command: Literal['holdings', 'retro-pending', 'retro-stats', 'watchlist', 'list']


class Payload(BaseModel):
    data: dict


class TradeRows(BaseModel):
    rows: list[dict]


class CommandResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Context:
    connection: sqlite3.Connection
    database: Path
    principal: api_keys.Principal


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    roles: frozenset[str]
    arguments: type[Arguments]
    result: type[BaseModel]
    handler: Callable[[Context, Arguments], BaseModel]

    def describe(self) -> dict:
        return dict(name=self.name, version=1, description=self.description,
                    input_schema=self.arguments.model_json_schema(), output_schema=self.result.model_json_schema(),
                    access='read', idempotent=True,
                    errors=['invalid_arguments', 'forbidden', 'not_found', 'unavailable', 'timeout'])


def _policy(ctx: Context, args: PolicyRead) -> CommandResult:
    # Explicit R0 allowlist: no caller-controlled argv, environment or shell.
    argv = [sys.executable, '-m', 'a_share_research.policy.v0.cache', args.command]
    if args.command == 'holdings':
        argv.append('--json')
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'LANG', 'LC_ALL')}
    env['POLICY_V0_DB'] = str(ctx.database.resolve())
    try:
        result = subprocess.run(argv, env=env, cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolError('timeout', '政策读取超时；未重试', 504) from None
    return CommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)


_READERS = frozenset({'research', 'assistant'})
TOOLS = {tool.name: tool for tool in (
    Tool('research.cards', '读取论点卡', _READERS, Arguments, Payload,
         lambda c, a: Payload(data=service.company_cards(c.connection))),
    Tool('research.card', '读取指定公司论点卡', _READERS, Company, Payload,
         lambda c, a: Payload(data=service.company_card(c.connection, a.code))),
    Tool('research.report', '读取报告及证据', _READERS, Report, Payload,
         lambda c, a: Payload(data=service.report_view(c.connection, a.report_id, compact=a.compact))),
    Tool('account.research_coverage', '成交时可用研究；不作为显式依据或收益归因',
         frozenset({'assistant'}), Arguments, TradeRows,
         lambda c, a: TradeRows(rows=trades.research_available_at_trade(c.connection))),
    Tool('policy.v0.read', '隔离子进程读取旧政策结果，保留输出和退出码',
         frozenset({'assistant'}), PolicyRead, CommandResult, _policy),
)}


def discover(principal: api_keys.Principal) -> list[dict]:
    return [tool.describe() for tool in TOOLS.values() if principal.role in tool.roles]


def invoke(ctx: Context, name: str, arguments: dict) -> dict:
    """Reject scope/shape errors before invoking a handler; never upgrades caller privileges."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError('not_found', '工具不存在', 404)
    if ctx.principal.role not in tool.roles:
        raise ToolError('forbidden', '此凭据不能调用该工具', 403)
    try:
        parsed = tool.arguments.model_validate(arguments)
    except ValidationError:
        raise ToolError('invalid_arguments', '参数不符合工具 input_schema', 422) from None
    try:
        return tool.result.model_validate(tool.handler(ctx, parsed)).model_dump(mode='json')
    except service.RequestError as exc:
        raise ToolError('not_found' if exc.status == 404 else 'invalid_arguments', str(exc), exc.status) from None
