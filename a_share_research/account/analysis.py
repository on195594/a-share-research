"""Append-only shadow analysis snapshots; legacy monitoring continues to read its own projection."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints, model_validator

from a_share_research import db, gate
from a_share_research.account import evidence, ledger
from a_share_research.account.mirror import MANIFEST
from a_share_research.account.reconciliation import Value

TABLES = ('holding_l3_conditions', 'holding_alerts', 'holding_tier_state', 'holding_thesis_versions')
PositiveId = Annotated[StrictInt, Field(gt=0)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class L3Update(Value):
    kind: Literal['l3']
    id: PositiveId
    status: Literal['pending', 'not_triggered', 'watch', 'triggered']
    as_of: date
    evidence: Text
    next_review_date: date | None = None
    evidence_cadence: Literal['quarterly', 'semiannual', 'annual', 'event', 'unspecified'] | None = None


class AlertUpdate(Value):
    kind: Literal['alert']
    id: PositiveId
    status: Literal['pending', 'resolved']
    evidence: Text


class TierUpdate(Value):
    kind: Literal['tier']
    tier: Literal['tier1', 'tier2', 'tier3']
    status: Literal['pending', 'completed']
    # Entry-time exemptions and exit-path configuration are not ordinary status changes.


Change = Annotated[L3Update | AlertUpdate | TierUpdate, Field(discriminator='kind')]


class Update(Value):
    expected_version: Annotated[StrictInt, Field(ge=0)]
    evidence_hashes: list[Text] = Field(min_length=1)
    reason: Text
    changes: list[Change] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_targets(self):
        targets = [(c.kind, c.tier if isinstance(c, TierUpdate) else c.id) for c in self.changes]
        if len(targets) != len(set(targets)):
            raise ValueError('同一批次不能重复更新同一状态')
        return self


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS account_analysis_history (
        id INTEGER PRIMARY KEY, holding_id INTEGER NOT NULL, company_key TEXT,
        version INTEGER NOT NULL, parent_id INTEGER REFERENCES account_analysis_history(id),
        request_hash TEXT NOT NULL, source TEXT NOT NULL, actor_key_id INTEGER,
        created_at TEXT NOT NULL, snapshot_hash TEXT NOT NULL, evidence_hashes TEXT NOT NULL, request TEXT NOT NULL, snapshot TEXT NOT NULL,
        UNIQUE(holding_id,version), UNIQUE(holding_id,request_hash))''')
    conn.execute('CREATE INDEX IF NOT EXISTS account_analysis_company ON account_analysis_history(company_key,id)')
    for operation in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS account_analysis_no_{operation.lower()}
            BEFORE {operation} ON account_analysis_history
            BEGIN SELECT RAISE(ABORT,'analysis history is append-only'); END''')


def _latest(conn, holding_id):
    return conn.execute('SELECT * FROM account_analysis_history WHERE holding_id=? ORDER BY version DESC LIMIT 1',
                        (holding_id,)).fetchone()


def _result(row):
    return dict(revision_id=row['id'], holding_id=row['holding_id'], company_key=row['company_key'],
                version=row['version'], parent_id=row['parent_id'], source=row['source'], mode='shadow',
                actor_key_id=row['actor_key_id'], recorded_at=row['created_at'], request_hash=row['request_hash'],
                evidence_hashes=json.loads(row['evidence_hashes']), snapshot=json.loads(row['snapshot']))


def _holding(conn, holding_id):
    row = conn.execute('SELECT * FROM holdings WHERE id=?', (holding_id,)).fetchone()
    if row is None:
        raise ledger.LedgerError('持仓周期不存在', 404)
    return row


def _capture(conn, holding_id):
    result = {}
    for table in TABLES:
        columns = MANIFEST[table]
        rows = conn.execute(f"SELECT {','.join(columns)} FROM {table} WHERE holding_id=? ORDER BY 1", (holding_id,))
        result[table] = [dict(zip(columns, row)) for row in rows]
    return result


def _insert(conn, holding, version, parent, source, principal, request, hashes, snapshot):
    cursor = conn.execute('''INSERT INTO account_analysis_history
        (holding_id,company_key,version,parent_id,request_hash,source,actor_key_id,created_at,snapshot_hash,evidence_hashes,request,snapshot)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (holding['id'], holding['company_key'], version, parent, gate.digest(request), source, principal.key_id,
         db.now(), gate.digest(snapshot), gate.canonical(hashes), gate.canonical(request), gate.canonical(snapshot)))
    return _result(conn.execute('SELECT * FROM account_analysis_history WHERE id=?', (cursor.lastrowid,)).fetchone())


def initialize(conn, principal, holding_id: int) -> dict:
    """Capture an exact legacy baseline once, including missing evidence; never reconstruct overwritten history."""
    with db.transaction(conn):
        ledger.authorize(conn, principal)
        holding = _holding(conn, holding_id)
        previous = _latest(conn, holding_id)
        if previous:
            return _result(previous)
        snapshot = _capture(conn, holding_id)
        request = {'source': 'legacy_baseline', 'holding_id': holding_id, 'snapshot': snapshot}
        return _insert(conn, holding, 0, None, 'legacy_baseline', principal, request, [], snapshot)


def current(conn, principal, holding_id: int) -> dict:
    ledger.authorize(conn, principal)
    row = _latest(conn, holding_id)
    if row is None:
        raise ledger.LedgerError('尚未建立该持仓周期的分析基线', 404)
    return _result(row)


def history(conn, principal, holding_id: int, *, after_id: int = 0, limit: int = 100) -> dict:
    ledger.authorize(conn, principal)
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ledger.LedgerError('历史分页参数无效', 422)
    rows = conn.execute('SELECT * FROM account_analysis_history WHERE holding_id=? AND id>? ORDER BY id LIMIT ?',
                        (holding_id, after_id, limit)).fetchall()
    return {'revisions': [_result(r) for r in rows], 'next_after_id': rows[-1]['id'] if rows else after_id}


def _one(snapshot, table, identity):
    rows = [r for r in snapshot[table] if r.get('id', r.get('holding_id')) == identity]
    if len(rows) != 1:
        raise ledger.LedgerError('状态实体不存在或不唯一')
    return rows[0]


def update(conn, principal, holding_id: int, request: Update, evidence_directory: Path) -> dict:
    """Append an atomic status revision. Does not edit entry configuration, rewrite theses or alter legacy state."""
    request = Update.model_validate(request)
    data = request.model_dump(mode='json')
    with db.transaction(conn):
        ledger.authorize(conn, principal)
        previous = conn.execute('SELECT * FROM account_analysis_history WHERE holding_id=? AND request_hash=?',
                                (holding_id, gate.digest(data))).fetchone()
        if previous:
            return _result(previous)
        holding = _holding(conn, holding_id)
        if holding['exit_date'] is not None:
            raise ledger.LedgerError('已关闭持仓周期不能更新分析状态')
        latest = _latest(conn, holding_id)
        if latest is None:
            raise ledger.LedgerError('尚未建立分析基线', 404)
        if holding['company_key'] != latest['company_key']:
            raise ledger.LedgerError('持仓周期的公司身份已变化，须先核对基线')
        if request.expected_version != latest['version']:
            raise ledger.LedgerError(f"分析状态版本冲突，当前版本 {latest['version']}")
        # A mirror refresh must not silently make a shadow revision refer to another legacy state.
        baseline = conn.execute('SELECT snapshot FROM account_analysis_history WHERE holding_id=? AND version=0',
                                (holding_id,)).fetchone()
        if json.loads(baseline[0]) != _capture(conn, holding_id):
            raise ledger.LedgerError('旧分析基线已变化，须先人工核对影子分支')
        for sha in request.evidence_hashes:
            evidence.verify(evidence_directory, sha)
        snapshot = json.loads(latest['snapshot'])
        stamp = db.now()
        for change in request.changes:
            if isinstance(change, L3Update):
                row = _one(snapshot, 'holding_l3_conditions', change.id)
                if not row['is_active']:
                    raise ledger.LedgerError('已退役 L3 条件不可更新')
                row.update(status=change.status, as_of=change.as_of.isoformat(), evidence=change.evidence,
                           next_review_date=change.next_review_date.isoformat() if change.next_review_date else None,
                           updated_at=stamp)
                if change.evidence_cadence is not None:
                    row['evidence_cadence'] = change.evidence_cadence
            elif isinstance(change, AlertUpdate):
                row = _one(snapshot, 'holding_alerts', change.id)
                if row['status'] == 'resolved' or change.status == 'pending' and row['status'] != 'active':
                    raise ledger.LedgerError('预警当前状态不允许该更新')
                row.update(status=change.status, updated_at=stamp)
                if change.status == 'resolved':
                    row.update(resolved_at=stamp, resolution_evidence=change.evidence)
                else:
                    row['evidence'] = change.evidence
            else:
                row = _one(snapshot, 'holding_tier_state', holding_id)
                if row[change.tier + '_status'] == 'exempted':
                    raise ledger.LedgerError('买入时豁免不能由普通状态更新更改')
                row.update({change.tier + '_status': change.status, 'updated_at': stamp})
        return _insert(conn, holding, latest['version'] + 1, latest['id'], 'assistant', principal,
                       data, request.evidence_hashes, snapshot)


def inspect(conn) -> list[str]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_analysis_history'").fetchone():
        return []
    problems, previous = [], {}
    cursor = conn.execute('SELECT * FROM account_analysis_history ORDER BY holding_id,version')
    columns = [c[0] for c in cursor.description]
    for values in cursor:
        row = dict(zip(columns, values))
        label = f"分析历史 {row['id']}"
        parent = previous.get(row['holding_id'])
        expected = (parent['version'] + 1, parent['id']) if parent else (0, None)
        if (row['version'], row['parent_id']) != expected:
            problems.append(label + '：版本或前序关系不一致')
        if parent and row['company_key'] != parent['company_key']:
            problems.append(label + '：持仓周期公司身份变化')
        try:
            request, snapshot = json.loads(row['request']), json.loads(row['snapshot'])
            hashes = json.loads(row['evidence_hashes'])
            if gate.digest(request) != row['request_hash'] or gate.digest(snapshot) != row['snapshot_hash']:
                problems.append(label + '：请求或快照哈希不一致')
            if not isinstance(snapshot, dict) or set(snapshot) != set(TABLES):
                raise ValueError
            for table in TABLES:
                states = snapshot[table]
                if not isinstance(states, list) or any(not isinstance(r, dict) or r.get('holding_id') != row['holding_id'] for r in states):
                    raise ValueError
            if row['source'] == 'legacy_baseline':
                if row['version'] != 0 or hashes or request != {'source':'legacy_baseline', 'holding_id':row['holding_id'], 'snapshot':snapshot}:
                    problems.append(label + '：旧基线来源不一致')
            elif row['source'] == 'assistant':
                parsed = Update.model_validate(request)
                if row['version'] == 0 or parsed.expected_version != row['version'] - 1 or hashes != parsed.evidence_hashes:
                    problems.append(label + '：追加请求版本或证据不一致')
            else:
                problems.append(label + '：未知来源')
        except (ValueError, TypeError, KeyError):
            problems.append(label + '：请求或快照非法')
        previous[row['holding_id']] = row
    return problems
