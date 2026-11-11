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

RESEARCH_ORIGINAL = "u.kind = 'research' AND u.replay_of IS NULL AND NOT u.is_feedback"
FORMAL_REPORT = f"{RESEARCH_ORIGINAL} AND r.passed = 1 AND u.status = 'done'"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    request_id TEXT UNIQUE,
    kind TEXT NOT NULL DEFAULT 'research',  -- research / price / diagnostic (diagnostic never counts as formal research)
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
    status TEXT NOT NULL,  -- queued / screening / researching / selecting / proposed / frozen / tracking / closed / failed
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


def _migrate_v7(conn: sqlite3.Connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS subscriptions (code TEXT PRIMARY KEY, origin TEXT NOT NULL DEFAULT 'external',"
        " added_at TEXT NOT NULL, last_close REAL, last_close_date TEXT)",
        "CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, status TEXT NOT NULL"
        " CHECK(status IN ('queued','running','done','failed')), requested_at TEXT NOT NULL,"
        " started_at TEXT, finished_at TEXT, error TEXT, summary TEXT)",
        "CREATE UNIQUE INDEX IF NOT EXISTS scans_one_active ON scans ((1)) WHERE status IN ('queued','running')",
        "CREATE TABLE IF NOT EXISTS scan_events (id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL REFERENCES scans(id),"
        " code TEXT NOT NULL, doc_key TEXT NOT NULL, title TEXT NOT NULL, announced TEXT NOT NULL, url TEXT NOT NULL,"
        " kind TEXT NOT NULL CHECK(kind IN ('periodic','forecast','major_risk')), severity INTEGER NOT NULL,"
        " detected_at TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('queued_review','deferred','no_card','covered','skipped')),"
        " run_id INTEGER REFERENCES runs(id), UNIQUE(code, doc_key))",
        "CREATE INDEX IF NOT EXISTS scan_events_scan ON scan_events(scan_id)",
    ):
        conn.execute(statement)


def _migrate_v8(conn: sqlite3.Connection) -> None:
    # Nullable JSON snapshots: no rewrite of existing batches or their frozen rules.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}
    for column in ("s10", "sq3", "sl3", "coverage", "small_selection"):
        if column not in existing:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {column} TEXT")


COMPANY_KEY = ("company_key TEXT GENERATED ALWAYS AS (CASE WHEN length(code) IN (6, 9)"
               " AND substr(code, 1, 6) NOT GLOB '*[^0-9]*'"
               " AND (length(code) = 6 OR substr(code, 7) IN ('.SH', '.SZ', '.BJ'))"
               " THEN substr(code, 1, 6) END) VIRTUAL NOT NULL")
_BATCH_NUMBER = "substr(request_id, 7, instr(substr(request_id, 7), '-') - 1)"
# External request ids are only length-checked, so match the whole id: its batch number and company must both fit.
BATCH_ID = (f"batch_id INTEGER GENERATED ALWAYS AS (CASE WHEN request_id GLOB 'batch-[1-9]*-*'"
            f" AND {_BATCH_NUMBER} NOT GLOB '*[^0-9]*'"
            " AND substr(request_id, 7 + instr(substr(request_id, 7), '-')) IN ("
            "substr(code, 1, 6), substr(code, 1, 6) || '.SH', substr(code, 1, 6) || '.SZ', substr(code, 1, 6) || '.BJ')"
            f" THEN CAST({_BATCH_NUMBER} AS INTEGER) END) VIRTUAL")
# Payload columns stay last so metadata reads never walk their overflow pages.
REBUILT = (
    ("runs", "material", "id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE,"
     " kind TEXT NOT NULL DEFAULT 'research', code TEXT NOT NULL, name TEXT, status TEXT NOT NULL DEFAULT 'queued',"
     " step TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, replay_of INTEGER,"
     f" {COMPANY_KEY},"
     " parent_report_id INTEGER GENERATED ALWAYS AS (json_extract(request, '$.parent_report_id')) VIRTUAL,"
     " is_feedback INTEGER GENERATED ALWAYS AS (json_extract(request, '$.feedback') IS NOT NULL) VIRTUAL,"
     f" {BATCH_ID}, request TEXT, identity TEXT, material TEXT"),
    ("reports", "body", "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),"
     " code TEXT NOT NULL, name TEXT, period TEXT NOT NULL, passed INTEGER NOT NULL, verdict TEXT,"
     f" created_at TEXT NOT NULL, body_sha256 TEXT, coverage TEXT, {COMPANY_KEY}, body TEXT NOT NULL"),
    ("calls", "prompt", "id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), step TEXT NOT NULL,"
     " usage TEXT, seconds REAL, error TEXT, created_at TEXT NOT NULL, identity TEXT,"
     " prompt_base INTEGER REFERENCES calls(id), prompt_block TEXT, response TEXT, prompt TEXT NOT NULL"),
)


def _migrate_v9(conn: sqlite3.Connection) -> None:
    for table, payload, columns in REBUILT:
        existing = list(conn.execute(f"PRAGMA table_xinfo({table})"))
        if existing[-1]["name"] == payload:  # already rebuilt; a re-run must be a no-op
            continue
        names = ", ".join(row["name"] for row in existing)
        attached = [row["sql"] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
            (table,))]
        conn.execute(f"CREATE TABLE {table}_v9 ({columns})")
        conn.execute(f"INSERT INTO {table}_v9 ({names}) SELECT {names} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_v9 RENAME TO {table}")
        for sql in attached:
            conn.execute(sql)
    for table in ("prices", "scan_events", "subscriptions"):
        if "company_key" not in {row["name"] for row in conn.execute(f"PRAGMA table_xinfo({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {COMPANY_KEY}")
    for table in ("runs", "reports", "prices", "scan_events"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_by_company_id ON {table}(company_key, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_by_parent_report ON runs(parent_report_id)"
                 " WHERE parent_report_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_by_batch ON runs(batch_id) WHERE batch_id IS NOT NULL")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("foreign key violations after rebuild")


# Account codes also hold non-A-share history (old analyses), so this key is nullable here.
ACCOUNT_COMPANY_KEY = COMPANY_KEY.removesuffix(" NOT NULL")
# Account and analysis-state tables of the former a-stock-agent-skills runtime (policy v0), under their
# original names and column semantics. Until the account cutover they only hold a mirror of the old
# account database (account/mirror.py). Large payload columns stay last.
ACCOUNT_TABLES = {
    "holdings": "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT, cost_price REAL, shares INTEGER,"
                " buy_date TEXT, buy_score INTEGER, stop_loss_15 REAL, stop_loss_20 REAL, notes TEXT, updated_at TEXT,"
                " exit_price REAL, exit_date TEXT, framework TEXT, initial_shares INTEGER, main_entry_date TEXT,"
                " main_entry_basis REAL, additions_since_main REAL NOT NULL DEFAULT 0, reference_cost REAL,"
                f" framework_confident INTEGER NOT NULL DEFAULT 0, {ACCOUNT_COMPANY_KEY}",
    "holding_events": "id INTEGER PRIMARY KEY AUTOINCREMENT, holding_id INTEGER NOT NULL, code TEXT NOT NULL,"
                      " event_type TEXT NOT NULL, event_date TEXT NOT NULL, shares INTEGER, price REAL,"
                      " fees REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0, cash_amount REAL, realized_pnl REAL,"
                      " notes TEXT, inferred INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, report_id INTEGER,"
                      f" {ACCOUNT_COMPANY_KEY},"
                      " CHECK (event_type IN ('buy', 'sell', 'dividend', 'adjustment'))",
    "holding_alerts": "id INTEGER PRIMARY KEY AUTOINCREMENT, holding_id INTEGER NOT NULL, code TEXT NOT NULL,"
                      " level TEXT NOT NULL, category TEXT NOT NULL, reason_code TEXT NOT NULL, reason TEXT NOT NULL,"
                      " status TEXT NOT NULL DEFAULT 'active', evidence TEXT, opened_at TEXT NOT NULL, review_due TEXT,"
                      f" resolved_at TEXT, resolution_evidence TEXT, updated_at TEXT NOT NULL, {ACCOUNT_COMPANY_KEY},"
                      " CHECK (level IN ('yellow', 'red')),"
                      " CHECK (category IN ('holding_deterioration', 'entry_valuation', 'unverified')),"
                      " CHECK (status IN ('active', 'pending', 'resolved'))",
    "holding_l3_conditions":
        "id INTEGER PRIMARY KEY AUTOINCREMENT, holding_id INTEGER NOT NULL, condition_text TEXT NOT NULL,"
        " origin_type TEXT NOT NULL DEFAULT 'original', status TEXT NOT NULL DEFAULT 'pending', evidence TEXT,"
        " as_of TEXT, next_review_date TEXT, temporary_exit_rule TEXT, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, thesis_version_id INTEGER,"
        " is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)), retired_at TEXT, retired_reason TEXT,"
        " condition_scope TEXT NOT NULL DEFAULT 'legacy_unclassified' CHECK(condition_scope IN"
        " ('aggregate','core_driver','non_core','governance','legacy_unclassified')),"
        " action_level TEXT NOT NULL DEFAULT 'legacy_unclassified' CHECK(action_level IN"
        " ('review','reduce','exit','legacy_unclassified')), materiality_basis TEXT,"
        " evidence_cadence TEXT NOT NULL DEFAULT 'unspecified' CHECK(evidence_cadence IN"
        " ('quarterly','semiannual','annual','event','unspecified')),"
        " CHECK (origin_type IN ('original', 'recovered', 'new_monitoring')),"
        " CHECK (status IN ('pending', 'not_triggered', 'watch', 'triggered'))",
    "holding_thesis_versions": "id INTEGER PRIMARY KEY AUTOINCREMENT, holding_id INTEGER NOT NULL,"
                               " version INTEGER NOT NULL, l1 TEXT NOT NULL, l2 TEXT NOT NULL,"
                               " rewrite_reason TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,"
                               " UNIQUE (holding_id, version), CHECK (status IN ('active', 'superseded'))",
    "holding_tier_state": "holding_id INTEGER PRIMARY KEY, tier1_status TEXT NOT NULL DEFAULT 'pending',"
                          " tier2_status TEXT NOT NULL DEFAULT 'pending', tier3_status TEXT NOT NULL DEFAULT 'pending',"
                          " exit_path TEXT, exit_target_pct REAL, exemption_framework TEXT,"
                          " exemption_declared_at TEXT, peg_method TEXT, updated_at TEXT NOT NULL,"
                          " CHECK (tier1_status IN ('pending', 'completed', 'exempted')),"
                          " CHECK (tier2_status IN ('pending', 'completed')),"
                          " CHECK (tier3_status IN ('pending', 'completed'))",
    "retro_notes": "id INTEGER PRIMARY KEY AUTOINCREMENT, holding_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT,"
                   " framework TEXT, buy_date TEXT, exit_date TEXT, buy_score INTEGER, actual_return_pct REAL,"
                   f" holding_days INTEGER, error_tags TEXT, created_at TEXT NOT NULL, {ACCOUNT_COMPANY_KEY},"
                   " thesis_notes TEXT, retro_text TEXT, framework_gap TEXT",
    "analysis_results": "code TEXT, date TEXT, created_at TEXT, name TEXT, score INTEGER,"
                        " return_pct REAL, holding_days INT, framework TEXT, quote_price REAL, quote_as_of TEXT,"
                        f" quote_source TEXT, scoring_status TEXT DEFAULT 'complete', {ACCOUNT_COMPANY_KEY},"
                        " flags TEXT, score_breakdown TEXT, decision_json TEXT, result TEXT, PRIMARY KEY (code, date)",
    "stock_fundamentals": "code TEXT PRIMARY KEY, name TEXT, industry TEXT, updated_at TEXT,"
                          f" ttl_hours INTEGER DEFAULT 24, {ACCOUNT_COMPANY_KEY}, data JSON",
    "quote_snapshots": "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, price REAL NOT NULL,"
                       " quote_date TEXT NOT NULL, quote_time TEXT NOT NULL, fetched_at TEXT NOT NULL,"
                       " source TEXT NOT NULL, verification_sources TEXT NOT NULL, degraded INTEGER NOT NULL DEFAULT 0,"
                       f" valid INTEGER NOT NULL DEFAULT 1, {ACCOUNT_COMPANY_KEY}",
    "qualitative_only_securities": "code TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT NOT NULL,"
                                   f" updated_at TEXT NOT NULL, {ACCOUNT_COMPANY_KEY}",
    "market_indicator_snapshots": "indicator_key TEXT PRIMARY KEY, value REAL NOT NULL, as_of TEXT NOT NULL,"
                                  " fetched_at TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ok'",
}
ACCOUNT_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_active_reason ON holding_alerts(holding_id, reason_code)"
    " WHERE status != 'resolved'",
    "CREATE INDEX IF NOT EXISTS idx_alert_code_status ON holding_alerts(code, status)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_code_created ON analysis_results(code, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_results_date ON analysis_results(date)",
    "CREATE INDEX IF NOT EXISTS idx_holding_events_holding_date ON holding_events(holding_id, event_date, id)",
    "CREATE INDEX IF NOT EXISTS idx_holdings_code ON holdings(code)",
    "CREATE INDEX IF NOT EXISTS idx_holdings_exit ON holdings(exit_date)",
    "CREATE INDEX IF NOT EXISTS idx_l3_holding_status ON holding_l3_conditions(holding_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_quote_snapshots_code_id ON quote_snapshots(code, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_quote_snapshots_code_time ON quote_snapshots(code, fetched_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_retro_framework ON retro_notes(framework)",
    "CREATE INDEX IF NOT EXISTS idx_retro_holding ON retro_notes(holding_id)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_holding_version ON holding_thesis_versions(holding_id, version)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_one_active_per_holding ON holding_thesis_versions(holding_id)"
    " WHERE status='active'",
    "CREATE INDEX IF NOT EXISTS holding_events_by_company_date ON holding_events(company_key, event_date)",
)


def _migrate_v10(conn: sqlite3.Connection) -> None:
    for table, columns in ACCOUNT_TABLES.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
    for statement in ACCOUNT_INDEXES:
        conn.execute(statement)
    # One row: where account state comes from. 'mirror' until the cutover, then 'ledger' for good.
    conn.execute("CREATE TABLE IF NOT EXISTS account_source (id INTEGER PRIMARY KEY CHECK (id = 1),"
                 " mode TEXT NOT NULL CHECK (mode IN ('mirror', 'ledger')), source_sha256 TEXT NOT NULL,"
                 " updated_at TEXT NOT NULL, watermark TEXT NOT NULL)")
    # Append-only pointer to the investment policy in force; the newest row wins.
    conn.execute("CREATE TABLE IF NOT EXISTS policy_activations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " policy TEXT NOT NULL, activated_at TEXT NOT NULL, activated_by TEXT NOT NULL, identity TEXT NOT NULL)")
    for event in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS policy_activations_no_{event.lower()} BEFORE {event}"
                     " ON policy_activations BEGIN SELECT RAISE(ABORT, 'policy_activations is append-only'); END")


def _migrate_v11(conn: sqlite3.Connection) -> None:
    from a_share_research.api_keys import migrate_roles
    migrate_roles(conn)


def _migrate_v12(conn: sqlite3.Connection) -> None:
    from a_share_research.external_research import migrate
    migrate(conn)


def _migrate_v13(conn: sqlite3.Connection) -> None:
    from a_share_research.external_review import migrate
    migrate(conn)


def _migrate_v14(conn: sqlite3.Connection) -> None:
    from a_share_research.account.ledger import migrate
    migrate(conn)


def _migrate_v15(conn: sqlite3.Connection) -> None:
    from a_share_research.account.analysis import migrate
    migrate(conn)


def _migrate_v16(conn: sqlite3.Connection) -> None:
    from a_share_research.account.position_store import migrate
    migrate(conn)


def _migrate_v17(conn: sqlite3.Connection) -> None:
    from a_share_research.account.projection_ids import migrate
    migrate(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS policy_v0_profiles (identity TEXT PRIMARY KEY, created_at TEXT NOT NULL, parameters TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS policy_v0_account_openings (account_id TEXT PRIMARY KEY REFERENCES account_position_books(account_id), profile_identity TEXT NOT NULL REFERENCES policy_v0_profiles(identity), created_at TEXT NOT NULL, evidence_hashes TEXT NOT NULL, source_hash TEXT NOT NULL, snapshot TEXT NOT NULL)")
    for table in ('policy_v0_profiles','policy_v0_account_openings'):
        for operation in ('UPDATE','DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'policy opening is immutable'); END")


MIGRATIONS = (_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7, _migrate_v8,
              _migrate_v9, _migrate_v10, _migrate_v11, _migrate_v12, _migrate_v13, _migrate_v14, _migrate_v15, _migrate_v16, _migrate_v17)


def now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DATA / "app.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        migrated = False
        for version, migrate in enumerate(MIGRATIONS, start=1):
            if conn.execute("PRAGMA user_version").fetchone()[0] < version:
                # Another process may finish the migration while we wait for the write lock.
                with transaction(conn):
                    if conn.execute("PRAGMA user_version").fetchone()[0] < version:
                        migrate(conn)
                        conn.execute(f"PRAGMA user_version = {version}")
                        migrated = True
        conn.execute("PRAGMA foreign_keys=ON")  # A table rebuild cannot run with enforcement on.
        if migrated:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()  # a rebuild leaves a WAL as large as the tables
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True):
    """Commit every statement inside together, or none (connections are in autocommit mode)."""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED")
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


def save_material(conn, run_id: int, snapshot: dict) -> str:
    """Persist snapshots when their SHA changes; skip serialization and writes on a match."""
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
