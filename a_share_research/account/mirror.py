"""One-way mirror of the old account database into the account tables, usable only before the cutover.

A run reads every source table inside one read transaction, replaces the target tables inside one
write transaction and commits only if the copied content hashes equal to what was read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from a_share_research import db

# Table -> copied columns. Source and target use the same names; company_key is derived in the target.
MANIFEST = {
    "holdings": ("id", "code", "name", "cost_price", "shares", "buy_date", "buy_score", "stop_loss_15",
                 "stop_loss_20", "notes", "updated_at", "exit_price", "exit_date", "framework", "initial_shares",
                 "main_entry_date", "main_entry_basis", "additions_since_main", "reference_cost",
                 "framework_confident"),
    "holding_events": ("id", "holding_id", "code", "event_type", "event_date", "shares", "price", "fees", "tax",
                       "cash_amount", "realized_pnl", "notes", "inferred", "created_at", "report_id"),
    "holding_alerts": ("id", "holding_id", "code", "level", "category", "reason_code", "reason", "status",
                       "evidence", "opened_at", "review_due", "resolved_at", "resolution_evidence", "updated_at"),
    "holding_l3_conditions": ("id", "holding_id", "condition_text", "origin_type", "status", "evidence", "as_of",
                              "next_review_date", "temporary_exit_rule", "created_at", "updated_at",
                              "thesis_version_id", "is_active", "retired_at", "retired_reason", "condition_scope",
                              "action_level", "materiality_basis", "evidence_cadence"),
    "holding_thesis_versions": ("id", "holding_id", "version", "l1", "l2", "rewrite_reason", "status",
                                "created_at"),
    "holding_tier_state": ("holding_id", "tier1_status", "tier2_status", "tier3_status", "exit_path",
                           "exit_target_pct", "exemption_framework", "exemption_declared_at", "peg_method",
                           "updated_at"),
    "retro_notes": ("id", "holding_id", "code", "name", "framework", "buy_date", "exit_date", "buy_score",
                    "actual_return_pct", "holding_days", "error_tags", "thesis_notes", "retro_text",
                    "framework_gap", "created_at"),
    "analysis_results": ("code", "date", "result", "created_at", "name", "score", "flags", "score_breakdown",
                         "return_pct", "holding_days", "framework", "quote_price", "quote_as_of", "quote_source",
                         "scoring_status", "decision_json"),
    "stock_fundamentals": ("code", "name", "industry", "data", "updated_at", "ttl_hours"),
    "quote_snapshots": ("id", "code", "price", "quote_date", "quote_time", "fetched_at", "source",
                        "verification_sources", "degraded", "valid"),
    "qualitative_only_securities": ("code", "name", "industry", "updated_at"),
    "market_indicator_snapshots": ("indicator_key", "value", "as_of", "fetched_at", "source", "status"),
}
# Source columns left behind: no reader in the old runtime; a non-NULL value stops the mirror.
DROPPED = {"analysis_results": ("data_date",)}
# Source tables left behind: the old runtime's own migration ledger (this repository's migrations own the schema).
SKIPPED_TABLES = frozenset({"schema_migrations"})


class MirrorError(RuntimeError):
    """The mirror refused to run or to commit; the target is unchanged."""


def _read(conn: sqlite3.Connection) -> dict:
    """Rows of every manifest table plus AUTOINCREMENT counters, in a stable order."""
    tables = {}
    for table, columns in MANIFEST.items():
        names = ", ".join(columns)
        # The key of every table is its first column or its first two.
        tables[table] = [tuple(row) for row in conn.execute(f"SELECT {names} FROM {table} ORDER BY 1, 2")]
    sequences = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
        sequences = {name: seq for name, seq in conn.execute("SELECT name, seq FROM sqlite_sequence")
                     if name in MANIFEST}
    return {"tables": tables, "sequences": dict(sorted(sequences.items()))}


def digest(content: dict) -> str:
    # repr keeps int/float/None/str distinct, which a JSON round trip would not.
    return hashlib.sha256(repr((sorted(content["tables"].items()), sorted(content["sequences"].items())))
                          .encode()).hexdigest()


def read_source(path: Path) -> dict:
    """One consistent snapshot of the old account database, after checking it still has the manifest's shape."""
    if not path.is_file():
        raise MirrorError(f"来源账户库不存在：{path}")
    source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        source.execute("BEGIN")  # every read below sees the same committed state
        found = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        expected = set(MANIFEST) | SKIPPED_TABLES
        if found != expected:
            raise MirrorError(f"来源表与迁移清单不一致：多出 {sorted(found - expected)}，缺少 {sorted(expected - found)}")
        for table, columns in MANIFEST.items():
            actual = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
            listed = set(columns) | set(DROPPED.get(table, ()))
            if actual != listed:
                raise MirrorError(f"来源表 {table} 的列与迁移清单不一致：多出 {sorted(actual - listed)}，"
                                  f"缺少 {sorted(listed - actual)}")
            for column in DROPPED.get(table, ()):
                if source.execute(f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1").fetchone():
                    raise MirrorError(f"来源列 {table}.{column} 出现数据，清单把它列为不迁移，需要先更新清单")
        return _read(source)
    finally:
        source.close()


def mirror(conn: sqlite3.Connection, source_path: Path) -> dict:
    """Replace the account tables with the source's current content; returns what was copied."""
    content = read_source(source_path)
    sha = digest(content)
    with db.transaction(conn):
        state = conn.execute("SELECT mode FROM account_source WHERE id = 1").fetchone()
        if state is not None and state["mode"] != "mirror":
            raise MirrorError("账户已切换到本库账本，镜像不再运行")
        for table, columns in MANIFEST.items():
            conn.execute(f"DELETE FROM {table}")
            conn.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                             content["tables"][table])
        # Counters follow the source so an id it once issued is never issued again here.
        conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({', '.join('?' * len(MANIFEST))})", tuple(MANIFEST))
        conn.executemany("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", content["sequences"].items())
        if digest(_read(conn)) != sha:
            raise MirrorError("镜像后的内容与来源不一致，已回滚")
        watermark = {"rows": {table: len(rows) for table, rows in content["tables"].items()},
                     "sequences": content["sequences"]}
        conn.execute("INSERT INTO account_source (id, mode, source_sha256, updated_at, watermark)"
                     " VALUES (1, 'mirror', ?, ?, ?) ON CONFLICT(id) DO UPDATE SET"
                     " source_sha256 = excluded.source_sha256, updated_at = excluded.updated_at,"
                     " watermark = excluded.watermark",
                     (sha, db.now(), json.dumps(watermark, ensure_ascii=False)))
    return {"source_sha256": sha, **watermark}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="old account database (opened read-only)")
    args = parser.parse_args()
    conn = db.connect()
    try:
        print(json.dumps(mirror(conn, args.source), ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
