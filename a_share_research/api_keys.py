"""Manage persistent research API keys locally; only creation prints the secret."""

import argparse
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal

from a_share_research import db


Role = Literal["research", "assistant", "owner"]
ROLES = ("research", "assistant", "owner")


@dataclass(frozen=True)
class Principal:
    key_id: int
    role: Role


def migrate_roles(conn: sqlite3.Connection) -> None:
    """Add roles without changing existing tokens or their research privileges."""
    if "role" not in {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}:
        conn.execute("ALTER TABLE api_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'research' "
                     "CHECK (role IN ('research', 'assistant', 'owner'))")
    # Roles cannot be escalated in place: rotate the key so old sessions cannot
    # regain a blind identity after observing account information.
    conn.execute("CREATE TRIGGER IF NOT EXISTS api_keys_role_immutable BEFORE UPDATE OF role ON api_keys "
                 "WHEN NEW.role != OLD.role BEGIN SELECT RAISE(ABORT, 'rotate key to change role'); END")


_LEGACY_COLUMNS = ('id', 'name', 'token_hash', 'created_at', 'role')


def _column_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    hashes = {name: hashlib.sha256() for name in _LEGACY_COLUMNS}
    for row in conn.execute(f"SELECT {','.join(_LEGACY_COLUMNS)} FROM api_keys ORDER BY id"):
        for name, value in zip(_LEGACY_COLUMNS, row, strict=True):
            hashes[name].update(json.dumps(value, ensure_ascii=False).encode() + b'\n')
    return {name: digest.hexdigest() for name, digest in hashes.items()}


def migrate_grants(conn: sqlite3.Connection) -> None:
    """Rebuild v24 keys inside the migration transaction, retaining IDs and audit references."""
    columns = tuple(row[1] for row in conn.execute('PRAGMA table_xinfo(api_keys)'))
    if columns == (*_LEGACY_COLUMNS, 'kind', 'client', 'revoked_at'):
        return
    if columns != _LEGACY_COLUMNS:
        raise ValueError('api_keys 结构与 v24 不符，无法核对全部旧列')
    before = _column_hashes(conn)
    attached = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name='api_keys' AND type IN ('index','trigger') AND sql IS NOT NULL")]
    conn.execute("""CREATE TABLE api_keys_v25 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, token_hash TEXT UNIQUE, created_at TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'research' CHECK(role IN ('research','assistant','owner')),
        kind TEXT NOT NULL DEFAULT 'pat' CHECK(kind IN ('pat','oauth')),
        client TEXT REFERENCES oauth_clients(client_id), revoked_at TEXT,
        CHECK(kind='pat' AND client IS NULL OR kind='oauth' AND role='assistant' AND token_hash IS NULL AND client IS NOT NULL))""")
    names = ','.join(_LEGACY_COLUMNS)
    conn.execute(f'INSERT INTO api_keys_v25 ({names}) SELECT {names} FROM api_keys')
    # A deleted key may have a higher ID than any surviving row. Preserve even an absent sequence.
    conn.execute("DELETE FROM sqlite_sequence WHERE name='api_keys_v25'")
    conn.execute("INSERT INTO sqlite_sequence(name,seq) SELECT 'api_keys_v25',seq FROM sqlite_sequence WHERE name='api_keys'")
    conn.execute('DROP TABLE api_keys')
    conn.execute('ALTER TABLE api_keys_v25 RENAME TO api_keys')
    for statement in attached:
        conn.execute(statement)
    if _column_hashes(conn) != before:
        raise ValueError('api_keys 旧列哈希不一致，迁移已停止')


def is_active(conn: sqlite3.Connection, key_id: int, role: Role) -> bool:
    """Recheck the grant against the caller's database snapshot, including during a write."""
    if role not in ROLES:
        return False
    return conn.execute("""
        SELECT 1 FROM api_keys k WHERE k.id=? AND k.role=? AND k.revoked_at IS NULL
        AND (k.kind='pat' OR EXISTS (
            SELECT 1 FROM oauth_clients c WHERE c.client_id=k.client AND c.revoked_at IS NULL))
        """, (key_id, role)).fetchone() is not None


def authenticate(conn: sqlite3.Connection, token: str) -> Principal | None:
    """Look up a persistent token without writes; revoked/unknown tokens return None."""
    row = conn.execute("SELECT id, role FROM api_keys WHERE kind='pat' AND token_hash = ?",
                       (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if row is None or not is_active(conn, row[0], row[1]):
        return None
    return Principal(key_id=row[0], role=row[1])


def create(conn, name: str, *, role: Role = "research") -> str:
    if role not in ROLES:
        raise ValueError("未知 Key 类型")
    name = name.strip()
    if not 1 <= len(name) <= 80 or not name.isprintable():
        raise ValueError("Key 名称须为 1–80 个可打印字符")
    token = "asr_" + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO api_keys (name, token_hash, created_at, role) VALUES (?, ?, ?, ?)",
                 (name, hashlib.sha256(token.encode()).hexdigest(), db.now(), role))
    return token


def revoke(conn: sqlite3.Connection, key_id: int) -> bool:
    return conn.execute("UPDATE api_keys SET revoked_at=?, token_hash=NULL WHERE id=? AND revoked_at IS NULL",
                        (db.now(), key_id)).rowcount == 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    create_parser = actions.add_parser("create", help="创建 Key，只输出一次明文")
    create_parser.add_argument("name")
    create_parser.add_argument("--role", choices=ROLES, default="research")
    actions.add_parser("list", help="列出有效 Key 的编号、名称与创建时间")
    actions.add_parser("revoke", help="按编号撤销 Key").add_argument("id", type=int)
    args = parser.parse_args()
    conn = db.connect()
    try:
        if args.action == "create":
            try:
                token = create(conn, args.name, role=args.role)
            except ValueError as exc:
                parser.error(str(exc))
            print(token)
        elif args.action == "list":
            rows = conn.execute("SELECT id, name, created_at, role FROM api_keys WHERE kind='pat' "
                                "AND revoked_at IS NULL ORDER BY id").fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False))
        else:
            if not revoke(conn, args.id):
                parser.error("Key 不存在或已撤销")
            print(json.dumps({"id": args.id, "revoked": True}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
