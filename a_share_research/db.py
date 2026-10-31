"""SQLite storage: runs, model calls, reports and candidate batches."""

import argparse
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
CREATE INDEX IF NOT EXISTS runs_by_status ON runs(status);
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
CREATE INDEX IF NOT EXISTS calls_by_run ON calls(run_id);
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
CREATE INDEX IF NOT EXISTS sessions_by_expires ON sessions(expires_at);
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
CREATE TABLE IF NOT EXISTS prompt_blocks (
    sha256 TEXT PRIMARY KEY,  -- of text; a long block (the research material) shared by several prompts
    text TEXT NOT NULL
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


def _migrate_v4(conn: sqlite3.Connection) -> None:
    # A tool-loop round resends the whole conversation; such a call may store only what it appended
    # to the call named here (see compact_prompt). Older images never set it and still write full prompts.
    conn.execute("ALTER TABLE calls ADD COLUMN prompt_base INTEGER REFERENCES calls(id)")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    # JSON {"sha256", "at", "escaped"}: the prompt_blocks text was cut out of prompt at character
    # offset at (JSON-escaped inside a tool-loop payload when escaped). Older images write full prompts.
    conn.execute("ALTER TABLE calls ADD COLUMN prompt_block TEXT")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS calls_by_run ON calls(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_by_status ON runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS sessions_by_expires ON sessions(expires_at)")


MIGRATIONS = (_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6)


def now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DATA / "app.db"
    path.parent.mkdir(parents=True, exist_ok=True)
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
             seconds=None, error=None, identity=None, base: tuple[int, str] | None = None,
             block: str | None = None) -> int:
    """Record a call; with base=(call id, its full prompt) a tool-loop round stores only its appended messages,
    and a block (the research material) found in what remains is stored once in prompt_blocks."""
    stored, base_id = prompt, None
    if base is not None and (delta := compact_prompt(base[1], prompt)) is not None:
        stored, base_id = delta, base[0]
    stored, block_ref = _cut_block(conn, stored, block)
    cur = conn.execute(
        "INSERT INTO calls (run_id, step, prompt, response, usage, seconds, error, created_at, identity,"
        " prompt_base, prompt_block) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, step, stored, response, json.dumps(usage) if usage else None, seconds, error, now(),
         json.dumps(identity) if identity is not None else None, base_id, block_ref),
    )
    return cur.lastrowid


# Where research prompts put the material, and what may follow it; only used to compact old rows.
BLOCK_HEADINGS = ("\n# 资料\n", "\n# 起始材料\n")
BLOCK_TRAILERS = ("\n\n# 上一次复核输出被拒", "\n# 补证与引用")


def _escaped(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)[1:-1]


def _cut_block(conn: sqlite3.Connection, stored: str, block: str | None) -> tuple[str, str | None]:
    if not block:
        return stored, None
    for escaped, form in ((False, block), (True, _escaped(block))):
        if (at := stored.find(form)) >= 0:
            sha = hashlib.sha256(block.encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO prompt_blocks (sha256, text) VALUES (?, ?)", (sha, block))
            return stored[:at] + stored[at + len(form):], json.dumps({"sha256": sha, "at": at, "escaped": escaped})
    return stored, None


def material_block(prompt: str) -> str | None:
    """The material section of a prompt written before blocks were cut out at call time."""
    text = prompt
    try:
        text = json.loads(prompt)["messages"][0]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    if not isinstance(text, str):
        return None
    for heading in BLOCK_HEADINGS:
        if (start := text.find(heading)) >= 0:
            block = text[start + len(heading):]
            for trailer in BLOCK_TRAILERS:
                block = block.split(trailer, 1)[0]
            return block.rstrip("\n")
    return None


def expand_prompt(base: str, delta: str) -> str:
    old, extra = json.loads(base), json.loads(delta)
    return json.dumps({"messages": old["messages"] + extra["append"], "tools": extra["tools"]}, ensure_ascii=False)


def compact_prompt(base: str, prompt: str) -> str | None:
    """What a tool-loop payload appends to the previous round's, or None unless expanding it gives prompt exactly."""
    try:
        old, new = json.loads(base), json.loads(prompt)
    except ValueError:
        return None
    if not (isinstance(old, dict) and isinstance(new, dict) and old.keys() == new.keys() == {"messages", "tools"}):
        return None
    # A changed earlier message or a different serialization fails the exact round trip.
    delta = json.dumps({"append": new["messages"][len(old["messages"]):], "tools": new["tools"]}, ensure_ascii=False)
    return delta if expand_prompt(base, delta) == prompt else None


def call_prompt(conn: sqlite3.Connection, call_id: int) -> str:
    """The exact prompt a call sent, rebuilt along its chain of appended rounds and shared blocks.

    Works on databases from before v4/v5 too (the integrity check reads pre-release backups)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
    select = ", ".join(c if c in columns else f"NULL AS {c}" for c in ("prompt", "prompt_base", "prompt_block"))
    chain = []
    while call_id is not None:
        row = conn.execute(f"SELECT {select} FROM calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            raise ValueError(f"调用 {call_id} 不存在")
        text, call_id, ref = row
        if ref is not None:
            ref = json.loads(ref)
            block = conn.execute("SELECT text FROM prompt_blocks WHERE sha256 = ?", (ref["sha256"],)).fetchone()
            if block is None:
                raise ValueError(f"共享块 {ref['sha256']} 不存在")
            form = _escaped(block[0]) if ref["escaped"] else block[0]
            text = text[:ref["at"]] + form + text[ref["at"]:]
        chain.append(text)
    prompt = chain.pop()
    while chain:
        prompt = expand_prompt(prompt, chain.pop())
    return prompt


def compact_calls(conn: sqlite3.Connection) -> dict:
    """Re-store earlier full prompts as appended rounds and shared material blocks, one run per transaction,
    each rewritten call verified to rebuild exactly before commit."""
    counts = {"compacted": 0, "bytes_before": 0, "bytes_after": 0}
    for (run_id,) in conn.execute("SELECT DISTINCT run_id FROM calls ORDER BY run_id").fetchall():
        with transaction(conn):
            previous = None
            for call_id, stored, base_id, block_ref in conn.execute(
                    "SELECT id, prompt, prompt_base, prompt_block FROM calls WHERE run_id = ? ORDER BY id",
                    (run_id,)).fetchall():
                prompt = call_prompt(conn, call_id)
                if base_id is None and block_ref is None:
                    new, new_base = prompt, None
                    if previous and (delta := compact_prompt(previous[1], prompt)) is not None:
                        new, new_base = delta, previous[0]
                    new, new_ref = _cut_block(conn, new, material_block(prompt))
                    if new != stored:
                        conn.execute("UPDATE calls SET prompt = ?, prompt_base = ?, prompt_block = ? WHERE id = ?",
                                     (new, new_base, new_ref, call_id))
                        if call_prompt(conn, call_id) != prompt:
                            raise RuntimeError(f"调用 {call_id} 压缩后无法还原原提示，本任务未改动")
                        counts["compacted"] += 1
                        counts["bytes_before"] += len(prompt.encode())
                        counts["bytes_after"] += len(new.encode())
                previous = (call_id, prompt)
    return counts


def save_material(conn, run_id: int, snapshot: dict) -> str:
    """Persist changed snapshots (including trace/stops), repairing the identity hash if needed."""
    sha = snapshot["sha256"]
    # Avoid serializing and binding the full snapshot on unchanged tool rounds.
    row = conn.execute("SELECT json_extract(identity, '$.material_sha256') FROM runs WHERE id = ?",
                       (run_id,)).fetchone()
    if row is None or row[0] == sha:
        return sha
    text = json.dumps(snapshot, ensure_ascii=False)
    conn.execute(
        "UPDATE runs SET material = ?, identity = json_set(coalesce(identity, '{}'), '$.material_sha256', ?)"
        " WHERE id = ? AND coalesce(json_extract(identity, '$.material_sha256'), '') != ?",
        (text, sha, run_id, sha),
    )
    return sha


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Database maintenance")
    parser.add_argument("command", choices=["compact-calls"])
    parser.add_argument("--vacuum", action="store_true", help="reclaim the freed pages afterwards")
    args = parser.parse_args()
    conn = connect()
    print(json.dumps(compact_calls(conn)))
    if args.vacuum:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # in WAL mode VACUUM leaves the whole file in the log
    conn.close()


if __name__ == "__main__":
    main()
