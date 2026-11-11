"""Explicit cost openings and atomic journal-driven shadow projections."""
import json
from datetime import date
from pathlib import Path

from a_share_research import db, gate
from a_share_research.account import evidence, ledger, positions
from a_share_research.account.reconciliation import Event, Opening, identity


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS account_position_books (
        account_id TEXT PRIMARY KEY REFERENCES account_books(account_id), version INTEGER NOT NULL,
        created_at TEXT NOT NULL, evidence_hashes TEXT NOT NULL, basis TEXT NOT NULL, projection TEXT NOT NULL)''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS account_position_opening_immutable
        BEFORE UPDATE OF account_id,created_at,evidence_hashes,basis ON account_position_books
        BEGIN SELECT RAISE(ABORT,'position opening is immutable'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS account_position_no_delete BEFORE DELETE ON account_position_books
        BEGIN SELECT RAISE(ABORT,'position opening is immutable'); END''')


def initialize(conn, principal, account_id: str, seeds: list[positions.PositionSeed],
               evidence_directory: Path, evidence_hashes: list[str]) -> dict:
    """Attach an evidenced cost basis at the account opening, before any financial events."""
    account_id = ledger._account(account_id)
    seeds = [positions.PositionSeed.model_validate(s) for s in seeds]
    if not evidence_hashes:
        raise ledger.LedgerError('成本期初须有明确证据', 422)
    basis = [s.model_dump(mode='json') for s in seeds]
    with db.transaction(conn):
        ledger.authorize(conn, principal)
        for sha in evidence_hashes:
            evidence.verify(evidence_directory, sha)
        old = conn.execute('SELECT * FROM account_position_books WHERE account_id=?',(account_id,)).fetchone()
        if old:
            if json.loads(old['basis']) != basis or json.loads(old['evidence_hashes']) != evidence_hashes:
                raise ledger.LedgerError('成本期初已存在且不同，不能覆盖')
        else:
            book = conn.execute('SELECT * FROM account_books WHERE account_id=?',(account_id,)).fetchone()
            if book is None:
                raise ledger.LedgerError('须先建立账户股数现金期初', 404)
            if book['version'] or conn.execute('SELECT 1 FROM account_journal WHERE account_id=?',(account_id,)).fetchone():
                raise ledger.LedgerError('须在首笔入账前建立成本期初')
            opening = Opening.model_validate_json(book['opening'])
            projection = positions.replay(seeds, [], {})
            if projection['positions'] != {k:v for k,v in opening.positions.items() if v}:
                raise ledger.LedgerError('成本期初股数与账户期初不一致')
            if any(s.buy_date > date.fromisoformat(book['opened_at'][:10]) for s in seeds):
                raise ledger.LedgerError('持仓建仓日期晚于账户期初')
            conn.execute('INSERT INTO account_position_books VALUES (?,?,?,?,?,?)',
                         (account_id,0,db.now(),gate.canonical(evidence_hashes),gate.canonical(basis),gate.canonical(projection)))
    return state(conn, principal, account_id)


def state(conn, principal, account_id: str) -> dict:
    ledger.authorize(conn, principal)
    row = conn.execute('SELECT * FROM account_position_books WHERE account_id=?',(ledger._account(account_id),)).fetchone()
    if row is None:
        raise ledger.LedgerError('尚未提供明确成本期初，不能由股数和现金推导', 404)
    return dict(account_id=row['account_id'],version=row['version'],mode='shadow',
                cash_flow_scope='since_opening',policy_evaluation='pending',projection=json.loads(row['projection']))


def entries_for(conn, account_id, rows):
    return _context_for(conn, account_id, rows, 'position_entries', positions.PositionEntry.model_validate)


def dividends_for(conn, account_id, rows):
    return _context_for(conn, account_id, rows, 'position_dividends', str)


def _context_for(conn, account_id, rows, field, parse):
    """Recover context from the original immutable batch, keyed by business identity."""
    entries, batches = {}, {}
    for row in rows:
        bid = row['batch_id']
        if bid not in batches:
            raw = conn.execute('SELECT request FROM account_batches WHERE id=?',(bid,)).fetchone()[0]
            request = json.loads(raw)
            batches[bid] = {identity(Event.model_validate(request['events'][int(i)]),account_id):
                           parse(value)
                           for i,value in request.get(field,{}).items()}
        if row['business_id'] in batches[bid]:
            entries[row['business_id']] = batches[bid][row['business_id']]
    return entries


def inspect(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_position_books'").fetchone():
        return []
    problems = []
    for cost in conn.execute('SELECT * FROM account_position_books'):
        account = cost['account_id']
        try:
            book = conn.execute('SELECT * FROM account_books WHERE account_id=?',(account,)).fetchone()
            if book is None or cost['version'] != book['version']:
                raise ValueError('账户或版本不一致')
            seeds = [positions.PositionSeed.model_validate(s) for s in json.loads(cost['basis'])]
            opening = Opening.model_validate_json(book['opening'])
            if positions.replay(seeds,[],{})['positions'] != {k:v for k,v in opening.positions.items() if v}:
                raise ValueError('期初股数不一致')
            rows = conn.execute('SELECT j.* FROM account_journal j LEFT JOIN account_voids v ON v.event_id=j.id'
                                ' WHERE j.account_id=? AND v.event_id IS NULL ORDER BY j.id',(account,)).fetchall()
            replayed = positions.replay(seeds,[(r['business_id'],Event.model_validate_json(r['event'])) for r in rows],entries_for(conn,account,rows),dividend_cycles=dividends_for(conn,account,rows))
            if replayed != json.loads(cost['projection']) or replayed['positions'] != json.loads(book['projection'])['positions']:
                raise ValueError('成本或股数重放不一致')
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            problems.append(f'账户成本 {account}：{exc}')
    return problems
