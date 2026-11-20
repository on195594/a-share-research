"""Typed tool discovery and dispatch shared by in-process and HTTP callers."""
from __future__ import annotations

import base64
import binascii
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from requests import RequestException

from a_share_research import api_keys, db, scans, service
from a_share_research.account import trades, ledger, evidence, analysis, position_store, cycle_analysis
from a_share_research.account import nav
from a_share_research.policy import account_view, registry as policies
from a_share_research.policy.stop_state import StopState
from a_share_research.policy.position_request import Proposal
from a_share_research.policy.scenario_request import Proposal as ScenarioProposal


class ToolError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


class UnknownTool(ToolError):
    """Dispatch failed to find a tool, before any lookup of business data."""


def validation_message(errors) -> str:
    fields = []
    for error in errors[:8]:
        path = '.'.join(str(part) for part in error['loc']) or 'arguments'
        reason = error['msg'].removeprefix('Value error, ')
        fields.append(f'{path[:160]}: {reason[:240]}')
    return '；'.join(fields)[:1000]


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Company(Arguments):
    code: str = Field(pattern=r'^[0-9]{6}$')


class Report(Arguments):
    report_id: int = Field(gt=0)
    compact: bool = True


class MethodQuery(Arguments):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class ResearchSearch(Arguments):
    q: str = Field(max_length=100)
    code: str | None = Field(default=None, pattern=r'^[0-9]{6}$')
    section: Literal['risks', 'checkpoints', 'answer', 'business', 'financial', 'stance', 'prior_status'] | None = None
    history: bool = False
    page: int = Field(default=1, ge=1, le=10000)


class MonthlyBatch(Arguments):
    month: str = Field(pattern=r'^[0-9]{4}-(0[1-9]|1[0-2])$')


class MonthlyBatchStatus(MonthlyBatch):
    compact: bool = True


class ResearchStatus(Arguments):
    run_id: int | None = Field(default=None, gt=0, description='与 request_id 二选一')
    request_id: str | None = Field(default=None, min_length=1, max_length=128, description='与 run_id 二选一')
    compact: bool = True


class ReportFeedback(service.FeedbackRequest):
    report_id: int = Field(gt=0)


class ScanRequest(Arguments):
    request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')


class ScanView(Arguments):
    compact: bool = True


class ScanRead(ScanView):
    scan_id: int = Field(gt=0)


class SubscriptionCodes(Arguments):
    codes: list[str]


class PolicyGuide(Arguments):
    path: str | None = None


class PolicyEvaluation(Arguments):
    evaluation_id: int = Field(gt=0)


class PolicyMonitor(Arguments):
    request_id: str = Field(min_length=1, max_length=128)
    expected_activation_id: int = Field(gt=0)


class PolicyStopAlert(PolicyMonitor):
    previous_state: StopState


class PolicyBenchmark(PolicyMonitor):
    end: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class PolicyPositionSize(PolicyMonitor):
    monitor_evaluation_id: int = Field(gt=0)
    proposal: Proposal


class PolicyPortfolioScenario(PolicyMonitor):
    monitor_evaluation_id: int = Field(gt=0)
    proposal: ScenarioProposal


class Account(Arguments):
    account_id: str | None = Field(default=None, min_length=1, max_length=80)


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


class AccountNav(Account):
    request: nav.Request


class AccountNavReplay(Account):
    artifact_sha256: nav.Hash


class CycleAnalysis(Account):
    cycle_id: analysis.Text


class CycleAnalysisInitialize(CycleAnalysis):
    initialize: cycle_analysis.Initialize


class CycleAnalysisUpdate(CycleAnalysis):
    update: analysis.Update


class CycleAnalysisHistory(CycleAnalysis):
    after_id: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class EvidenceUpload(Arguments):
    content_base64: str = Field(max_length=28 * 1024 * 1024)


class RecentEvidence(Arguments):
    limit: int = Field(default=10, ge=1, le=100)


class Payload(BaseModel):
    data: dict


class TradeRows(BaseModel):
    rows: list[dict]


@dataclass(frozen=True)
class Context:
    connection: sqlite3.Connection
    database: Path
    principal: api_keys.Principal
    account_shadow_enabled: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    roles: frozenset[str]
    arguments: type[BaseModel]
    result: type[BaseModel]
    handler: Callable[[Context, BaseModel], BaseModel]

    access: Literal['read', 'write'] = 'read'
    account: bool = False
    collects: bool = False
    destructive: bool = False

    def describe(self) -> dict:
        return dict(name=self.name, version=1, description=self.description,
                    input_schema=self.arguments.model_json_schema(), output_schema=self.result.model_json_schema(),
                    access=self.access, collects=self.collects, idempotent=True,
                    errors=['invalid_arguments', 'forbidden', 'not_found', 'conflict', 'unavailable', 'timeout'])


def _request_scan(ctx: Context, args: ScanRequest) -> Payload:
    requester = service.requester(ctx.connection, ctx.principal)
    scan_id = scans.submit_request(ctx.connection, requester, args.request_id)
    receipt = scans.view(ctx.connection, scan_id, compact=True)
    return Payload(data=receipt | {'request_id': args.request_id, 'scan_id': scan_id})


def _research_status(ctx: Context, args: ResearchStatus) -> Payload:
    requester = service.requester(ctx.connection, ctx.principal) if args.request_id is not None else None
    view = service.research_status(ctx.connection, requester=requester, **args.model_dump())
    return Payload(data=view | service.run_link(view['id']))


def _queue_research(ctx: Context, args: service.ResearchRequest | ReportFeedback) -> Payload:
    submit = service.request_feedback if isinstance(args, ReportFeedback) else service.request_research
    receipt = submit(ctx.connection, service.requester(ctx.connection, ctx.principal), **args.model_dump())
    summary = service.run_summary(ctx.connection, receipt['run_id'], include_gaps=True)
    return Payload(data=summary | receipt | {'request_id': args.request_id})


def _monthly_batch(ctx: Context, month: str, *, submit: bool = False, compact: bool = True) -> Payload:
    data = service.monthly_batch(ctx.connection, month, submit=submit)
    batch = data['batch']
    if compact:
        del data['batch']
    data['queried_at'] = db.now()
    if batch is None:
        return Payload(data=data | {'batch_id': None, 'status': 'absent', 'path': '/batches',
                                    'gaps': ['该月尚无批次']})
    gaps = [error[:240] for error in (batch['error'], batch['ranking_error']) if error]
    if batch['failed_research']:
        gaps.append('候选研究失败：' + '、'.join(row['code'] for row in batch['failed_research']))
    data.update(batch_id=batch['id'], status=batch['status'], path=f"/batches/{batch['id']}", gaps=gaps,
                created_at=batch['created_at'], frozen_at=batch['frozen_at'], as_of=batch['as_of'])
    return Payload(data=data)


def _monitor(ctx: Context, args: PolicyMonitor) -> Payload:
    from a_share_research.policy.monitor import run
    return Payload(data=run(ctx.connection,ctx.principal,ctx.database,args.request_id,args.expected_activation_id))


def _current_account(ctx: Context, args: Arguments) -> Payload:
    return Payload(data=account_view.current(ctx.connection, ctx.principal, ctx.database))


def _stop_alert(ctx: Context, args: PolicyStopAlert) -> Payload:
    from a_share_research.policy.stop_alert import run
    return Payload(data=run(ctx.connection, ctx.principal, ctx.database, args.request_id,
                           args.expected_activation_id, args.previous_state.model_dump(mode='json', exclude_unset=True)))


def _daily_brief(ctx: Context, args: PolicyMonitor) -> Payload:
    from a_share_research.policy.daily_brief import run
    return Payload(data=run(ctx.connection, ctx.principal, ctx.database, args.request_id, args.expected_activation_id))


def _holdings_check(ctx: Context, args: PolicyMonitor) -> Payload:
    from a_share_research.policy.holdings_check import run
    return Payload(data=run(ctx.connection, ctx.principal, ctx.database, args.request_id, args.expected_activation_id))


def _benchmark(ctx: Context, args: PolicyBenchmark) -> Payload:
    from a_share_research.policy.benchmark import run
    return Payload(data=run(ctx.connection, ctx.principal, ctx.database, args.request_id, args.expected_activation_id, args.end))


def _position_size(ctx: Context, args: PolicyPositionSize) -> Payload:
    from a_share_research.policy.position_sizing import run
    return Payload(data=run(ctx.connection, ctx.principal, ctx.database, args.request_id, args.expected_activation_id,
                           args.monitor_evaluation_id, args.proposal.model_dump(mode='json')))


def _portfolio_scenario(ctx: Context, args: PolicyPortfolioScenario) -> Payload:
    from a_share_research.policy.portfolio_scenario import run
    return Payload(data=run(ctx.connection, ctx.principal, ctx.database, args.request_id, args.expected_activation_id,
                           args.monitor_evaluation_id, args.proposal.model_dump(mode='json')))


def _evidence_directory(ctx: Context) -> Path:
    return ctx.database.resolve().parent / 'account-evidence'


def _collect_nav_index(start: str, end: str) -> dict:
    from a_share_research.core.providers import TushareMarketDataProvider
    from a_share_research.policy.benchmark import _response

    response = _response(lambda: TushareMarketDataProvider().fetch_index_bars('H00906.CSI', start, end),
                         kind='index', source='tushare.index_daily')
    return dict(schema_version=1, code='H00906.CSI', currency='CNY', return_type='total_return',
                requested_dates=[start, end], **response)


def _store_evidence(ctx: Context, args: EvidenceUpload) -> Payload:
    try:
        content = base64.b64decode(args.content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise ToolError('invalid_arguments', '凭证须为有效base64内容', 422) from None
    return Payload(data={'sha256': evidence.store(_evidence_directory(ctx), content)})


def _recent_evidence(ctx: Context, args: RecentEvidence) -> Payload:
    try:
        items = evidence.recent(_evidence_directory(ctx), args.limit)
    except evidence.EvidenceError as exc:
        raise ToolError('unavailable', '账户证据暂不可用，请核对证据目录与文件权限', 503) from exc
    return Payload(data={'evidence': items})


def _research_coverage(ctx: Context, args: Arguments) -> TradeRows:
    ledger.authorize(ctx.connection, ctx.principal)
    return TradeRows(rows=trades.research_available_at_trade(ctx.connection))


def _research_methods(ctx: Context, args: MethodQuery) -> Payload:
    from a_share_research.research_tools import active_methods_digest, method_cards, research_methods

    root = os.environ.get('METHOD_CARDS_DIR')
    if not root or not Path(root).is_dir():
        return Payload(data=dict(cards=[], catalog_sha256=None, gaps=['方法卡目录未挂载，请检查 METHOD_CARDS_DIR']))
    catalog = method_cards(root)
    if not catalog:
        return Payload(data=dict(cards=[], catalog_sha256=None, gaps=['方法卡目录没有可用卡片，请检查已审核卡的 stable 状态']))
    return Payload(data=dict(cards=research_methods(ctx.connection, args.query, catalog=catalog),
                             catalog_sha256=active_methods_digest(ctx.connection, catalog=catalog), gaps=[]))


def _company_overview(ctx: Context, args: Company) -> Payload:
    from a_share_research.overview import company_overview
    ledger.authorize(ctx.connection, ctx.principal)
    return Payload(data=company_overview(ctx.connection, args.code))


def _portfolio_overview(ctx: Context, args: Arguments) -> Payload:
    from a_share_research.overview import portfolio_overview
    ledger.authorize(ctx.connection, ctx.principal)
    return Payload(data=portfolio_overview(ctx.connection))


_READERS = frozenset({'research', 'assistant'})
TOOLS = {tool.name: tool for tool in (
    Tool('research.company_overview', '读取公司研究、公告复核、本次持仓周期与已保存的监控判断',
         frozenset({'assistant'}), Company, Payload, _company_overview, account=True),
    Tool('account.portfolio_overview', '读取已核实持仓的研究、在仓论点、监控判断与待补资料',
         frozenset({'assistant'}), Arguments, Payload, _portfolio_overview, account=True),
    Tool('account.nav.capture', '冻结账户账本与估值出处，返回资料哈希；不认定资金流水完整',
         frozenset({'assistant'}), Account, Payload,
         lambda c,a: Payload(data=nav.capture(c.connection,c.principal,a.account_id)), account=True),
    Tool('account.nav.evaluate', '按完整资金声明、明确流前估值保存净值对照；省略指数证据时采集 H00906 收盘值',
         frozenset({'assistant'}), AccountNav, Payload,
         lambda c,a: Payload(data=nav.evaluate(c.connection,c.principal,a.account_id,a.request,
                                               _evidence_directory(c), collect_index=_collect_nav_index)),
         access='write', account=True, collects=True),
    Tool('account.nav.replay', '按哈希重放保存的净值输入与结果；不代表当前账户状态或券商认证',
         frozenset({'assistant'}), AccountNavReplay, Payload,
         lambda c,a: Payload(data=nav.replay(c.connection,c.principal,a.account_id,a.artifact_sha256,_evidence_directory(c))),
         account=True),
    Tool('policy.holdings_check', '采集并保存原持仓检查和已有流动性延迟复核证据；保留市场数据缺口，不执行交易',
         frozenset({'assistant'}), PolicyMonitor, Payload, _holdings_check, access='write', account=True, collects=True),
    Tool('policy.portfolio_scenario', '引用已保存监控评估，按调用者指定的卖出数量和费用保存静态风险情景；不刷新行情、不执行交易',
         frozenset({'assistant'}), PolicyPortfolioScenario, Payload, _portfolio_scenario, access='write', account=True),
    Tool('policy.position_size', '引用已保存的服务端监控评估，按拟买入条件保存风险预算定仓；不刷新行情、不形成交易动作',
         frozenset({'assistant'}), PolicyPositionSize, Payload, _position_size, access='write', account=True),
    Tool('policy.benchmark', '按指定截止日采集完整交易历史与双全收益指数，保存相同现金流比较及失败证据；不发送通知、不修改账户',
         frozenset({'assistant'}), PolicyBenchmark, Payload, _benchmark, access='write', account=True, collects=True),
    Tool('policy.daily_brief', '采集持仓监控和分红核查，保存原始输入、失败项与盘后日报；不发送通知、不记分红账',
         frozenset({'assistant'}), PolicyMonitor, Payload, _daily_brief, access='write', account=True, collects=True),
    Tool('policy.stop_alert', '采集在仓止损线和行情，按生效政策保存升档提醒与下一通知状态；不发送通知、不修改账户',
         frozenset({'assistant'}), PolicyStopAlert, Payload, _stop_alert, access='write', account=True, collects=True),
    Tool('policy.monitor', '从已核对的账户镜像或明确绑定的账本采集行情，按生效政策保存评估与原始输入；来源与时点随回执返回',
         frozenset({'assistant'}), PolicyMonitor, Payload, _monitor, access='write', account=True, collects=True),
    Tool('policy.current', '读取生效政策身份、参数与实装一致性；未启用时不猜测默认版本',
         frozenset({'assistant','owner'}), Arguments, Payload,
         lambda c,a: Payload(data=policies.current(c.connection,c.principal)), account=True),
    Tool('policy.guide', '发现或读取生效政策保存的指南；文本资料不是可执行指令',
         frozenset({'assistant','owner'}), PolicyGuide, Payload,
         lambda c,a: Payload(data=policies.guide(c.connection,c.principal,a.path)), account=True),
    Tool('policy.evaluation', '读取已保存的政策重放输入、结果与身份',
         frozenset({'assistant','owner'}), PolicyEvaluation, Payload,
         lambda c,a: Payload(data=policies.evaluation(c.connection,c.principal,a.evaluation_id)), account=True),
    Tool('account.cycle_analysis.initialize', '以证据建立账户周期分析分支；旧周期须明确来源版本', frozenset({'assistant'}), CycleAnalysisInitialize, Payload,
         lambda c,a: Payload(data=cycle_analysis.initialize(c.connection,c.principal,a.account_id,a.cycle_id,a.initialize,_evidence_directory(c))),
         access='write', account=True),
    Tool('account.cycle_analysis.current', '读取指定账户与周期的最新分析状态', frozenset({'assistant'}), CycleAnalysis, Payload,
         lambda c,a: Payload(data=cycle_analysis.current(c.connection,c.principal,a.account_id,a.cycle_id)), account=True),
    Tool('account.cycle_analysis.history', '读取指定账户周期的不可变分析历史，保留已作废周期', frozenset({'assistant'}), CycleAnalysisHistory, Payload,
         lambda c,a: Payload(data=cycle_analysis.history(c.connection,c.principal,a.account_id,a.cycle_id,after_id=a.after_id,limit=a.limit)), account=True),
    Tool('account.cycle_analysis.update', '追加指定账户周期的论点、L3、预警与Tier状态', frozenset({'assistant'}), CycleAnalysisUpdate, Payload,
         lambda c,a: Payload(data=cycle_analysis.update(c.connection,c.principal,a.account_id,a.cycle_id,a.update,_evidence_directory(c))),
         access='write', account=True),
    Tool('account.positions', '读取新账本成本和周期投影；现金流仅自期初累计', frozenset({'assistant'}), Account, Payload,
         lambda c, a: Payload(data=position_store.state(c.connection, c.principal, a.account_id)), account=True),
    Tool('account.current', '读取已核对镜像或明确绑定账本的在仓公司代码与来源；不猜测账户、不采集行情',
         frozenset({'assistant'}), Arguments, Payload, _current_account, account=True),
    Tool('account.evidence', '私有存储截图或交割单，返回内容哈希', frozenset({'assistant'}), EvidenceUpload, Payload,
         _store_evidence, access='write', account=True),
    Tool('account.evidence.recent', '按文件保存时间列出最近账户凭证的 SHA-256、时间和字节数',
         frozenset({'assistant'}), RecentEvidence, Payload, _recent_evidence, account=True),
    Tool('account.state', '查询账户账本版本、股数与现金余额', frozenset({'assistant'}), Account, Payload,
         lambda c,a: Payload(data=ledger.state(c.connection,c.principal,a.account_id)), account=True),
    Tool('account.submit', '完整对账后追加账户流水与回执，支持作废并提交更正流水', frozenset({'assistant'}), AccountSubmit, Payload,
         lambda c,a: Payload(data=ledger.submit(c.connection,c.principal,a.account_id,a.batch,_evidence_directory(c))),
         access='write', account=True, destructive=True),
    Tool('account.events', '分页读取原事件与作废状态，按事件编号更正', frozenset({'assistant'}), AccountEvents, Payload,
         lambda c,a: Payload(data=ledger.events(c.connection,c.principal,a.account_id,after_id=a.after_id,limit=a.limit)), account=True),
    Tool('account.receipt', '查阅对账回执', frozenset({'assistant'}), AccountReceipt, Payload,
         lambda c,a: Payload(data=ledger.receipt(c.connection,c.principal,a.account_id,a.receipt_id)), account=True),
    Tool('account.observe', '仅记录资产观察，不推断或补写成交', frozenset({'assistant'}), AccountObserve, Payload,
         lambda c,a: Payload(data=ledger.observe(c.connection,c.principal,a.account_id,a.observation,
                                                _evidence_directory(c),a.evidence_hash)), access='write', account=True),
    Tool('research.search', '按公司或内容检索已有报告，返回匹配段落摘要和链接', _READERS, ResearchSearch, Payload,
         lambda c, a: Payload(data=service.research_search(c.connection, **a.model_dump(), library=True))),
    Tool('research.cards', '读取论点卡', _READERS, Arguments, Payload,
         lambda c, a: Payload(data=service.company_cards(c.connection))),
    Tool('research.request', '提交单股研究，立即返回任务编号、状态和进度页', _READERS,
         service.ResearchRequest, Payload, _queue_research, access='write', collects=True),
    Tool('research.status', '按任务编号或原凭据的请求编号查询研究状态，compact=false 展开诊断与预算', _READERS,
         ResearchStatus, Payload, _research_status),
    Tool('research.feedback', '按报告段落提交纠错，返回核查任务编号、状态和进度页', _READERS,
         ReportFeedback, Payload, _queue_research, access='write'),
    Tool('research.card', '读取指定公司论点卡', _READERS, Company, Payload,
         lambda c, a: Payload(data=service.company_card(c.connection, a.code))),
    Tool('research.methods', '按空格分隔的关键词检索最多五张已审核方法卡，返回标题、含适用条件的正文、来源链接与目录哈希',
         _READERS, MethodQuery, Payload, _research_methods),
    Tool('research.report', '读取报告及证据', _READERS, Report, Payload,
         lambda c, a: Payload(data=service.report_view(c.connection, a.report_id, compact=a.compact))),
    Tool('research.scan.request', '按原凭据与请求编号提交或找回扫描，采集由 worker 执行', frozenset({'assistant'}),
         ScanRequest, Payload, _request_scan, access='write', account=True),
    Tool('research.scan.read', '读取扫描状态、事件数和缺口，compact=false 展开事件及通知', frozenset({'assistant'}),
         ScanRead, Payload, lambda c, a: Payload(data=scans.view(c.connection, a.scan_id, compact=a.compact)), account=True),
    Tool('research.scan.weekly', '读取近七日扫描分布和覆盖缺口，compact=false 展开周报', frozenset({'assistant'}),
         ScanView, Payload, lambda c, a: Payload(data=scans.weekly(c.connection, compact=a.compact, account_sources=True)),
         account=True),
    Tool('research.subscriptions', '读取显式订阅和冻结候选及最近收盘时点', frozenset({'assistant'}), Arguments, Payload,
         lambda c, a: Payload(data=scans.subscription_view(scans.subscriptions(c.connection))), account=True),
    Tool('research.subscriptions.replace', '按公司代码完整替换外部订阅，提交空列表清空外部订阅', frozenset({'assistant'}),
         SubscriptionCodes, Payload, lambda c,a: Payload(data=scans.subscription_view(
             scans.replace_subscriptions(c.connection, a.codes))), access='write', collects=True, account=True,
         destructive=True),
    Tool('research.batch.monthly.request', '受理上海当月的候选批次，已有批次返回原编号和状态',
         _READERS, MonthlyBatch, Payload, lambda c, a: _monthly_batch(c, a.month, submit=True), access='write'),
    Tool('research.batch.monthly.status', '按月查询候选批次状态和缺口，可展开冻结名单与失败详情',
         _READERS, MonthlyBatchStatus, Payload, lambda c, a: _monthly_batch(c, a.month, compact=a.compact)),
    Tool('account.research_coverage', '分开读取成交明确采用的报告与成交时可查的报告',
         frozenset({'assistant'}), Arguments, TradeRows, _research_coverage, account=True),
)}


def visible_tools(principal: api_keys.Principal, *, account_shadow_enabled: bool = False) -> list[Tool]:
    return [tool for tool in TOOLS.values()
            if principal.role in tool.roles and (not tool.account or account_shadow_enabled)]


def discover(principal: api_keys.Principal, *, account_shadow_enabled: bool = False) -> list[dict]:
    return [tool.describe() for tool in visible_tools(principal, account_shadow_enabled=account_shadow_enabled)]


def authorize_tool(ctx: Context, tool: Tool | None) -> Tool:
    if tool is None or (tool.account and not ctx.account_shadow_enabled):
        raise UnknownTool('not_found', '工具不存在', 404)
    if ctx.principal.role not in tool.roles:
        raise ToolError('forbidden', '此凭据不能调用该工具', 403)
    # Rechecked on each connection the call uses, so a grant revoked mid-request stops at the write.
    if not api_keys.is_active(ctx.connection, ctx.principal.key_id, ctx.principal.role):
        raise ToolError('forbidden', '凭据已撤销或角色已变更', 403)
    return tool


def invoke(ctx: Context, name: str, arguments: dict) -> dict:
    """Reject scope/shape errors before invoking a handler; never upgrades caller privileges."""
    tool = authorize_tool(ctx, TOOLS.get(name))
    try:
        parsed = tool.arguments.model_validate(arguments)
    except ValidationError as exc:
        raise ToolError('invalid_arguments', validation_message(exc.errors(include_input=False)), 422) from None
    try:
        if isinstance(parsed, Account):
            ledger.authorize(ctx.connection, ctx.principal)
            account_id = account_view.bound_account_id(ctx.connection)
            if parsed.account_id is not None and parsed.account_id != account_id:
                raise ToolError('conflict', 'account_id 与当前绑定账户不一致，请核对账户或省略此字段', 409)
            parsed.account_id = account_id
        return tool.result.model_validate(tool.handler(ctx, parsed)).model_dump(mode='json')
    except (service.RequestError, ledger.LedgerError, policies.PolicyError) as exc:
        code = {400: 'invalid_arguments', 403: 'forbidden', 404: 'not_found', 409: 'conflict',
                422: 'invalid_arguments', 503: 'unavailable', 504: 'timeout'}[exc.status]
        if isinstance(exc, policies.PolicyError) and exc.status == 409 and exc.code == 'policy_activation_changed':
            code = exc.code
        raise ToolError(code, str(exc), exc.status) from None
    except evidence.EvidenceError as exc:
        raise ToolError('invalid_arguments', str(exc), 422) from None
    except RequestException:
        raise ToolError('unavailable', '上游服务暂不可用，请稍后用原请求重试', 503) from None
