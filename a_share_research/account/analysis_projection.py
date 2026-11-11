"""Connection-local v0 projection of shadow analysis; main database is never modified.

Legacy mode shadows active holdings. Account mode also retains scoped analysis of closed cycles.
Temporary tables preserve v0's table/index introspection; query_only seals them after loading.
"""
from __future__ import annotations

import json
import re
import sqlite3

from a_share_research import gate
from a_share_research.account.analysis import TABLES
from a_share_research.account.analysis_rebase import baseline
from a_share_research.account.mirror import MANIFEST


def _legacy(conn, holding_id):
    return {table: [dict(zip(MANIFEST[table], row)) for row in conn.execute(
                f"SELECT {','.join(MANIFEST[table])} FROM main.{table} WHERE holding_id=? ORDER BY 1",
                (holding_id,))] for table in TABLES}


def install(conn: sqlite3.Connection, *, seal: bool = True, account_id: str | None = None) -> None:
    """Validate and materialize one consistent shadow read, or raise without exposing partial state.

    The caller owns the connection and must close it on failure. This function keeps its read
    transaction open so the subsequent v0 queries share the same database snapshot.
    """
    if not conn.in_transaction:
        conn.execute('BEGIN DEFERRED')
    if account_id:
        from a_share_research.account import cycle_analysis, projection_ids
        cost = conn.execute('SELECT projection FROM main.account_position_books WHERE account_id=?',(account_id,)).fetchone()
        if cost is None:
            raise sqlite3.OperationalError('缺少账户成本期初')
        ids = projection_ids.lookup(conn,account_id,'cycle')
        cycles = {ids[c['cycle_id']]:c for c in json.loads(cost[0])['cycles']}
        active = conn.execute('SELECT id,company_key,exit_date FROM temp.holdings').fetchall()
    else:
        cycles = {}
        active = conn.execute('SELECT id,company_key,exit_date FROM main.holdings WHERE exit_date IS NULL').fetchall()
    snapshots = {}
    for holding_id, company_key, exit_date in active:
        cycle = cycles.get(holding_id)
        if cycle is not None:
            if cycle['code'] != company_key:
                raise sqlite3.OperationalError('账户周期与持仓公司身份不一致')
            scoped = conn.execute('SELECT 1 FROM main.account_analysis_history WHERE account_id=? AND holding_id=?',
                                  (account_id,holding_id)).fetchone()
            if scoped:
                try:
                    snapshots[holding_id] = cycle_analysis.projected_snapshot(conn,account_id,cycle['cycle_id'],holding_id,company_key)
                except (ValueError,TypeError,KeyError) as exc:
                    raise sqlite3.OperationalError(f'账户周期分析无法读取：{exc}') from None
                continue
            if cycle['legacy_holding_id'] is None:
                if exit_date is None:
                    raise sqlite3.OperationalError(f'影子分析缺少账户周期 {cycle["cycle_id"]} 的基线')
                snapshots[holding_id] = {table:[] for table in TABLES}
                continue
        if account_id:
            original = conn.execute('SELECT exit_date FROM main.holdings WHERE id=?',(holding_id,)).fetchone()
            if original is None or original[0] is not None:
                continue
        rows = conn.execute('''SELECT version,company_key,snapshot,snapshot_hash FROM main.account_analysis_history
            WHERE holding_id=? AND account_id='' AND (version=0 OR version=(
                SELECT max(version) FROM main.account_analysis_history WHERE holding_id=? AND account_id='')) ORDER BY version''',
            (holding_id, holding_id)).fetchall()
        if not rows or rows[0][0] != 0:
            raise sqlite3.OperationalError(f'影子分析缺少持仓周期 {holding_id} 的基线')
        if any(row[1] != company_key for row in rows):
            raise sqlite3.OperationalError(f'影子分析持仓周期 {holding_id} 公司身份变化')
        try:
            parsed = [json.loads(row[2]) for row in rows]
            if any(gate.digest(snapshot) != row[3] for snapshot, row in zip(parsed, rows)):
                raise ValueError('快照哈希不一致')
            if baseline(conn, holding_id) != _legacy(conn, holding_id):
                raise ValueError('旧分析基线已变化')
            latest = parsed[-1]
            if not isinstance(latest, dict) or set(latest) != set(TABLES):
                raise ValueError('状态表不完整')
            for table in TABLES:
                if not isinstance(latest[table], list) or any(
                    not isinstance(row, dict) or set(row) != set(MANIFEST[table]) or row['holding_id'] != holding_id
                    for row in latest[table]
                ):
                    raise ValueError('状态列或持仓归属不一致')
            snapshots[holding_id] = latest
        except (ValueError, TypeError, KeyError) as exc:
            raise sqlite3.OperationalError(f'影子分析持仓周期 {holding_id} 无法读取：{exc}') from None
    for table in TABLES:
        sql = conn.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        conn.execute(re.sub(r'^CREATE TABLE', 'CREATE TEMP TABLE', sql, count=1, flags=re.IGNORECASE))
        columns = ','.join(MANIFEST[table])
        conn.execute(f'INSERT INTO temp.{table} ({columns}) SELECT {columns} FROM main.{table}')
        for holding_id, snapshot in snapshots.items():
            conn.execute(f'DELETE FROM temp.{table} WHERE holding_id=?', (holding_id,))
            conn.executemany(f"INSERT INTO temp.{table} ({columns}) VALUES ({','.join('?' for _ in MANIFEST[table])})",
                             [[row[column] for column in MANIFEST[table]] for row in snapshot[table]])
        indexes = conn.execute("SELECT sql FROM main.sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                               (table,)).fetchall()
        for (sql,) in indexes:
            conn.execute(re.sub(r'^(CREATE (?:UNIQUE )?INDEX )', r'\1temp.', sql, count=1, flags=re.IGNORECASE))
    if seal:
        conn.execute('PRAGMA query_only=ON')
