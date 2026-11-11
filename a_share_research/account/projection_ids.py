"""Immutable per-account integer IDs for legacy-shaped cycle and event projections.

Writers call sync inside the same transaction as the cost projection. A void leaves
its identity reserved, and read paths only look up mappings; they never allocate IDs.
"""
import json
import sqlite3

_TABLES = {'cycle': 'holdings', 'event': 'holding_events'}


def migrate(conn: sqlite3.Connection) -> None:
    """Create and backfill mappings for existing cost books; caller owns the migration transaction."""
    if not conn.in_transaction:
        raise ValueError('投影编号迁移须在事务内执行')
    conn.execute('''CREATE TABLE IF NOT EXISTS account_projection_ids (
        account_id TEXT NOT NULL REFERENCES account_position_books(account_id),
        kind TEXT NOT NULL CHECK(kind IN ('cycle','event')), identity TEXT NOT NULL,
        legacy_id INTEGER NOT NULL CHECK(legacy_id>0),
        PRIMARY KEY(account_id,kind,identity), UNIQUE(account_id,kind,legacy_id))''')
    for operation in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS account_projection_ids_no_{operation.lower()}
            BEFORE {operation} ON account_projection_ids
            BEGIN SELECT RAISE(ABORT,'projection identities are immutable'); END''')
    for account_id, projection in conn.execute('SELECT account_id,projection FROM account_position_books').fetchall():
        sync(conn, account_id, json.loads(projection))


def lookup(conn: sqlite3.Connection, account_id: str, kind: str) -> dict[str, int]:
    """Read only: return stable integer IDs for one account and entity kind."""
    if kind not in _TABLES:
        raise ValueError('投影实体类型非法')
    return dict(conn.execute('SELECT identity,legacy_id FROM account_projection_ids WHERE account_id=? AND kind=?',
                             (account_id,kind)).fetchall())


def _next(conn, account_id, kind):
    table = _TABLES[kind]
    legacy = conn.execute(f'SELECT coalesce(max(id),0) FROM main.{table}').fetchone()[0]
    issued = conn.execute('SELECT seq FROM main.sqlite_sequence WHERE name=?',(table,)).fetchone()
    mapped = conn.execute('SELECT coalesce(max(legacy_id),0) FROM account_projection_ids WHERE account_id=? AND kind=?',
                          (account_id,kind)).fetchone()[0]
    return max(legacy, issued[0] if issued else 0, mapped) + 1


def _bind(conn, account_id, kind, identity, requested=None):
    if not isinstance(identity,str) or not identity.strip():
        raise ValueError('投影实体身份须为非空字符串')
    if requested is not None and (type(requested) is not int or requested <= 0):
        raise ValueError('旧投影编号须为正整数')
    old = conn.execute('SELECT legacy_id FROM account_projection_ids WHERE account_id=? AND kind=? AND identity=?',
                       (account_id,kind,identity)).fetchone()
    if old:
        if requested is not None and old[0] != requested:
            raise ValueError('实体已有投影编号，不能重新绑定')
        return
    target = requested if requested is not None else _next(conn, account_id, kind)
    other = conn.execute('SELECT identity FROM account_projection_ids WHERE account_id=? AND kind=? AND legacy_id=?',
                         (account_id,kind,target)).fetchone()
    if other:
        raise ValueError(f'旧投影编号 {target} 已绑定其他实体')
    conn.execute('INSERT INTO account_projection_ids(account_id,kind,identity,legacy_id) VALUES (?,?,?,?)',
                 (account_id,kind,identity,target))


def sync(conn: sqlite3.Connection, account_id: str, projection: dict) -> None:
    """Reserve cycle IDs and all stock journal identities, including voided records, atomically."""
    if not conn.in_transaction:
        raise ValueError('投影编号须与成本投影在同一事务内分配')
    cycles = projection['cycles']
    # Reserve explicit legacy identities first so a new ID cannot take a later seed's number.
    for cycle in sorted(cycles, key=lambda c: c.get('legacy_holding_id') is None):
        _bind(conn, account_id, 'cycle', cycle['cycle_id'], cycle.get('legacy_holding_id'))
    rows = conn.execute('SELECT business_id FROM account_journal WHERE account_id=? AND company_key IS NOT NULL ORDER BY id',
                        (account_id,)).fetchall()
    for (identity,) in rows:
        _bind(conn, account_id, 'event', identity)


def inspect(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_projection_ids'").fetchone():
        return []
    problems=[]
    for account,raw in conn.execute('SELECT account_id,projection FROM account_position_books').fetchall():
        try:
            cycles=lookup(conn,account,'cycle')
            events=lookup(conn,account,'event')
            for cycle in json.loads(raw)['cycles']:
                mapped=cycles.get(cycle['cycle_id'])
                if mapped is None or cycle.get('legacy_holding_id') is not None and mapped!=cycle['legacy_holding_id']:
                    raise ValueError('周期编号缺失或与原编号不同')
            if any(identity not in events for (identity,) in conn.execute('SELECT business_id FROM account_journal WHERE account_id=? AND company_key IS NOT NULL',(account,))):
                raise ValueError('事件编号缺失')
        except (ValueError,TypeError,KeyError) as exc:
            problems.append(f'账户投影编号 {account}：{exc}')
    return problems
