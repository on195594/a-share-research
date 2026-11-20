"""SQLite storage for preregistered clients and opaque OAuth credentials."""

import hashlib
import json
import sqlite3
import time
from typing import Literal

from a_share_research import db


TokenKind = Literal["access", "refresh"]


def create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id TEXT NOT NULL PRIMARY KEY, name TEXT NOT NULL,
        created_at TEXT NOT NULL, revoked_at TEXT, redirect_uris TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS oauth_codes (
        code_hash TEXT NOT NULL PRIMARY KEY CHECK (
            length(code_hash)=64 AND code_hash NOT GLOB '*[^0-9a-f]*'),
        grant_id INTEGER NOT NULL REFERENCES api_keys(id),
        client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
        expires_at REAL NOT NULL, used_at REAL, code_challenge TEXT NOT NULL,
        resource TEXT NOT NULL, redirect_uri TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS oauth_tokens (
        token_hash TEXT NOT NULL PRIMARY KEY CHECK (
            length(token_hash)=64 AND token_hash NOT GLOB '*[^0-9a-f]*'),
        grant_id INTEGER NOT NULL REFERENCES api_keys(id),
        kind TEXT NOT NULL CHECK(kind IN ('access','refresh')),
        expires_at REAL NOT NULL, revoked_at TEXT, resource TEXT NOT NULL)""")


def register_client(conn: sqlite3.Connection, client_id: str, name: str, redirect_uris: list[str]) -> None:
    conn.execute("INSERT INTO oauth_clients(client_id,name,created_at,redirect_uris) VALUES(?,?,?,?)",
                 (client_id, name, db.now(), json.dumps(redirect_uris)))


def get_client(conn: sqlite3.Connection, client_id: str) -> dict | None:
    row = conn.execute("SELECT name,redirect_uris FROM oauth_clients WHERE client_id=? AND revoked_at IS NULL",
                       (client_id,)).fetchone()
    if row is None:
        return None
    return dict(client_id=client_id, name=row[0], redirect_uris=json.loads(row[1]))


def create_grant(conn: sqlite3.Connection, client_id: str) -> int:
    row = conn.execute("""
        INSERT INTO api_keys(name,created_at,role,kind,client)
        SELECT name,?,'assistant','oauth',client_id FROM oauth_clients WHERE client_id=? AND revoked_at IS NULL
        RETURNING id""", (db.now(), client_id)).fetchone()
    if row is None:
        raise ValueError("OAuth 客户端未登记或已撤销")
    return row[0]


def revoke_client(conn: sqlite3.Connection, client_id: str) -> bool:
    return bool(conn.execute("UPDATE oauth_clients SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL",
                             (db.now(), client_id)).rowcount)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# Scope every credential operation to its live grant and registered client in the same statement.
_ACTIVE_GRANTS = """
    SELECT g.id, c.client_id, c.redirect_uris FROM api_keys g
    JOIN oauth_clients c ON c.client_id=g.client
    WHERE g.kind='oauth' AND g.revoked_at IS NULL AND c.revoked_at IS NULL
"""


def save_code(conn: sqlite3.Connection, code: str, *, grant_id: int, client_id: str, redirect_uri: str,
              resource: str, code_challenge: str, expires_at: float) -> None:
    result = conn.execute(f"""
        INSERT INTO oauth_codes(code_hash,grant_id,client_id,redirect_uri,resource,code_challenge,expires_at)
        SELECT ?,id,client_id,?,?,?,? FROM ({_ACTIVE_GRANTS})
        WHERE id=? AND client_id=? AND ? IN (SELECT value FROM json_each(redirect_uris))""",
        (_digest(code), redirect_uri, resource, code_challenge, expires_at, grant_id, client_id, redirect_uri))
    if not result.rowcount:
        raise ValueError("OAuth 授权无效或回调地址未登记")


def consume_code(conn: sqlite3.Connection, code: str, *, client_id: str, redirect_uri: str, resource: str,
                 code_challenge: str) -> int | None:
    """Consume once, using the S256 challenge derived by the caller from the submitted verifier."""
    now = time.time()
    row = conn.execute(f"""
        UPDATE oauth_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL AND expires_at>?
        AND client_id=? AND redirect_uri=? AND resource=? AND code_challenge=?
        AND grant_id IN (SELECT id FROM ({_ACTIVE_GRANTS}) WHERE client_id=?)
        RETURNING grant_id""",
        (now, _digest(code), now, client_id, redirect_uri, resource, code_challenge, client_id)).fetchone()
    return row[0] if row else None


def save_token(conn: sqlite3.Connection, token: str, *, grant_id: int, kind: TokenKind,
               resource: str, expires_at: float) -> None:
    result = conn.execute(f"""
        INSERT INTO oauth_tokens(token_hash,grant_id,kind,resource,expires_at)
        SELECT ?,id,?,?,? FROM ({_ACTIVE_GRANTS}) WHERE id=?""",
        (_digest(token), kind, resource, expires_at, grant_id))
    if not result.rowcount:
        raise ValueError("OAuth 授权无效")


def token_grant(conn: sqlite3.Connection, token: str, *, kind: TokenKind, resource: str) -> int | None:
    row = conn.execute(f"""
        SELECT t.grant_id FROM oauth_tokens t JOIN ({_ACTIVE_GRANTS}) g ON g.id=t.grant_id
        WHERE t.token_hash=? AND t.kind=? AND t.resource=? AND t.revoked_at IS NULL AND t.expires_at>?""",
        (_digest(token), kind, resource, time.time())).fetchone()
    return row[0] if row else None


def revoke_token(conn: sqlite3.Connection, token: str) -> bool:
    return bool(conn.execute("UPDATE oauth_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                             (db.now(), _digest(token))).rowcount)
