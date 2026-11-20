"""Committed interval references to immutable NAV artifacts and their source evidence."""
import json
from pathlib import Path
import sqlite3

from a_share_research import db, gate
from a_share_research.account import evidence


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS account_nav_refs (
        artifact_sha256 TEXT PRIMARY KEY NOT NULL,
        account_id TEXT NOT NULL REFERENCES account_books(account_id),
        start_date TEXT NOT NULL, end_date TEXT NOT NULL, benchmark_code TEXT NOT NULL,
        created_at TEXT NOT NULL, key_id INTEGER NOT NULL REFERENCES api_keys(id),
        evidence_hashes TEXT NOT NULL)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS account_nav_intervals
        ON account_nav_refs(account_id, end_date, start_date)''')
    for operation in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS account_nav_refs_no_{operation.lower()}
            BEFORE {operation} ON account_nav_refs
            BEGIN SELECT RAISE(ABORT,'NAV references are append-only'); END''')


def _reference(artifact: dict) -> dict:
    source, request, result = artifact['source'], artifact['request'], artifact['result']
    if artifact['schema'] != 'account_nav_v1' or artifact['account_id'] != source['account_id']:
        raise ValueError('净值凭证格式或账户不一致')
    # The artifact embeds the frozen ledger, including observations and posted batches.
    hashes = {source['book']['opening_evidence'], request['benchmark_evidence_hash'],
              *request['coverage']['evidence_hashes']}
    hashes.update(row['evidence'] for row in source['account_observations'])
    for row in source['account_batches']:
        hashes.update(json.loads(row['request'])['evidence_hashes'])
    return dict(account_id=source['account_id'], start_date=result['inputs']['start']['as_of'][:10],
                end_date=result['inputs']['end']['as_of'][:10], benchmark_code=result['basis']['benchmark'],
                evidence_hashes=sorted(hashes))


def record(conn: sqlite3.Connection, artifact: dict, sha: str, key_id: int) -> None:
    fields = _reference(artifact)
    conn.execute('''INSERT INTO account_nav_refs
        (artifact_sha256,account_id,start_date,end_date,benchmark_code,created_at,key_id,evidence_hashes)
        VALUES (:sha,:account_id,:start_date,:end_date,:benchmark_code,:created_at,:key_id,:evidence_hashes)
        ON CONFLICT(artifact_sha256) DO NOTHING''',
        dict(fields, sha=sha, key_id=key_id, created_at=db.now(),
             evidence_hashes=gate.canonical(fields['evidence_hashes'])))


def inspect(conn: sqlite3.Connection, directory: Path) -> list[str]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_nav_refs'").fetchone():
        return ['净值区间引用表缺失'] if conn.execute('PRAGMA user_version').fetchone()[0] >= 25 else []
    problems = [f'净值引用 {rowid}：{parent} 记录缺失'
                for _, rowid, parent, _ in conn.execute('PRAGMA foreign_key_check(account_nav_refs)')]
    verified = set()
    for row in conn.execute('SELECT * FROM account_nav_refs ORDER BY artifact_sha256'):
        sha = row['artifact_sha256']
        try:
            fields = _reference(json.loads(evidence.read(directory, sha)))
            hashes = fields.pop('evidence_hashes')
            if any(row[k] != v for k, v in fields.items()) or json.loads(row['evidence_hashes']) != hashes:
                raise ValueError('区间、基准或证据引用与凭证不一致')
            for source_hash in set(hashes) - verified:
                evidence.verify(directory, source_hash)
                verified.add(source_hash)
        except (ValueError, TypeError, KeyError) as exc:
            problems.append(f'净值引用 {sha}：{exc}')
    return problems
