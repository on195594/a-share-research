"""Shadow account journal: immutable events, void corrections and atomic reconciliation receipts.

Does not update legacy holdings or switch account_source. Production cutover is a later gate.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from pydantic import Field, StrictInt

from a_share_research import api_keys, db, gate
from a_share_research.account import evidence
from a_share_research.account.reconciliation import Event, Money, Opening, Snapshot, Value, identity, money, project, reconcile


class Batch(Value):
    expected_version: Annotated[StrictInt, Field(ge=0)]
    events: list[Event] = Field(default_factory=list)
    void_event_ids: list[Annotated[StrictInt, Field(gt=0)]] = Field(default_factory=list)
    evidence_hashes: list[str] = Field(default_factory=list)
    snapshot: Snapshot | None = None


class Observation(Value):
    observed_at: datetime
    total_assets: Annotated[Money, Field(ge=0)] | None = None
    cash: Annotated[Money, Field(ge=0)] | None = None
    market_values: dict[str, Annotated[Money, Field(ge=0)]] = Field(default_factory=dict)


class LedgerError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS account_books (
        account_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
        opened_at TEXT NOT NULL, created_at TEXT NOT NULL, opening_evidence TEXT NOT NULL,
        tolerance TEXT NOT NULL, opening TEXT NOT NULL, projection TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS account_batches (
        id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account_books(account_id),
        request_hash TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
        receipt TEXT NOT NULL, request TEXT NOT NULL)""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS account_posted_request ON account_batches(account_id,request_hash) WHERE status='posted'")
    conn.execute("""CREATE TABLE IF NOT EXISTS account_journal (
        id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account_books(account_id),
        batch_id INTEGER NOT NULL REFERENCES account_batches(id), business_id TEXT NOT NULL,
        company_key TEXT, occurred_at TEXT NOT NULL, event TEXT NOT NULL)""")
    conn.execute('CREATE INDEX IF NOT EXISTS account_journal_business ON account_journal(account_id,business_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS account_journal_company ON account_journal(company_key,occurred_at)')
    conn.execute("""CREATE TABLE IF NOT EXISTS account_voids (
        event_id INTEGER PRIMARY KEY REFERENCES account_journal(id),
        batch_id INTEGER NOT NULL REFERENCES account_batches(id), created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS account_observations (
        id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        created_at TEXT NOT NULL, evidence TEXT NOT NULL, observation TEXT NOT NULL,
        UNIQUE(account_id,request_hash))""")
    for table in ('account_batches', 'account_journal', 'account_voids', 'account_observations'):
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation}"
                         f" ON {table} BEGIN SELECT RAISE(ABORT,'account journal is append-only'); END")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS account_opening_immutable
        BEFORE UPDATE OF account_id,opened_at,opening_evidence,tolerance,opening ON account_books
        BEGIN SELECT RAISE(ABORT,'account opening is immutable'); END""")


def authorize(conn, principal):
    role = conn.execute('SELECT role FROM api_keys WHERE id=?', (principal.key_id,)).fetchone()
    if role is None or role[0] != principal.role or principal.role != 'assistant':
        raise LedgerError('此凭据没有账户权限或已撤销', 403)


def _account(account_id):
    if not isinstance(account_id, str) or not 1 <= len(account_id.strip()) <= 80:
        raise LedgerError('账户标识须为1–80字符', 422)
    return account_id.strip()


def initialize(conn: sqlite3.Connection, principal: api_keys.Principal, account_id: str, opening: Opening,
               opened_at: datetime, evidence_directory: Path, evidence_hash: str, *, tolerance: Decimal) -> dict:
    """Create a shadow book once; explicit opening and tolerance are never guessed from incomplete screenshots."""
    account_id = _account(account_id)
    opening = Opening.model_validate(opening)
    tolerance = money(tolerance)
    if tolerance < 0:
        raise LedgerError('容差不能为负', 422)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=db.SHANGHAI)
    stamp = opened_at.astimezone(db.SHANGHAI).isoformat()
    with db.transaction(conn):
        authorize(conn, principal)
        evidence.verify(evidence_directory, evidence_hash)
        text = gate.canonical(opening.model_dump(mode='json'))
        row = conn.execute('SELECT * FROM account_books WHERE account_id=?', (account_id,)).fetchone()
        if row:
            if (Opening.model_validate_json(row['opening']) != opening or row['opened_at'] != stamp
                    or row['opening_evidence'] != evidence_hash or Decimal(row['tolerance']) != tolerance):
                raise LedgerError('账户期初已存在且不同，不能覆盖')
        else:
            conn.execute('INSERT INTO account_books(account_id,opened_at,created_at,opening_evidence,tolerance,opening,projection)'
                         ' VALUES (?,?,?,?,?,?,?)', (account_id, stamp, db.now(), evidence_hash, str(tolerance), text, text))
    return state(conn, principal, account_id)


def state(conn, principal, account_id: str) -> dict:
    authorize(conn, principal)
    row = conn.execute('SELECT * FROM account_books WHERE account_id=?', (_account(account_id),)).fetchone()
    if row is None:
        raise LedgerError('尚未建立影子账本期初', 404)
    return dict(account_id=row['account_id'], version=row['version'], mode='shadow',
                projection=json.loads(row['projection']), tolerance=row['tolerance'])


def _save_receipt(conn, account_id, request_hash, result, request):
    # Allocate IDs explicitly within BEGIN IMMEDIATE so the immutable receipt is written only once.
    batch_id = conn.execute('SELECT coalesce(max(id),0)+1 FROM account_batches').fetchone()[0]
    result = result | {'receipt_id': batch_id, 'request_hash': request_hash, 'mode': 'shadow',
                       'account_id': account_id, 'evidence_hashes': request['evidence_hashes']}
    conn.execute('INSERT INTO account_batches(id,account_id,request_hash,status,created_at,receipt,request) VALUES (?,?,?,?,?,?,?)',
                 (batch_id, account_id, request_hash, result['status'], db.now(), gate.canonical(result), gate.canonical(request)))
    return result


def receipt(conn, principal, account_id, receipt_id):
    authorize(conn, principal)
    row = conn.execute('SELECT receipt FROM account_batches WHERE account_id=? AND id=?',
                       (_account(account_id), receipt_id)).fetchone()
    if row is None:
        raise LedgerError('回执不存在', 404)
    return json.loads(row[0])


def events(conn, principal, account_id: str, *, after_id: int = 0, limit: int = 100) -> dict:
    """Page immutable events with their current void status so corrections can name original IDs."""
    authorize(conn, principal)
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise LedgerError('事件分页参数无效', 422)
    rows = conn.execute('SELECT j.id,j.batch_id,j.business_id,j.event,v.event_id AS voided'
                        ' FROM account_journal j LEFT JOIN account_voids v ON v.event_id=j.id'
                        ' WHERE j.account_id=? AND j.id>? ORDER BY j.id LIMIT ?',
                        (_account(account_id), after_id, limit)).fetchall()
    return {'events': [dict(id=r['id'], batch_id=r['batch_id'],business_id=r['business_id'],
                           active=r['voided'] is None, event=json.loads(r['event'])) for r in rows],
            'next_after_id': rows[-1]['id'] if rows else after_id}


def _basis_problems(conn, event):
    if event.report_id is None:
        return []
    report = conn.execute("SELECT r.company_key,r.created_at,r.passed,u.status,u.kind FROM reports r"
                          " JOIN runs u ON u.id=r.run_id WHERE r.id=?", (event.report_id,)).fetchone()
    if (report is None or report['company_key'] != event.code or not report['passed']
            or report['status'] != 'done' or report['kind'] not in ('research', 'external')
            or report['created_at'][:10] > event.occurred_at.date().isoformat()):
        return [{'field': 'report_id', 'actual': event.report_id, 'expected': '同公司、通过复核且不晚于成交日的报告'}]
    return []


def submit(conn: sqlite3.Connection, principal, account_id: str, batch: Batch, evidence_directory: Path) -> dict:
    """Return a durable failure receipt when storage can recover after rolling back the batch."""
    account_id = _account(account_id)
    batch = Batch.model_validate(batch)
    try:
        return _submit_atomic(conn, principal, account_id, batch, evidence_directory)
    except sqlite3.Error as exc:
        logging.getLogger(__name__).error('Account batch rolled back (%s)', type(exc).__name__)
        # This transaction only records the failure. It never retries a financial mutation.
        # If storage itself is unavailable this also fails, and HTTP returns service_failure/503.
        with db.transaction(conn):
            authorize(conn, principal)
            book = conn.execute('SELECT version,projection FROM account_books WHERE account_id=?', (account_id,)).fetchone()
            if book is None:
                raise
            before = json.loads(book['projection'])
            request = batch.model_dump(mode='json')
            return _save_receipt(conn, account_id, gate.digest(request),
                dict(status='service_failure',version=book['version'],before=before,after=before,
                     added=0,duplicates=0,voided=0,
                     problems=[{'field':'service','expected':'入账事务已回滚，可用原请求重试'}]), request)


def _submit_atomic(conn: sqlite3.Connection, principal, account_id: str, batch: Batch, evidence_directory: Path) -> dict:
    """Persist all journal changes only after quantity, cash and total-assets reconciliation succeeds."""
    account_id = _account(account_id)
    batch = Batch.model_validate(batch)
    request_hash = gate.digest(batch.model_dump(mode='json'))
    with db.transaction(conn):
        authorize(conn, principal)
        book = conn.execute('SELECT * FROM account_books WHERE account_id=?', (account_id,)).fetchone()
        if book is None:
            raise LedgerError('尚未建立影子账本期初', 404)
        previous = conn.execute("SELECT receipt FROM account_batches WHERE account_id=? AND request_hash=? AND status='posted'",
                                (account_id, request_hash)).fetchone()
        if previous:
            return json.loads(previous[0])
        before = json.loads(book['projection'])
        result = dict(status='conflict', version=book['version'], before=before, after=before,
                      added=0, duplicates=0, voided=0, problems=[])
        def reject(status, problems):
            return _save_receipt(conn, account_id, request_hash, result | {'status': status, 'problems': problems}, batch.model_dump(mode='json'))
        missing = []
        if batch.snapshot is None:
            missing.append({'field': 'snapshot', 'expected': '完整期末持仓、现金、市值与总资产'})
        if not batch.evidence_hashes:
            missing.append({'field': 'evidence_hashes', 'expected': '已存储的成交及快照材料'})
        for sha in batch.evidence_hashes:
            try:
                evidence.verify(evidence_directory, sha)
            except evidence.EvidenceError as exc:
                missing.append({'field': 'evidence_hashes', 'actual': sha, 'expected': str(exc)})
        if missing:
            return reject('missing_material', missing)
        if batch.expected_version != book['version']:
            return reject('conflict', [{'field': 'version', 'expected': book['version'], 'actual': batch.expected_version}])
        rows = conn.execute('SELECT j.* FROM account_journal j LEFT JOIN account_voids v ON v.event_id=j.id'
                            ' WHERE j.account_id=? AND v.event_id IS NULL ORDER BY j.id', (account_id,)).fetchall()
        active = {r['id']: r for r in rows}
        voids = set(batch.void_event_ids)
        if len(voids) != len(batch.void_event_ids) or not voids <= active.keys():
            return reject('conflict', [{'field': 'void_event_ids', 'expected': '本账户未作废事件且不重复',
                                       'actual': batch.void_event_ids}])
        retained = [r for r in rows if r['id'] not in voids]
        by_business = {r['business_id']: Event.model_validate_json(r['event']) for r in retained}
        new, duplicates, problems = [], 0, []
        opening_time = datetime.fromisoformat(book['opened_at'])
        for event in batch.events:
            if event.occurred_at < opening_time:
                problems.append({'field': 'occurred_at', 'expected': '不早于期初', 'actual': event.occurred_at.isoformat()})
            problems += _basis_problems(conn, event)
            business_id = identity(event, account_id)
            if business_id in by_business:
                if by_business[business_id] != event:
                    problems.append({'field': 'business_id', 'actual': business_id, 'expected': '业务身份相同但内容不同，须作废更正'})
                else:
                    duplicates += 1
            else:
                by_business[business_id] = event
                new.append((business_id, event))
        if problems:
            return reject('conflict', problems)
        try:
            projected = project(Opening.model_validate_json(book['opening']),
                                [Event.model_validate_json(r['event']) for r in retained] + [e for _, e in new])
        except ValueError as exc:
            return reject('conflict', [{'field': 'events', 'expected': str(exc)}])
        problems = reconcile(projected, batch.snapshot, Decimal(book['tolerance']))
        if problems:
            return reject('conflict', problems)
        version = book['version'] + int(bool(new or voids))
        result = _save_receipt(conn, account_id, request_hash, result | dict(status='posted', version=version,
                              added=len(new), duplicates=duplicates, voided=len(voids),
                              after=projected.model_dump(mode='json')), batch.model_dump(mode='json'))
        for event_id in voids:
            conn.execute('INSERT INTO account_voids(event_id,batch_id,created_at) VALUES (?,?,?)',
                         (event_id, result['receipt_id'], db.now()))
        for business_id, event in new:
            conn.execute('INSERT INTO account_journal(account_id,batch_id,business_id,company_key,occurred_at,event)'
                         ' VALUES (?,?,?,?,?,?)', (account_id, result['receipt_id'], business_id, event.code,
                         event.occurred_at.isoformat(), gate.canonical(event.model_dump(mode='json'))))
        conn.execute('UPDATE account_books SET version=?,projection=? WHERE account_id=?',
                     (version, gate.canonical(projected.model_dump(mode='json')), account_id))
        return result


def observe(conn, principal, account_id: str, observation: Observation, evidence_directory: Path,
            evidence_hash: str) -> dict:
    """An incomplete screenshot can record assets without claiming any missing trades occurred."""
    account_id = _account(account_id)
    observation = Observation.model_validate(observation)
    if observation.total_assets is None and observation.cash is None and not observation.market_values:
        raise LedgerError('资产观察至少提供一项金额', 422)
    data = observation.model_dump(mode='json')
    request_hash = gate.digest({'observation': data, 'evidence_hash': evidence_hash})
    with db.transaction(conn):
        authorize(conn, principal)
        evidence.verify(evidence_directory, evidence_hash)
        conn.execute('INSERT OR IGNORE INTO account_observations(account_id,request_hash,created_at,evidence,observation)'
                     ' VALUES (?,?,?,?,?)', (account_id, request_hash, db.now(), evidence_hash, gate.canonical(data)))
        row = conn.execute('SELECT id FROM account_observations WHERE account_id=? AND request_hash=?',
                           (account_id, request_hash)).fetchone()
        return {'status': 'observed', 'observation_id': row[0], 'trades_recorded': False, 'request_hash': request_hash}
