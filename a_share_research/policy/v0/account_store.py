"""Evidenced v0 openings and read-only legacy-table projections of an account journal."""
import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

from a_share_research import db, gate
from a_share_research.account import evidence, ledger, positions, position_store, projection_ids
from a_share_research.account.mirror import MANIFEST
from a_share_research.account.reconciliation import Event
from a_share_research.policy.v0 import account_projection, domain


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql,args)
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns,row)) for row in cursor]


def _profile(conn):
    """Register the current v0 implementation and explicit parameters; never activate a production policy."""
    frameworks = set(domain.FRAMEWORK_ALIASES.values())
    parameters = dict(algorithm_version=1,main_entry_ratio='0.5',
        stops={f:[str(x) for x in domain.get_stop_loss_pct(f)] for f in sorted(frameworks)},
        code_sha256=hashlib.sha256(Path(account_projection.__file__).read_bytes()).hexdigest())
    identity = 'v0:' + gate.digest(parameters)
    conn.execute('INSERT OR IGNORE INTO policy_v0_profiles VALUES (?,?,?)',(identity,db.now(),gate.canonical(parameters)))
    return identity


def initialize(conn, principal, account_id, evidence_directory: Path, evidence_hashes: list[str]):
    """Bind a complete legacy snapshot to an untraded cost opening; preserve all prior event evidence."""
    account_id = ledger._account(account_id)
    if not evidence_hashes:
        raise ledger.LedgerError('政策期初须有证据',422)
    with db.transaction(conn):
        ledger.authorize(conn,principal)
        for sha in evidence_hashes:
            evidence.verify(evidence_directory,sha)
        cost = _rows(conn,'SELECT * FROM account_position_books WHERE account_id=?',(account_id,))
        if not cost:
            raise ledger.LedgerError('须先建立成本期初',404)
        old = _rows(conn,'SELECT * FROM policy_v0_account_openings WHERE account_id=?',(account_id,))
        if old:
            if json.loads(old[0]['evidence_hashes']) != evidence_hashes:
                raise ledger.LedgerError('政策期初已存在且证据不同，不能覆盖')
        else:
            if cost[0]['version'] or conn.execute('SELECT 1 FROM account_journal WHERE account_id=?',(account_id,)).fetchone():
                raise ledger.LedgerError('政策期初须在首笔入账前绑定')
            seeds = [positions.PositionSeed.model_validate(s) for s in json.loads(cost[0]['basis'])]
            snapshot = {t:_rows(conn,f"SELECT {','.join(MANIFEST[t])} FROM main.{t} ORDER BY id")
                        for t in ('holdings','holding_events')}
            active = {r['id']:r for r in snapshot['holdings'] if r['exit_date'] is None}
            if set(active) != {s.legacy_holding_id for s in seeds}:
                raise ledger.LedgerError('政策期初活动持仓与成本期初旧周期编号不一致')
            for seed in seeds:
                row = active[seed.legacy_holding_id]
                if (row['code'] != seed.code or row['shares'] != seed.shares or
                        Decimal(str(row['cost_price'])) != seed.cost_price or row['buy_date'] != seed.buy_date.isoformat()):
                    raise ledger.LedgerError('政策与成本期初的公司、股数、成本或日期不一致')
                for field in ('reference_cost','initial_shares','main_entry_date','main_entry_basis','additions_since_main','stop_loss_15','stop_loss_20'):
                    if row[field] is None:
                        raise ledger.LedgerError(f'政策期初缺少 {field}')
            stamp = conn.execute('SELECT opened_at FROM account_books WHERE account_id=?',(account_id,)).fetchone()[0]
            if any(r['event_date'] > stamp[:10] for r in snapshot['holding_events']):
                raise ledger.LedgerError('旧历史含晚于账户期初的事件')
            profile = _profile(conn)
            conn.execute('INSERT INTO policy_v0_account_openings VALUES (?,?,?,?,?,?)',
                (account_id,profile,db.now(),gate.canonical(evidence_hashes),gate.digest(snapshot),gate.canonical(snapshot)))
    return read(conn,principal,account_id)


def project(conn, account_id):
    """Project from one caller-owned read transaction. This function never creates identities or metadata."""
    opening = _rows(conn,'SELECT * FROM main.policy_v0_account_openings WHERE account_id=?',(account_id,))
    costs = _rows(conn,'SELECT * FROM main.account_position_books WHERE account_id=?',(account_id,))
    if not opening or not costs:
        raise ledger.LedgerError('缺少政策或成本期初',404)
    opening, cost = opening[0], costs[0]
    source = json.loads(opening['snapshot'])
    if gate.digest(source) != opening['source_hash']:
        raise ledger.LedgerError('政策期初快照哈希不一致')
    book = _rows(conn,'SELECT * FROM main.account_books WHERE account_id=?',(account_id,))[0]
    if book['version'] != cost['version']:
        raise ledger.LedgerError('账户与成本版本不一致')
    rows = _rows(conn,'SELECT j.*,b.created_at FROM main.account_journal j JOIN main.account_batches b ON b.id=j.batch_id'
        ' LEFT JOIN main.account_voids v ON v.event_id=j.id WHERE j.account_id=? AND v.event_id IS NULL ORDER BY j.id',(account_id,))
    entries = position_store.entries_for(conn,account_id,rows)
    seeds = [positions.PositionSeed.model_validate(s) for s in json.loads(cost['basis'])]
    economic = positions.replay(seeds,[(r['business_id'],Event.model_validate_json(r['event'])) for r in rows],entries,
                              dividend_cycles=position_store.dividends_for(conn,account_id,rows))
    if economic != json.loads(cost['projection']) or economic['positions'] != json.loads(book['projection'])['positions']:
        raise ledger.LedgerError('账户经济投影与重放不一致')
    profiles = {}
    for row in _rows(conn,'SELECT * FROM main.policy_v0_profiles'):
        parameters = json.loads(row['parameters'])
        if row['identity'] != 'v0:' + gate.digest(parameters) or parameters.get('algorithm_version') != 1:
            raise ledger.LedgerError('政策参数身份或算法版本不支持')
        profiles[row['identity']] = parameters
    originals = {r['id']:r for r in source['holdings']}
    selected = {s.cycle_id:originals[s.legacy_holding_id] for s in seeds}
    records = [dict(business_id=r['business_id'],event=Event.model_validate_json(r['event']),created_at=r['created_at']) for r in rows]
    try:
        projected = account_projection.project(selected,economic,records,entries,profiles,opening['profile_identity'],
            projection_ids.lookup(conn,account_id,'cycle'),projection_ids.lookup(conn,account_id,'event'),
            position_store.distributions_for(conn,account_id,rows))
    except (ValueError, KeyError) as exc:
        raise ledger.LedgerError(f'政策投影缺口：{exc}') from None
    seed_ids = {s.legacy_holding_id for s in seeds}
    for row in projected['holdings']:
        if row['id'] in originals and row['id'] not in seed_ids:
            raise ledger.LedgerError('新周期编号与保存的旧周期冲突')
        originals[row['id']] = row
    old_event_ids = {r['id'] for r in source['holding_events']}
    if any(r['id'] in old_event_ids for r in projected['holding_events']):
        raise ledger.LedgerError('新事件编号与保存的旧事件冲突')
    return dict(account_id=account_id,version=cost['version'],mode='shadow',profile_identity=opening['profile_identity'],
                source_hash=opening['source_hash'],holdings=sorted(originals.values(),key=lambda r:r['id']),
                holding_events=source['holding_events']+projected['holding_events'],history_precision='legacy dates plus journal timestamps')


def read(conn, principal, account_id):
    with db.transaction(conn,immediate=False):
        ledger.authorize(conn,principal)
        return project(conn,ledger._account(account_id))


def install(conn, account_id, *, seal=True):
    """Expose legacy-shaped TEMP tables on a mode=ro connection, then seal all writes."""
    if not conn.in_transaction:
        conn.execute('BEGIN DEFERRED')
    try:
        result = project(conn,account_id)
    except ledger.LedgerError as exc:
        raise sqlite3.OperationalError(str(exc)) from None
    for table in ('holdings','holding_events'):
        ddl = conn.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
        conn.execute(re.sub(r'^CREATE TABLE','CREATE TEMP TABLE',ddl,count=1,flags=re.IGNORECASE))
        columns = MANIFEST[table]
        conn.executemany(f"INSERT INTO temp.{table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         [[row[c] for c in columns] for row in result[table]])
        for (ddl,) in conn.execute("SELECT sql FROM main.sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,)).fetchall():
            conn.execute(re.sub(r'^(CREATE (?:UNIQUE )?INDEX )',r'\1temp.',ddl,count=1,flags=re.IGNORECASE))
    if seal:
        conn.execute('PRAGMA query_only=ON')


def inspect(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='policy_v0_account_openings'").fetchone():
        return []
    problems = []
    for (account,) in conn.execute('SELECT account_id FROM policy_v0_account_openings').fetchall():
        try:
            project(conn,account)
        except (ValueError,TypeError,KeyError,IndexError) as exc:
            problems.append(f'账户政策投影 {account}：{exc}')
    return problems
