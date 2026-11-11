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


def authenticate(conn: sqlite3.Connection, token: str) -> Principal | None:
    """Look up a persistent token without writes; revoked/unknown tokens return None."""
    row = conn.execute("SELECT id, role FROM api_keys WHERE token_hash = ?",
                       (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if row is None or row[1] not in ROLES:
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
            rows = conn.execute("SELECT id, name, created_at, role FROM api_keys ORDER BY id").fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False))
        else:
            if not conn.execute("DELETE FROM api_keys WHERE id = ?", (args.id,)).rowcount:
                parser.error("Key 不存在或已撤销")
            print(json.dumps({"id": args.id, "revoked": True}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
