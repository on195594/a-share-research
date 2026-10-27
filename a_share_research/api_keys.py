"""Manage persistent research API keys locally; only creation prints the secret."""

import argparse
import hashlib
import json
import secrets

from a_share_research import db


def create(conn, name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 80 or not name.isprintable():
        raise ValueError("Key 名称须为 1–80 个可打印字符")
    token = "asr_" + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO api_keys (name, token_hash, created_at) VALUES (?, ?, ?)",
                 (name, hashlib.sha256(token.encode()).hexdigest(), db.now()))
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("create", help="创建 Key，只输出一次明文").add_argument("name")
    actions.add_parser("list", help="列出有效 Key 的编号、名称与创建时间")
    actions.add_parser("revoke", help="按编号撤销 Key").add_argument("id", type=int)
    args = parser.parse_args()
    conn = db.connect()
    try:
        if args.action == "create":
            try:
                token = create(conn, args.name)
            except ValueError as exc:
                parser.error(str(exc))
            print(token)
        elif args.action == "list":
            rows = conn.execute("SELECT id, name, created_at FROM api_keys ORDER BY id").fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False))
        else:
            if not conn.execute("DELETE FROM api_keys WHERE id = ?", (args.id,)).rowcount:
                parser.error("Key 不存在或已撤销")
            print(json.dumps({"id": args.id, "revoked": True}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
