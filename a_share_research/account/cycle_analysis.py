"""Account-scoped analysis branches for immutable journal cycle identities."""
import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, StrictInt

from a_share_research import db, gate
from a_share_research.account import analysis, evidence, ledger
from a_share_research.account.mirror import MANIFEST
from a_share_research.account.reconciliation import Value


class Initialize(Value):
    expected_account_version: Annotated[StrictInt, Field(ge=0)]
    evidence_hashes: list[analysis.Text] = Field(min_length=1)
    legacy_revision_id: analysis.PositiveId | None = None


def _row(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    values = cursor.fetchone()
    return dict(zip((c[0] for c in cursor.description), values)) if values is not None else None


def mapped_id(conn, account_id, cycle_id):
    row = conn.execute("SELECT legacy_id FROM main.account_projection_ids WHERE account_id=? AND kind='cycle' AND identity=?",
                       (ledger._account(account_id), cycle_id)).fetchone()
    if row is None:
        raise ledger.LedgerError('账户周期编号不存在', 404)
    return row[0]


def holding(conn, account_id, cycle_id):
    """Resolve an extant cycle from this account; retired mappings remain queryable for audit only."""
    account_id = ledger._account(account_id)
    hid = mapped_id(conn, account_id, cycle_id)
    cost = _row(conn, 'SELECT * FROM main.account_position_books WHERE account_id=?', (account_id,))
    book = _row(conn, 'SELECT version FROM main.account_books WHERE account_id=?', (account_id,))
    if cost is None or book is None or cost['version'] != book['version']:
        raise ledger.LedgerError('账户与成本版本不一致')
    cycles = [c for c in json.loads(cost['projection'])['cycles'] if c['cycle_id'] == cycle_id]
    if len(cycles) != 1:
        raise ledger.LedgerError('周期已因事件更正失效；分析历史保留只读')
    cycle = cycles[0]
    return dict(cycle, id=hid, company_key=cycle['code']), cost['version']


def _baseline(conn, account_id, hid):
    return _row(conn, 'SELECT * FROM main.account_analysis_history WHERE account_id=? AND holding_id=? AND version=0',
                (account_id, hid))


def initialize(conn, principal, account_id: str, cycle_id: str, request: Initialize, evidence_directory: Path):
    """Start a branch with explicit evidence; seed cycles import one explicitly selected legacy revision."""
    request = Initialize.model_validate(request)
    data = request.model_dump(mode='json')
    account_id = ledger._account(account_id)
    with db.transaction(conn):
        ledger.authorize(conn, principal)
        hid = mapped_id(conn, account_id, cycle_id)
        previous = _baseline(conn, account_id, hid)
        if previous:
            if previous['cycle_id'] != cycle_id or json.loads(previous['request'])['initialize'] != data:
                raise ledger.LedgerError('周期分析基线已存在，不能覆盖')
            return analysis.current(conn, principal, hid, account_id=account_id)
        subject, version = holding(conn, account_id, cycle_id)
        if subject['exit_date'] is not None:
            raise ledger.LedgerError('已关闭周期不能建立新的分析分支')
        if request.expected_account_version != version:
            raise ledger.LedgerError(f'账户版本冲突，当前版本 {version}')
        for sha in request.evidence_hashes:
            evidence.verify(evidence_directory, sha)
        snapshot = {table: [] for table in analysis.TABLES}
        legacy_id = subject['legacy_holding_id']
        if legacy_id is not None:
            from a_share_research.account.analysis_rebase import baseline
            source = analysis._latest(conn, legacy_id)
            if source is None or source['id'] != request.legacy_revision_id:
                raise ledger.LedgerError('旧周期须明确选择当前分析版本编号')
            if source['company_key'] != subject['company_key'] or baseline(conn, legacy_id) != analysis._capture(conn, legacy_id):
                raise ledger.LedgerError('旧分析身份或基线已变化，须先协调')
            snapshot = json.loads(source['snapshot'])
            if gate.digest(snapshot) != source['snapshot_hash']:
                raise ledger.LedgerError('旧分析快照哈希不一致')
        else:
            if request.legacy_revision_id is not None:
                raise ledger.LedgerError('新周期不能借用其他周期分析')
            tier = dict.fromkeys(MANIFEST['holding_tier_state'])
            tier.update(holding_id=hid, tier1_status='pending', tier2_status='pending', tier3_status='pending', updated_at=db.now())
            snapshot['holding_tier_state'] = [tier]
        envelope = dict(source='journal_baseline', account_id=account_id, cycle_id=cycle_id, holding_id=hid,
                        company_key=subject['company_key'], initialize=data, snapshot=snapshot)
        return analysis._insert(conn, subject, 0, None, 'journal_baseline', principal, envelope,
                                request.evidence_hashes, snapshot, account_id=account_id, cycle_id=cycle_id)


def current(conn, principal, account_id, cycle_id):
    account_id = ledger._account(account_id)
    with db.transaction(conn, immediate=False):
        ledger.authorize(conn, principal)
        return analysis.current(conn, principal, mapped_id(conn, account_id, cycle_id), account_id=account_id)


def history(conn, principal, account_id, cycle_id, *, after_id=0, limit=100):
    account_id = ledger._account(account_id)
    with db.transaction(conn, immediate=False):
        ledger.authorize(conn, principal)
        return analysis.history(conn, principal, mapped_id(conn, account_id, cycle_id),
                                after_id=after_id, limit=limit, account_id=account_id)


def update(conn, principal, account_id, cycle_id, request: analysis.Update, evidence_directory: Path):
    account_id = ledger._account(account_id)
    request = analysis.Update.model_validate(request)
    data = request.model_dump(mode='json')
    with db.transaction(conn):
        ledger.authorize(conn, principal)
        hid = mapped_id(conn, account_id, cycle_id)
        old = conn.execute('SELECT * FROM account_analysis_history WHERE account_id=? AND holding_id=? AND request_hash=?',
                           (account_id, hid, gate.digest(data))).fetchone()
        if old:
            return analysis._result(old)
        subject, _ = holding(conn, account_id, cycle_id)
        if subject['exit_date'] is not None:
            raise ledger.LedgerError('已关闭持仓周期不能更新分析状态')
        latest = analysis._latest(conn, hid, account_id=account_id)
        if latest is None:
            raise ledger.LedgerError('尚未建立该账户周期的分析基线', 404)
        if latest['version'] != request.expected_version:
            raise ledger.LedgerError(f"分析状态版本冲突，当前版本 {latest['version']}")
        snapshot = projected_snapshot(conn, account_id, cycle_id, hid, subject['company_key'])
        for sha in request.evidence_hashes:
            evidence.verify(evidence_directory, sha)
        analysis.apply_changes(conn, subject, snapshot, request.changes)
        return analysis._insert(conn, subject, latest['version']+1, latest['id'], 'assistant', principal,
                                data, request.evidence_hashes, snapshot, account_id=account_id, cycle_id=cycle_id)


def inspect_baseline(conn, row, request, snapshot, hashes):
    parsed = Initialize.model_validate(request['initialize'])
    expected = dict(source='journal_baseline', account_id=row['account_id'], cycle_id=row['cycle_id'],
                    holding_id=row['holding_id'], company_key=row['company_key'], initialize=parsed.model_dump(mode='json'),
                    snapshot=snapshot)
    if (not row['account_id'] or not row['cycle_id'] or row['version'] != 0 or request != expected
            or hashes != parsed.evidence_hashes or mapped_id(conn,row['account_id'],row['cycle_id']) != row['holding_id']):
        return ['账户周期分析基线归属或来源不一致']
    if parsed.legacy_revision_id is not None:
        source = _row(conn, "SELECT * FROM account_analysis_history WHERE id=? AND account_id=''", (parsed.legacy_revision_id,))
        if (source is None or source['holding_id'] != row['holding_id'] or source['company_key'] != row['company_key']
                or json.loads(source['snapshot']) != snapshot):
            return ['周期分析导入的旧版本不一致']
    return []


def projected_snapshot(conn, account_id, cycle_id, hid, company_key):
    """Validate scoped ownership and snapshot hashes before exposing a TEMP projection."""
    base = _baseline(conn,account_id,hid)
    latest = _row(conn, 'SELECT * FROM main.account_analysis_history WHERE account_id=? AND holding_id=? ORDER BY version DESC LIMIT 1',
                  (account_id,hid))
    if base is None or latest is None:
        raise ledger.LedgerError('影子分析缺少账户周期基线')
    for row in (base,latest):
        if row['cycle_id'] != cycle_id or row['company_key'] != company_key:
            raise ledger.LedgerError('账户周期分析身份不一致')
        snapshot = json.loads(row['snapshot'])
        if gate.digest(snapshot) != row['snapshot_hash'] or gate.digest(json.loads(row['request'])) != row['request_hash']:
            raise ledger.LedgerError('账户周期分析哈希不一致')
        if set(snapshot) != set(analysis.TABLES) or any(
            not isinstance(snapshot[t],list) or any(set(r) != set(MANIFEST[t]) or r['holding_id'] != hid for r in snapshot[t])
            for t in analysis.TABLES):
            raise ledger.LedgerError('账户周期分析状态列或归属不一致')
    issues = inspect_baseline(conn,base,json.loads(base['request']),json.loads(base['snapshot']),json.loads(base['evidence_hashes']))
    if issues:
        raise ledger.LedgerError(issues[0])
    return snapshot
