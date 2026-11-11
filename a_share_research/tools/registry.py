"""Typed tool discovery and dispatch shared by in-process and HTTP callers."""
from __future__ import annotations

import os
import base64
import binascii
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from a_share_research import api_keys, service, external_research, external_review
from a_share_research.account import trades, ledger, evidence, analysis, analysis_rebase


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


class ExternalStart(Arguments):
    request_id: str = Field(min_length=1, max_length=128)
    source_run_id: int = Field(gt=0)


class ExternalId(Arguments):
    external_id: int = Field(gt=0)


class ExternalReview(Arguments):
    submission_id: int = Field(gt=0)


class ExternalSubmit(ExternalId):
    expected_version: int = Field(ge=0)
    draft: dict


class Account(Arguments):
    account_id: str = Field(min_length=1, max_length=80)


class AccountSubmit(Account):
    batch: ledger.Batch


class AccountEvents(Account):
    after_id: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class AccountReceipt(Account):
    receipt_id: int = Field(gt=0)


class AccountObserve(Account):
    observation: ledger.Observation
    evidence_hash: str


class Analysis(Arguments):
    holding_id: int = Field(gt=0)


class AnalysisHistory(Analysis):
    after_id: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class AnalysisUpdate(Analysis):
    update: analysis.Update


class AnalysisRebase(Arguments):
    rebase: analysis_rebase.Rebase


class EvidenceUpload(Arguments):
    content_base64: str = Field(max_length=28 * 1024 * 1024)


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
    external_enabled: bool = False
    external_review_enabled: bool = False
    account_shadow_enabled: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    roles: frozenset[str]
    arguments: type[Arguments]
    result: type[BaseModel]
    handler: Callable[[Context, Arguments], BaseModel]

    access: str = 'read'
    external: bool = False
    account: bool = False

    def describe(self) -> dict:
        return dict(name=self.name, version=1, description=self.description,
                    input_schema=self.arguments.model_json_schema(), output_schema=self.result.model_json_schema(),
                    access=self.access, idempotent=True,
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


def _lifecycle(ctx: Context, args: Analysis) -> Payload:
    from a_share_research.tools.lifecycle import read
    return Payload(data=read(ctx.connection, ctx.principal, args.holding_id))


def _evidence_directory(ctx: Context) -> Path:
    return ctx.database.resolve().parent / 'account-evidence'


def _store_evidence(ctx: Context, args: EvidenceUpload) -> Payload:
    try:
        content = base64.b64decode(args.content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise ToolError('invalid_arguments', '凭证须为有效base64内容', 422) from None
    return Payload(data={'sha256': evidence.store(_evidence_directory(ctx), content)})


_READERS = frozenset({'research', 'assistant'})
TOOLS = {tool.name: tool for tool in (
    Tool('account.lifecycle.replay', '只读重放旧持仓周期成本和政策字段，保留推断与历史差异', frozenset({'assistant'}), Analysis, Payload,
         _lifecycle, account=True),
    Tool('account.analysis.preview', '对照旧基线、当前镜像与影子结果，取得协调版本和哈希', frozenset({'assistant'}), Analysis, Payload,
         lambda c, a: Payload(data=analysis_rebase.preview(c.connection, c.principal, a.holding_id)), account=True),
    Tool('account.analysis.rebase', '显式选择镜像或影子结果，原子追加基线协调历史', frozenset({'assistant'}), AnalysisRebase, Payload,
         lambda c, a: Payload(data=analysis_rebase.rebase(c.connection, c.principal, a.rebase,
                              c.database.parent / 'account-evidence')), access='write', account=True),
    Tool('account.analysis.current', '读取持仓周期最新影子分析状态', frozenset({'assistant'}), Analysis, Payload,
         lambda c, a: Payload(data=analysis.current(c.connection, c.principal, a.holding_id)), account=True),
    Tool('account.analysis.history', '分页读取不可变分析历史，旧基线不冒充完整历史', frozenset({'assistant'}), AnalysisHistory, Payload,
         lambda c, a: Payload(data=analysis.history(c.connection, c.principal, a.holding_id,
                              after_id=a.after_id, limit=a.limit)), account=True),
    Tool('account.analysis.update', '带证据追加论点/L3整组重写、预警与Tier状态；不修改旧监控来源', frozenset({'assistant'}), AnalysisUpdate, Payload,
         lambda c, a: Payload(data=analysis.update(c.connection, c.principal, a.holding_id, a.update,
                              c.database.parent / 'account-evidence')), access='write', account=True),
    Tool('account.evidence', '私有存储截图或交割单，返回内容哈希', frozenset({'assistant'}), EvidenceUpload, Payload,
         _store_evidence, access='write', account=True),
    Tool('account.state', '查询影子账本版本与股数现金投影', frozenset({'assistant'}), Account, Payload,
         lambda c,a: Payload(data=ledger.state(c.connection,c.principal,a.account_id)), account=True),
    Tool('account.submit', '完整对账后原子写入影子账本；不修改生产持仓', frozenset({'assistant'}), AccountSubmit, Payload,
         lambda c,a: Payload(data=ledger.submit(c.connection,c.principal,a.account_id,a.batch,_evidence_directory(c))),
         access='write', account=True),
    Tool('account.events', '分页读取原事件与作废状态，按事件编号更正', frozenset({'assistant'}), AccountEvents, Payload,
         lambda c,a: Payload(data=ledger.events(c.connection,c.principal,a.account_id,after_id=a.after_id,limit=a.limit)), account=True),
    Tool('account.receipt', '查阅对账回执', frozenset({'assistant'}), AccountReceipt, Payload,
         lambda c,a: Payload(data=ledger.receipt(c.connection,c.principal,a.account_id,a.receipt_id)), account=True),
    Tool('account.observe', '仅记录资产观察，不推断或补写成交', frozenset({'assistant'}), AccountObserve, Payload,
         lambda c,a: Payload(data=ledger.observe(c.connection,c.principal,a.account_id,a.observation,
                                                _evidence_directory(c),a.evidence_hash)), access='write', account=True),
    Tool('external.review', '将通过机械检查的本人提交交独立复核', _READERS, ExternalReview, Payload,
         lambda c, a: Payload(data=external_review.enqueue(c.connection, c.principal, a.submission_id,
                                                           allow=c.external_review_enabled)),
         access='write', external=True),
    Tool('external.start', '复用服务端材料开启外部研究；不排队、不发布', _READERS, ExternalStart, Payload,
         lambda c, a: Payload(data=external_research.start(c.connection, c.principal, a.request_id, a.source_run_id)),
         access='write', external=True),
    Tool('external.status', '查询本人研究及不可变提交回执', _READERS, ExternalId, Payload,
         lambda c, a: Payload(data=external_research.status(c.connection, c.principal, a.external_id)), external=True),
    Tool('external.material', '读取本人研究绑定的服务端材料', _READERS, ExternalId, Payload,
         lambda c, a: Payload(data=external_research.material_snapshot(c.connection, c.principal, a.external_id)), external=True),
    Tool('external.submit', '机械检查并冻结草稿；通过不代表独立复核通过', _READERS, ExternalSubmit, Payload,
         lambda c, a: Payload(data=external_research.submit(c.connection, c.principal, a.external_id,
                                                          a.expected_version, a.draft)), access='write', external=True),
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


def discover(principal: api_keys.Principal, *, external_enabled: bool = False,
             account_shadow_enabled: bool = False) -> list[dict]:
    return [tool.describe() for tool in TOOLS.values()
            if principal.role in tool.roles and (not tool.external or external_enabled)
            and (not tool.account or account_shadow_enabled)]


def invoke(ctx: Context, name: str, arguments: dict) -> dict:
    """Reject scope/shape errors before invoking a handler; never upgrades caller privileges."""
    tool = TOOLS.get(name)
    if tool is None or (tool.external and not ctx.external_enabled) or (tool.account and not ctx.account_shadow_enabled):
        raise ToolError('not_found', '工具不存在', 404)
    if ctx.principal.role not in tool.roles:
        raise ToolError('forbidden', '此凭据不能调用该工具', 403)
    try:
        parsed = tool.arguments.model_validate(arguments)
    except ValidationError:
        raise ToolError('invalid_arguments', '参数不符合工具 input_schema', 422) from None
    try:
        return tool.result.model_validate(tool.handler(ctx, parsed)).model_dump(mode='json')
    except ledger.LedgerError as exc:
        code = {403: 'forbidden', 404: 'not_found', 409: 'conflict', 422: 'invalid_arguments'}[exc.status]
        raise ToolError(code, str(exc), exc.status) from None
    except evidence.EvidenceError as exc:
        raise ToolError('invalid_arguments', str(exc), 422) from None
    except external_research.SubmissionError as exc:
        code = {403: 'forbidden', 404: 'not_found', 409: 'conflict', 422: 'invalid_arguments'}[exc.status]
        raise ToolError(code, str(exc), exc.status) from None
    except service.RequestError as exc:
        raise ToolError('not_found' if exc.status == 404 else 'invalid_arguments', str(exc), exc.status) from None
