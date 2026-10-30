"""SQLite storage: runs, model calls, reports and candidate batches."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
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
    created_at TEXT NOT NULL,
    body_sha256 TEXT  -- sha256 of body as written; NULL for reports saved before it was recorded
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
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    request_id TEXT UNIQUE,
    status TEXT NOT NULL,  -- queued / screening / researching / selecting / proposed / frozen / tracking / failed
    step TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    as_of TEXT,  -- screened trading day, YYYYMMDD
    rule TEXT NOT NULL,  -- JSON: frozen rule parameters
    universe TEXT,  -- JSON: funnel, gaps and every screened company's factors
    q20 TEXT,  -- JSON: ranked candidates with linked run/report ids
    q5 TEXT,  -- JSON: mechanical top five codes
    selection TEXT,  -- JSON: model comparison draft and its identity
    l5 TEXT,  -- JSON: owner's frozen picks
    frozen_at TEXT,
    entry TEXT,  -- JSON: entry day prices and benchmarks
    marks TEXT  -- JSON: latest valuation and horizon returns
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_progress (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    baseline_id INTEGER NOT NULL REFERENCES reports(id),
    current_id INTEGER NOT NULL REFERENCES reports(id),
    input_hashes TEXT NOT NULL,
    status TEXT NOT NULL,
    body TEXT,
    review TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS progress_calls (
    id INTEGER PRIMARY KEY,
    progress_id INTEGER NOT NULL REFERENCES research_progress(id),
    stage TEXT NOT NULL CHECK(stage IN ('generate', 'review')),
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT,
    identity TEXT,
    usage TEXT,
    seconds REAL,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(progress_id, stage)
);
"""


# A report's financial coverage: the later of its period and its newest T.* table period. Reports
# saved before period tracked the table recorded only the base document's period (e.g. a prior
# annual report while the table already held the half year). Expects the reports table aliased r.
COVERAGE = ("MAX(r.period, COALESCE((SELECT MAX(v.key) FROM json_each(r.body, '$.table') t,"
            " json_each(t.value, '$.values') v WHERE json_extract(t.value, '$.id') LIKE 'T.%'), ''))")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    for table, names in (("runs", ("identity", "material", "request")),
                         ("calls", ("identity",)), ("reports", ("body_sha256",))):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name in names:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE runs ADD COLUMN replay_of INTEGER")
    conn.execute("ALTER TABLE reports ADD COLUMN coverage TEXT")
    conn.execute("UPDATE runs SET replay_of = json_extract(identity, '$.replay_of')")
    conn.execute(f"UPDATE reports AS r SET coverage = {COVERAGE}")
    conn.execute("CREATE INDEX reports_by_code_coverage ON reports(code, coverage)")
    conn.execute("CREATE INDEX runs_by_replay_of ON runs(replay_of)")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    for event, suffix in (("INSERT", "ai"), ("UPDATE OF identity", "au")):
        condition = " WHEN NEW.identity IS NOT NULL" if event == "INSERT" else ""
        conn.execute(f"CREATE TRIGGER runs_replay_of_{suffix} AFTER {event} ON runs{condition} BEGIN"
                     " UPDATE runs SET replay_of = json_extract(NEW.identity, '$.replay_of')"
                     " WHERE id = NEW.id; END")
    for event, suffix in (("INSERT", "ai"), ("UPDATE OF body, period", "au")):
        conn.execute(f"CREATE TRIGGER reports_coverage_{suffix} AFTER {event} ON reports BEGIN"
                     f" UPDATE reports SET coverage = (SELECT {COVERAGE} FROM reports r WHERE r.id = NEW.id)"
                     " WHERE id = NEW.id; END")
    conn.execute("UPDATE runs SET replay_of = json_extract(identity, '$.replay_of')")
    conn.execute(f"UPDATE reports AS r SET coverage = {COVERAGE}")


MIGRATIONS = (_migrate_v1, _migrate_v2, _migrate_v3)


def now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DATA / "app.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Shared by the web threadpool; sqlite3 serializes access to one connection.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        for version, migrate in enumerate(MIGRATIONS, start=1):
            if conn.execute("PRAGMA user_version").fetchone()[0] < version:
                # Another process may finish the migration while we wait for the write lock.
                with transaction(conn):
                    if conn.execute("PRAGMA user_version").fetchone()[0] < version:
                        migrate(conn)
                        conn.execute(f"PRAGMA user_version = {version}")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Commit every statement inside together, or none (connections are in autocommit mode)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


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


def save_material(conn, run_id: int, snapshot: dict) -> None:
    """Store a material snapshot and keep the run identity's hash of it in step."""
    conn.execute(
        "UPDATE runs SET material = ?, identity = json_set(coalesce(identity, '{}'), '$.material_sha256', ?)"
        " WHERE id = ?",
        (json.dumps(snapshot, ensure_ascii=False), snapshot["sha256"], run_id),
    )


def save_report(conn, run_id: int, code: str, name: str, period: str, passed: bool,
                verdict: str | None, body: dict) -> None:
    text = json.dumps(body, ensure_ascii=False)
    conn.execute(
        "INSERT INTO reports (run_id, code, name, period, passed, verdict, body, created_at, body_sha256)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, code, name, period, int(passed), verdict, text, now(), hashlib.sha256(text.encode()).hexdigest()),
    )


def update_batch(conn: sqlite3.Connection, batch_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE batches SET {sets} WHERE id = ?", (*fields.values(), batch_id))
