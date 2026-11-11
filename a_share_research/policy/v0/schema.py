"""SQLite schema bootstrap and released migrations."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from a_share_research.policy.v0 import domain
from a_share_research.policy.v0.schema_ledger import bootstrap_schema

logger = logging.getLogger(__name__)


def _safe_json_value(raw: str | None, expected_type: type, default):
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, json.JSONDecodeError):
        return default
    return value if isinstance(value, expected_type) else default


SCHEMA_MIGRATIONS = [
    ("002-analysis-name", "ALTER TABLE analysis_results ADD COLUMN name TEXT"),
    ("003-analysis-score", "ALTER TABLE analysis_results ADD COLUMN score INTEGER"),
    ("004-analysis-flags", "ALTER TABLE analysis_results ADD COLUMN flags TEXT"),
    (
        "005-analysis-score-breakdown",
        "ALTER TABLE analysis_results ADD COLUMN score_breakdown TEXT",
    ),
    (
        "006-analysis-return-pct",
        "ALTER TABLE analysis_results ADD COLUMN return_pct REAL",
    ),
    (
        "007-analysis-holding-days",
        "ALTER TABLE analysis_results ADD COLUMN holding_days INT",
    ),
    ("008-holdings-exit-price", "ALTER TABLE holdings ADD COLUMN exit_price REAL"),
    ("009-holdings-exit-date", "ALTER TABLE holdings ADD COLUMN exit_date TEXT"),
    (
        "010-analysis-framework",
        "ALTER TABLE analysis_results ADD COLUMN framework TEXT",
    ),
    (
        "011-analysis-quote-price",
        "ALTER TABLE analysis_results ADD COLUMN quote_price REAL",
    ),
    (
        "012-analysis-quote-as-of",
        "ALTER TABLE analysis_results ADD COLUMN quote_as_of TEXT",
    ),
    (
        "013-analysis-quote-source",
        "ALTER TABLE analysis_results ADD COLUMN quote_source TEXT",
    ),
    (
        "014-analysis-scoring-status",
        "ALTER TABLE analysis_results ADD COLUMN scoring_status TEXT DEFAULT 'complete'",
    ),
    ("015-holdings-framework", "ALTER TABLE holdings ADD COLUMN framework TEXT"),
    (
        "016-holdings-initial-shares",
        "ALTER TABLE holdings ADD COLUMN initial_shares INTEGER",
    ),
    (
        "017-holdings-main-entry-date",
        "ALTER TABLE holdings ADD COLUMN main_entry_date TEXT",
    ),
    (
        "018-holdings-main-entry-basis",
        "ALTER TABLE holdings ADD COLUMN main_entry_basis REAL",
    ),
    (
        "019-holdings-additions-since-main",
        "ALTER TABLE holdings ADD COLUMN additions_since_main REAL NOT NULL DEFAULT 0",
    ),
    (
        "020-holdings-reference-cost",
        "ALTER TABLE holdings ADD COLUMN reference_cost REAL",
    ),
    (
        "021-holdings-framework-confident",
        "ALTER TABLE holdings ADD COLUMN framework_confident INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "022-holding-events-inferred",
        "ALTER TABLE holding_events ADD COLUMN inferred INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "027-l3-thesis-version-id",
        "ALTER TABLE holding_l3_conditions ADD COLUMN thesis_version_id INTEGER",
    ),
    (
        "028-l3-is-active",
        "ALTER TABLE holding_l3_conditions ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))",
    ),
    (
        "029-l3-retired-at",
        "ALTER TABLE holding_l3_conditions ADD COLUMN retired_at TEXT",
    ),
    (
        "030-l3-retired-reason",
        "ALTER TABLE holding_l3_conditions ADD COLUMN retired_reason TEXT",
    ),
    (
        "031-l3-condition-scope",
        "ALTER TABLE holding_l3_conditions ADD COLUMN condition_scope TEXT NOT NULL DEFAULT 'legacy_unclassified' CHECK(condition_scope IN ('aggregate','core_driver','non_core','governance','legacy_unclassified'))",
    ),
    (
        "032-l3-action-level",
        "ALTER TABLE holding_l3_conditions ADD COLUMN action_level TEXT NOT NULL DEFAULT 'legacy_unclassified' CHECK(action_level IN ('review','reduce','exit','legacy_unclassified'))",
    ),
    (
        "033-l3-materiality-basis",
        "ALTER TABLE holding_l3_conditions ADD COLUMN materiality_basis TEXT",
    ),
    (
        "035-analysis-decision-json",
        "ALTER TABLE analysis_results ADD COLUMN decision_json TEXT",
    ),
    (
        "036-l3-evidence-cadence",
        "ALTER TABLE holding_l3_conditions ADD COLUMN evidence_cadence TEXT NOT NULL DEFAULT 'unspecified' CHECK(evidence_cadence IN ('quarterly','semiannual','annual','event','unspecified'))",
    ),
    (
        "037-holding-events-report-id",
        "ALTER TABLE holding_events ADD COLUMN report_id INTEGER",
    ),
]


def apply_column_migration(conn: sqlite3.Connection, sql: str) -> None:
    """Add one column, ignoring only the already-applied duplicate-column case.

    Swallowing ``duplicate column name`` is the recovery mechanism, not a
    convenience.  Python's sqlite3 opens an implicit transaction only before DML,
    so this DDL autocommits immediately while the ledger INSERT that records it
    does not.  A batch that dies midway therefore leaves columns on disk that the
    ledger still reports as pending, and the next run replays them; this branch is
    what makes that replay idempotent.  Do not "tidy" it away, and do not add a
    commit here -- the ledger owns commits.
    """
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return
        logger.exception("Schema migration failed: %s", sql)
        raise


def _create_thesis_version_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS holding_thesis_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holding_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        l1 TEXT NOT NULL,
        l2 TEXT NOT NULL,
        rewrite_reason TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (holding_id, version),
        CHECK (status IN ('active', 'superseded'))
    )""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_one_active_per_holding "
        "ON holding_thesis_versions(holding_id) WHERE status='active'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thesis_holding_version "
        "ON holding_thesis_versions(holding_id, version)"
    )


def _create_core_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_fundamentals (
        code TEXT PRIMARY KEY,
        name TEXT,
        industry TEXT,
        data JSON,
        updated_at TEXT,
        ttl_hours INTEGER DEFAULT 24
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS analysis_results (
        code TEXT,
        date TEXT,
        name TEXT,
        result TEXT,
        created_at TEXT,
        score INTEGER,
        flags TEXT,
        score_breakdown TEXT,
        return_pct REAL,
        holding_days INT,
        framework TEXT,
        quote_price REAL,
        quote_as_of TEXT,
        quote_source TEXT,
        scoring_status TEXT DEFAULT 'complete',
        decision_json TEXT,
        PRIMARY KEY (code, date)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        name TEXT,
        cost_price REAL,
        shares INTEGER,
        buy_date TEXT,
        buy_score INTEGER,
        stop_loss_15 REAL,
        stop_loss_20 REAL,
        notes TEXT,
        updated_at TEXT,
        exit_price REAL,
        exit_date TEXT,
        framework TEXT,
        initial_shares INTEGER,
        main_entry_date TEXT,
        main_entry_basis REAL,
        additions_since_main REAL NOT NULL DEFAULT 0,
        reference_cost REAL,
        framework_confident INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS holding_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holding_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_date TEXT NOT NULL,
        shares INTEGER,
        price REAL,
        fees REAL NOT NULL DEFAULT 0,
        tax REAL NOT NULL DEFAULT 0,
        cash_amount REAL,
        realized_pnl REAL,
        notes TEXT,
        inferred INTEGER NOT NULL DEFAULT 0,
        report_id INTEGER,
        created_at TEXT NOT NULL,
        CHECK (event_type IN ('buy', 'sell', 'dividend', 'adjustment'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS holding_l3_conditions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holding_id INTEGER NOT NULL,
        condition_text TEXT NOT NULL,
        origin_type TEXT NOT NULL DEFAULT 'original',
        status TEXT NOT NULL DEFAULT 'pending',
        evidence TEXT,
        as_of TEXT,
        next_review_date TEXT,
        temporary_exit_rule TEXT,
        thesis_version_id INTEGER,
        is_active INTEGER NOT NULL DEFAULT 1,
        retired_at TEXT,
        retired_reason TEXT,
        condition_scope TEXT NOT NULL DEFAULT 'legacy_unclassified',
        action_level TEXT NOT NULL DEFAULT 'legacy_unclassified',
        materiality_basis TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (origin_type IN ('original', 'recovered', 'new_monitoring')),
        CHECK (status IN ('pending', 'not_triggered', 'watch', 'triggered')),
        CHECK (is_active IN (0,1)),
        CHECK (condition_scope IN ('aggregate','core_driver','non_core','governance','legacy_unclassified')),
        CHECK (action_level IN ('review','reduce','exit','legacy_unclassified'))
    )""")
    _create_thesis_version_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS holding_tier_state (
        holding_id INTEGER PRIMARY KEY,
        tier1_status TEXT NOT NULL DEFAULT 'pending',
        tier2_status TEXT NOT NULL DEFAULT 'pending',
        tier3_status TEXT NOT NULL DEFAULT 'pending',
        exit_path TEXT,
        exit_target_pct REAL,
        exemption_framework TEXT,
        exemption_declared_at TEXT,
        peg_method TEXT,
        updated_at TEXT NOT NULL,
        CHECK (tier1_status IN ('pending', 'completed', 'exempted')),
        CHECK (tier2_status IN ('pending', 'completed')),
        CHECK (tier3_status IN ('pending', 'completed'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS holding_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holding_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        level TEXT NOT NULL,
        category TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        evidence TEXT,
        opened_at TEXT NOT NULL,
        review_due TEXT,
        resolved_at TEXT,
        resolution_evidence TEXT,
        updated_at TEXT NOT NULL,
        CHECK (level IN ('yellow', 'red')),
        CHECK (category IN ('holding_deterioration', 'entry_valuation', 'unverified')),
        CHECK (status IN ('active', 'pending', 'resolved'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS retro_notes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        holding_id      INTEGER NOT NULL,
        code            TEXT NOT NULL,
        name            TEXT,
        framework       TEXT,
        buy_date        TEXT,
        exit_date       TEXT,
        buy_score       INTEGER,
        actual_return_pct REAL,
        holding_days    INTEGER,
        error_tags      TEXT,
        thesis_notes    TEXT,
        retro_text      TEXT,
        framework_gap   TEXT,
        created_at      TEXT NOT NULL
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_retro_holding ON retro_notes(holding_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_retro_framework ON retro_notes(framework)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_holding_events_holding_date "
        "ON holding_events(holding_id, event_date, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_l3_holding_status "
        "ON holding_l3_conditions(holding_id, status)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_active_reason "
        "ON holding_alerts(holding_id, reason_code) WHERE status != 'resolved'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_code_status "
        "ON holding_alerts(code, status)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS quote_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        price REAL NOT NULL,
        quote_date TEXT NOT NULL,
        quote_time TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        source TEXT NOT NULL,
        verification_sources TEXT NOT NULL,
        degraded INTEGER NOT NULL DEFAULT 0,
        valid INTEGER NOT NULL DEFAULT 1
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quote_snapshots_code_time "
        "ON quote_snapshots(code, fetched_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quote_snapshots_code_id "
        "ON quote_snapshots(code, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_results_code_created "
        "ON analysis_results(code, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_results_date ON analysis_results(date)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS market_indicator_snapshots (
        indicator_key TEXT PRIMARY KEY,
        value REAL NOT NULL,
        as_of TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS qualitative_only_securities (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        industry TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")


def _migrate_holdings_autoincrement_id(conn: sqlite3.Connection) -> None:
    holdings_cols = [
        r[1] for r in conn.execute("PRAGMA table_info(holdings)").fetchall()
    ]
    if "id" not in holdings_cols:
        conn.execute("DROP TABLE IF EXISTS holdings_new")
        conn.execute("""CREATE TABLE holdings_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT, cost_price REAL, shares INTEGER, buy_date TEXT,
            buy_score INTEGER, stop_loss_15 REAL, stop_loss_20 REAL,
            notes TEXT, updated_at TEXT, exit_price REAL, exit_date TEXT,
            framework TEXT, initial_shares INTEGER, main_entry_date TEXT,
            main_entry_basis REAL, additions_since_main REAL NOT NULL DEFAULT 0,
            reference_cost REAL, framework_confident INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""INSERT INTO holdings_new
            (code, name, cost_price, shares, buy_date, buy_score,
             stop_loss_15, stop_loss_20, notes, updated_at, exit_price, exit_date,
             framework, initial_shares, main_entry_date, main_entry_basis,
             additions_since_main, reference_cost, framework_confident)
            SELECT code, name, cost_price, shares, buy_date, buy_score,
                   stop_loss_15, stop_loss_20, notes, updated_at, exit_price, exit_date,
                   framework, initial_shares, main_entry_date, main_entry_basis,
                   additions_since_main, reference_cost, framework_confident
            FROM holdings""")
        conn.execute("DROP TABLE holdings")
        conn.execute("ALTER TABLE holdings_new RENAME TO holdings")
        conn.commit()


def _ensure_holdings_indices(conn: sqlite3.Connection) -> None:
    indices = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name IN ('idx_holdings_code', 'idx_holdings_exit')"
    ).fetchall()
    if len(indices) < 2:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_holdings_code ON holdings(code)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_holdings_exit ON holdings(exit_date)"
        )
        conn.commit()


def backfill_holding_metadata(conn: sqlite3.Connection) -> None:
    """Populate durable position metadata for legacy rows without changing later choices."""
    from a_share_research.policy.v0 import db

    if db.is_read_only():
        return
    conn.execute(
        "UPDATE holdings SET initial_shares=shares "
        "WHERE initial_shares IS NULL AND shares IS NOT NULL"
    )
    conn.execute(
        """UPDATE holdings
           SET main_entry_date=COALESCE(main_entry_date, buy_date),
               main_entry_basis=COALESCE(main_entry_basis, cost_price * shares),
               reference_cost=COALESCE(reference_cost, cost_price)
           WHERE shares IS NOT NULL"""
    )
    conn.execute(
        """UPDATE holdings
           SET framework=(
               SELECT a.framework FROM analysis_results a
               WHERE a.code=holdings.code AND a.framework IS NOT NULL
               ORDER BY a.date DESC LIMIT 1
           )
           WHERE framework IS NULL"""
    )
    conn.execute(
        """UPDATE holdings
           SET framework_confident=1
           WHERE framework IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM analysis_results a
               WHERE a.code=holdings.code AND a.framework=holdings.framework
             )"""
    )
    legacy_rows = conn.execute(
        """SELECT h.id, h.code, h.cost_price, h.shares, h.buy_date,
                  h.exit_price, h.exit_date
           FROM holdings h
           WHERE h.shares IS NOT NULL AND h.shares > 0
             AND NOT EXISTS (
               SELECT 1 FROM holding_events e WHERE e.holding_id=h.id
             )"""
    ).fetchall()
    for holding_id, code, cost, shares, buy_date, exit_price, exit_date in legacy_rows:
        created_at = domain.utc_now_iso()
        conn.execute(
            """INSERT INTO holding_events
               (holding_id, code, event_type, event_date, shares, price,
                notes, inferred, created_at)
               VALUES (?, ?, 'buy', ?, ?, ?, 'legacy_inferred_from_holding', 1, ?)""",
            (
                holding_id,
                code,
                buy_date or domain.cst_today(),
                shares,
                cost,
                created_at,
            ),
        )
        if exit_date and exit_price:
            conn.execute(
                """INSERT INTO holding_events
                   (holding_id, code, event_type, event_date, shares, price,
                    realized_pnl, notes, inferred, created_at)
                   VALUES (?, ?, 'sell', ?, ?, ?, ?,
                           'legacy_inferred_from_holding', 1, ?)""",
                (
                    holding_id,
                    code,
                    exit_date,
                    shares,
                    exit_price,
                    (exit_price - cost) * shares,
                    created_at,
                ),
            )
    conn.commit()


def backfill_legacy_alerts(conn: sqlite3.Connection) -> None:
    """Carry legacy flag JSON forward as pending/unverified without risk-count inflation."""
    rows = conn.execute(
        """SELECT h.id, h.code, (
               SELECT latest.flags
               FROM analysis_results latest
               WHERE latest.code=h.code
               ORDER BY latest.date DESC
               LIMIT 1
           )
           FROM holdings h
           WHERE h.exit_date IS NULL"""
    ).fetchall()
    for holding_id, code, raw_flags in rows:
        flags = _safe_json_value(raw_flags, list, [])
        latest_reasons = {
            str(flag.get("reason") or "").strip()
            for flag in flags
            if isinstance(flag, dict) and str(flag.get("reason") or "").strip()
        }
        migrated_alerts = conn.execute(
            """SELECT id, reason FROM holding_alerts
               WHERE holding_id=? AND category='unverified' AND status='pending'
                 AND reason_code LIKE 'legacy-%'
                 AND evidence='legacy analysis_results.flags; requires classification' """,
            (holding_id,),
        ).fetchall()
        now_iso = domain.utc_now_iso()
        for alert_id, reason in migrated_alerts:
            if reason not in latest_reasons:
                conn.execute(
                    """UPDATE holding_alerts
                       SET status='resolved', resolved_at=?,
                           resolution_evidence='superseded by latest analysis',
                           updated_at=?
                       WHERE id=?""",
                    (now_iso, now_iso, alert_id),
                )
        for flag in flags:
            if not isinstance(flag, dict):
                continue
            level = flag.get("level")
            reason = str(flag.get("reason") or "").strip()
            if level not in ("yellow", "red") or not reason:
                continue
            reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
            opened_at = str(flag.get("date") or domain.cst_today())
            conn.execute(
                """INSERT OR IGNORE INTO holding_alerts
                   (holding_id, code, level, category, reason_code, reason, status,
                    evidence, opened_at, updated_at)
                   VALUES (?, ?, ?, 'unverified', ?, ?, 'pending',
                           'legacy analysis_results.flags; requires classification',
                           ?, ?)""",
                (
                    holding_id,
                    code,
                    level,
                    f"legacy-{reason_hash}",
                    reason,
                    opened_at,
                    now_iso,
                ),
            )
    conn.commit()


def bootstrap_database_schema(conn: sqlite3.Connection) -> None:
    """Upgrade missing schema items and record each one durably.

    ``_SCHEMA_INITIALIZED`` avoids repeat work within a process.  The migration
    ledger below is the cross-process guard: normal one-command CLI invocations
    must not rerun DDL, legacy backfills or migration probes on every startup.
    """
    migrations = [("001-core-tables", lambda: _create_core_tables(conn))]
    migrations.extend(
        (migration_id, lambda sql=sql: apply_column_migration(conn, sql))
        for migration_id, sql in SCHEMA_MIGRATIONS
    )
    migrations.extend(
        [
            (
                "023-holdings-autoincrement-id",
                lambda: _migrate_holdings_autoincrement_id(conn),
            ),
            ("024-holdings-indices", lambda: _ensure_holdings_indices(conn)),
            ("025-holdings-metadata-backfill", lambda: backfill_holding_metadata(conn)),
            ("026-legacy-alerts-backfill", lambda: backfill_legacy_alerts(conn)),
            (
                "034-holding-thesis-versions",
                lambda: _create_thesis_version_schema(conn),
            ),
        ]
    )
    bootstrap_schema(conn, domain.utc_now_iso(), migrations)
