"""SQLite storage: runs, model calls and reports."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
DATA = Path(os.environ.get("DATA_DIR", "data"))  # production keeps data outside the code checkout

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    request_id TEXT UNIQUE,
    kind TEXT NOT NULL DEFAULT 'research',  -- research / price
    code TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued / running / done / failed
    step TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    identity TEXT,
    material TEXT,
    request TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    step TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT,
    usage TEXT,
    seconds REAL,
    error TEXT,
    created_at TEXT NOT NULL,
    identity TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),
    code TEXT NOT NULL,
    name TEXT,
    period TEXT NOT NULL,
    passed INTEGER NOT NULL,  -- 1: reviewed report; 0: only verified facts are shown
    verdict TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_by_code ON reports(code, created_at);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),
    code TEXT NOT NULL,
    name TEXT,
    as_of TEXT NOT NULL,
    expires TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prices_by_code ON prices(code, id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
"""


def now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DATA / "app.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Shared by the web threadpool; sqlite3 serializes access to one connection.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrations = [("runs", ("identity", "material", "request")), ("calls", ("identity",))]
    if any(name not in {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")} for t, names in migrations for name in names):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, names in migrations:
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name in names:
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            conn.close()
            raise
    return conn


def create_run(conn: sqlite3.Connection, code: str, request_id: str | None = None,
               kind: str = "research", name: str | None = None, status: str = "queued") -> int:
    """Insert a queued run; a repeated request_id returns the existing run."""
    if request_id:
        row = conn.execute("SELECT id FROM runs WHERE request_id = ?", (request_id,)).fetchone()
        if row:
            return row["id"]
    cur = conn.execute(
        "INSERT INTO runs (request_id, kind, code, name, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (request_id, kind, code, name, status, now()),
    )
    return cur.lastrowid


def update_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))


def log_call(conn, run_id: int, step: str, prompt: str, *, response=None, usage=None,
             seconds=None, error=None, identity=None) -> None:
    conn.execute(
        "INSERT INTO calls (run_id, step, prompt, response, usage, seconds, error, created_at, identity)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, step, prompt, response, json.dumps(usage) if usage else None, seconds, error, now(),
         json.dumps(identity) if identity is not None else None),
    )


def save_report(conn, run_id: int, code: str, name: str, period: str, passed: bool,
                verdict: str | None, body: dict) -> None:
    conn.execute(
        "INSERT INTO reports (run_id, code, name, period, passed, verdict, body, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, code, name, period, int(passed), verdict, json.dumps(body, ensure_ascii=False), now()),
    )
