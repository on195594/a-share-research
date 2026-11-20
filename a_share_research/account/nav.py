"""Evidence-bound NAV replay, independent of investment policies and live market access."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator

from a_share_research import db, gate
from a_share_research.account import evidence, ledger, nav_refs
from a_share_research.account.reconciliation import (
    Event, Money, Opening, Value, ValuationTime, identity, project, valuation_time,
)
from a_share_research.core import account_nav
from a_share_research.core.market_data import EMPTY_RESPONSE

Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Id = Annotated[StrictInt, Field(gt=0)]
Reason = Annotated[str, Field(min_length=1, max_length=2000, pattern=r'\S')]


class ValuationRef(Value):
    kind: Literal['observation', 'batch']
    id: Id
    reason: Reason  # Explicit selection, including how conflicting observations were resolved.


class FlowRef(Value):
    event_id: Id
    before: ValuationRef


class Coverage(Value):
    start: ValuationTime
    end: ValuationTime
    currency: Literal['CNY']
    scope: Literal['cash_equity_no_liabilities']
    complete_external_flows: StrictBool
    no_security_transfers: StrictBool
    evidence_hashes: list[Hash] = Field(min_length=1)
    explanation: Reason


class Request(Value):
    expected_state_sha256: Hash
    start: ValuationRef
    end: ValuationRef
    flows: list[FlowRef]
    coverage: Coverage
    benchmark_evidence_hash: Hash | None = None


class IndexRow(Value):
    date: date
    close: Annotated[Money, Field(gt=0)]

    @field_validator('date', mode='before')
    @classmethod
    def calendar_day(cls, value):
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError('指数日期须为 YYYY-MM-DD')
        return value


class IndexEvidence(Value):
    schema_version: Literal[1]
    code: Literal['H00906.CSI']
    currency: Literal['CNY']
    return_type: Literal['total_return']
    source: Literal['tushare.index_daily']
    fetched_at: ValuationTime
    source_as_of: str
    status: Literal['ok']
    rows: list[IndexRow] = Field(max_length=10000)
    # Server captures retain the request interval and the provider's response metadata.
    requested_dates: tuple[date, date] | None = None
    error_code: None = None
    error_type: None = None
    fallback_reason: None = None
    freshness_days: StrictInt | None = None

    @field_validator('schema_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError('指数 schema_version 须为整数 1')
        return value

    @field_validator('source_as_of')
    @classmethod
    def calendar_day(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError('指数截至日须为 YYYY-MM-DD')
        return value


def _json(raw, *, decimal_numbers=False):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('JSON 字段重复')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('JSON 含非有限数字')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid,
                      parse_float=Decimal if decimal_numbers else float)


def _source(conn, account_id):
    book = conn.execute('SELECT * FROM account_books WHERE account_id=?', (account_id,)).fetchone()
    if book is None:
        raise ledger.LedgerError('账户期初不存在', 404)
    result = {'account_id': account_id, 'book': dict(book)}
    for table in ('account_batches', 'account_journal', 'account_observations'):
        condition = " AND status='posted'" if table == 'account_batches' else ''
        result[table] = [dict(row) for row in conn.execute(
            f'SELECT * FROM {table} WHERE account_id=?{condition} ORDER BY id', (account_id,))]
    result['account_voids'] = [dict(row) for row in conn.execute(
        'SELECT v.* FROM account_voids v JOIN account_journal j ON j.id=v.event_id '
        'WHERE j.account_id=? ORDER BY v.event_id', (account_id,))]
    _active(result)
    return result


def _active(source):
    """Check captured journal ownership, voids and projection before choosing cash flows."""
    account = source['account_id']
    book = source['book']
    batches = {r['id']: r for r in source['account_batches']}
    journals = {r['id']: r for r in source['account_journal']}
    for table, key in (('account_batches', 'id'), ('account_journal', 'id'),
                       ('account_observations', 'id'), ('account_voids', 'event_id')):
        ids = [r[key] for r in source[table]]
        if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('净值输入的记录编号无效或重复')
    if book['account_id'] != account:
        raise ValueError('净值输入的账户归属不一致')
    for batch in batches.values():
        if (batch['account_id'] != account or batch['status'] != 'posted'
                or gate.digest(_json(batch['request'])) != batch['request_hash']):
            raise ValueError('净值输入的入账批次不一致')
    for row in source['account_observations']:
        if (row['account_id'] != account or gate.digest({'observation': _json(row['observation']),
                'evidence_hash': row['evidence']}) != row['request_hash']):
            raise ValueError('净值输入的资产观察不一致')
    events = {}
    for key, row in journals.items():
        event = Event.model_validate(_json(row['event']))
        batch = batches.get(row['batch_id'])
        if (row['account_id'] != account or batch is None or row['business_id'] != identity(event, account)
                or row['company_key'] != event.code or row['occurred_at'] != event.occurred_at.isoformat()
                or event.occurred_at < valuation_time(book['opened_at'])):
            raise ValueError('净值输入的事件归属或身份不一致')
        if event.model_dump(mode='json') not in _json(batch['request'])['events']:
            raise ValueError('净值事件不在原入账请求中')
        events[key] = event
    voids = set()
    for row in source['account_voids']:
        original, batch = journals.get(row['event_id']), batches.get(row['batch_id'])
        if (original is None or batch is None or row['batch_id'] <= original['batch_id']
                or row['event_id'] not in _json(batch['request'])['void_event_ids']):
            raise ValueError('净值输入的作废关系不一致')
        voids.add(row['event_id'])
    active = {key: event for key, event in events.items() if key not in voids}
    if len({identity(e, account) for e in active.values()}) != len(active):
        raise ValueError('净值输入的有效事件身份重复')
    if project(Opening.model_validate_json(book['opening']), list(active.values())) != Opening.model_validate_json(book['projection']):
        raise ValueError('净值输入与账本资金投影不一致')
    versions = {r['batch_id'] for r in journals.values()} | {r['batch_id'] for r in source['account_voids']}
    if book['version'] != len(versions):
        raise ValueError('净值输入与账本版本不一致')
    return active


def capture(conn, principal, account_id: str) -> dict:
    """Freeze rows in one read transaction; version alone does not cover observations."""
    try:
        with db.transaction(conn, immediate=False):
            ledger.authorize(conn, principal)
            source = _source(conn, ledger._account(account_id))
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, ledger.LedgerError):
            raise
        raise ledger.LedgerError(str(exc), 422) from None
    return {'source': source, 'state_sha256': gate.digest(source)}


def _valuation(source, ref, directory):
    table = 'account_observations' if ref.kind == 'observation' else 'account_batches'
    row = next((r for r in source[table] if r['id'] == ref.id), None)
    if row is None:
        raise ValueError('所选资产观察或已入账快照不存在于该账户')
    if ref.kind == 'observation':
        value, hashes = _json(row['observation']), [row['evidence']]
    else:
        request = _json(row['request'])
        value, hashes = request.get('snapshot'), request['evidence_hashes']
    if not value or value.get('portfolio_value_as_of') is None or value.get('total_assets') is None:
        raise ValueError('所选估值缺总资产或明确的资产估值时点；不能用记录时间代替')
    if not hashes:
        raise ValueError('所选估值缺原始凭证')
    for sha in hashes:
        evidence.verify(directory, sha)
    return account_nav.Valuation(as_of=value['portfolio_value_as_of'], total_assets=value['total_assets'])


def _account_inputs(source, request, directory):
    if gate.digest(source) != request.expected_state_sha256:
        raise ledger.LedgerError('账户资料已变化，请重新捕获并核对资金覆盖声明', 409)
    active = _active(source)
    start, end = (_valuation(source, ref, directory) for ref in (request.start, request.end))
    coverage = request.coverage
    if not coverage.complete_external_flows or not coverage.no_security_transfers:
        raise ValueError('缺完整外部资金流水声明，或含暂不支持的证券转入转出')
    if (coverage.start, coverage.end) != (start.as_of, end.as_of):
        raise ValueError('资金流水覆盖声明须精确匹配起止估值时点')
    account_nav.validate_endpoints(start, end)
    for sha in coverage.evidence_hashes:
        evidence.verify(directory, sha)
    flows = {key: e for key, e in active.items() if e.kind in ('deposit', 'withdrawal')
             and start.as_of <= e.occurred_at <= end.as_of}
    if len({f.event_id for f in request.flows}) != len(request.flows) or {f.event_id for f in request.flows} != set(flows):
        raise ValueError('须为区间内每笔有效出入金恰好指定一个流前估值；不能遗漏或引用已作废事件')
    values = []
    for ref in request.flows:
        event = flows[ref.event_id]
        row = next(r for r in source['account_journal'] if r['id'] == ref.event_id)
        batch = next(r for r in source['account_batches'] if r['id'] == row['batch_id'])
        if ref.before.kind == 'batch' and ref.before.id >= row['batch_id']:
            raise ValueError('已包含该资金事件的入账快照不能作为流前估值')
        hashes = _json(batch['request'])['evidence_hashes']
        if not hashes:
            raise ValueError('外部资金事件缺原始凭证')
        for sha in hashes:
            evidence.verify(directory, sha)
        values.append(account_nav.Flow(event_id=ref.event_id, occurred_at=event.occurred_at,
            cash=event.cash if event.kind == 'deposit' else event.cash.copy_negate(),
            before=_valuation(source, ref.before, directory)))
    return start, end, values


def _index_closes(raw, start, end, sha):
    index = IndexEvidence.model_validate(raw)
    closes = {row.date: row.close for row in index.rows}
    if len(closes) != len(index.rows):
        raise ValueError('指数日期重复，不能静默覆盖')
    missing = [str(day) for day in (start.date(), end.date()) if day not in closes]
    if missing:
        raise ValueError(f'H00906 缺少 {"、".join(missing)} 的收盘值，请另选两端均有收盘证据的区间。证据 {sha}')
    as_of = date.fromisoformat(index.source_as_of)
    if any(day > as_of for day in closes) or index.fetched_at < end:
        raise ValueError('H00906 指数时点不一致，请核对采集时点或另选区间')
    return closes[start.date()], closes[end.date()]


def _compute(account_inputs, request, directory, *, collected):
    start, end, values = account_inputs
    sha = request.benchmark_evidence_hash
    raw = _json(evidence.read(directory, sha), decimal_numbers=True)
    first, last = _index_closes(raw, start.as_of, end.as_of, sha)
    inputs = account_nav.Inputs(start=start, end=end, flows=values, benchmark_start=first, benchmark_end=last)
    return {'inputs': inputs.model_dump(mode='json'), 'calculation': account_nav.evaluate(inputs),
            'basis': {'currency': 'CNY', 'scope': request.coverage.scope, 'benchmark': 'H00906.CSI',
                'coverage': 'caller_declared_with_evidence', 'trade_history_acceptance': 'not_assessed',
                'benchmark_provenance': 'tools_collected_evidence' if collected else 'supplied_evidence_not_live_collection',
                'costs': 'account_observed_after_actual_costs_vs_index_total_return',
                'valuation_selection': 'explicit_refs_not_latest_record'}}


def _saved_collection(conn, account_id, request, request_hash, directory):
    rows = conn.execute('SELECT artifact_sha256 FROM account_nav_refs '
                        'WHERE account_id=? AND start_date=? AND end_date=?',
                        (account_id, request.coverage.start.date().isoformat(), request.coverage.end.date().isoformat()))
    for row in rows:
        sha = row['artifact_sha256']
        artifact = _json(evidence.read(directory, sha))
        if artifact.get('collection_request_sha256') == request_hash:
            return {'artifact_sha256': sha, 'state_sha256': request.expected_state_sha256, **artifact['result']}
    return None


def evaluate(conn, principal, account_id: str, request: Request, directory: Path, *,
             collect_index: Callable[[str, str], dict] | None = None) -> dict:
    """Record the evidence-backed interval and return its immutable calculation."""
    try:
        request = Request.model_validate(request)
        account_id = ledger._account(account_id)
        collected = request.benchmark_evidence_hash is None
        if collected:
            ledger.authorize(conn, principal)
            request_hash = gate.digest(request.model_dump(mode='json'))
            if saved := _saved_collection(conn, account_id, request, request_hash, directory):
                return saved
        captured = capture(conn, principal, account_id)
        account_inputs = _account_inputs(captured['source'], request, directory)
        if collected:
            if collect_index is None:
                raise ValueError('请通过 account_nav_evaluate 采集指数，或提供 benchmark_evidence_hash')
            start, end, _ = account_inputs
            response = collect_index(start.as_of.date().isoformat(), end.as_of.date().isoformat())
        with db.transaction(conn):
            ledger.authorize(conn, principal)
            if collected:
                # A concurrent caller may have committed while we were fetching the index.
                if saved := _saved_collection(conn, account_id, request, request_hash, directory):
                    return saved
                index_sha = evidence.store(directory, gate.canonical(response).encode())
                if response['status'] != 'ok':
                    if response['error_code'] == EMPTY_RESPONSE:
                        raise ledger.LedgerError(f'H00906 在 {start.as_of.date()} 至 {end.as_of.date()} '
                                                 f'无交易日收盘数据，请另选区间。证据 {index_sha}', 422)
                    raise ledger.LedgerError(f'H00906 行情暂不可用，稍后用原请求重试。证据 {index_sha}', 503)
                request = request.model_copy(update={'benchmark_evidence_hash': index_sha})
            result = _compute(account_inputs, request, directory, collected=collected)
            artifact = {'schema': 'account_nav_v1', 'account_id': account_id, 'source': captured['source'],
                        'request': request.model_dump(mode='json'), 'result': result}
            if collected:
                artifact['collection_request_sha256'] = request_hash
            sha = evidence.store(directory, gate.canonical(artifact).encode())
            nav_refs.record(conn, artifact, sha, principal.key_id)
        return {'artifact_sha256': sha, 'state_sha256': captured['state_sha256'], **result}
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, (ledger.LedgerError, evidence.EvidenceError)):
            raise
        raise ledger.LedgerError(str(exc), 422) from None


def replay(conn, principal, account_id: str, artifact_sha256: str, directory: Path) -> dict:
    """Recompute saved inputs, not current account truth; uploaded hashes are not authentication."""
    ledger.authorize(conn, principal)
    account_id = ledger._account(account_id)
    try:
        artifact = _json(evidence.read(directory, artifact_sha256))
        collected = 'collection_request_sha256' in artifact
        if (set(artifact) - {'collection_request_sha256'} != {'schema', 'account_id', 'source', 'request', 'result'}
                or artifact['schema'] != 'account_nav_v1' or artifact['account_id'] != account_id
                or artifact['source']['account_id'] != account_id):
            raise ValueError('净值输入包格式或账户不一致')
        request = Request.model_validate(artifact['request'])
        if collected:
            if artifact['collection_request_sha256'] != gate.digest(
                    request.model_dump(mode='json') | {'benchmark_evidence_hash': None}):
                raise ValueError('净值采集请求与保存凭证不一致')
            if conn.execute('SELECT 1 FROM account_nav_refs WHERE account_id=? AND artifact_sha256=?',
                            (account_id, artifact_sha256)).fetchone() is None:
                raise ValueError('当前账户缺少该服务采集凭证的已提交引用，请核对原凭证哈希')
        result = _compute(_account_inputs(artifact['source'], request, directory), request, directory, collected=collected)
        if result != artifact['result']:
            raise ValueError('保存结果与固定输入重放不一致')
        return {'artifact_sha256': artifact_sha256, 'mode': 'saved_input_replay', **result}
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, (ledger.LedgerError, evidence.EvidenceError)):
            raise
        raise ledger.LedgerError(str(exc), 422) from None
