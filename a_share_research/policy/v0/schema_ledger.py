"""SQLite schema bootstrap 的跨进程锁和持久化迁移账本。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile

import sqlite3


@contextmanager
def schema_process_lock(database_path: str) -> Iterator[None]:
    """Serialize one database's first-run bootstrap across CLI processes."""
    database_key = hashlib.sha256(
        os.path.abspath(database_path).encode("utf-8")
    ).hexdigest()[:16]
    lock_path = (
        Path(tempfile.gettempdir()) / f"a-stock-cache-schema-{database_key}.lock"
    )
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def bootstrap_schema(
    conn: sqlite3.Connection,
    applied_at: str,
    migrations: Sequence[tuple[str, Callable[[], None]]],
) -> list[str]:
    """Run every missing migration and return the IDs applied in this process.

    Each migration has its own stable ID.  A later release can append a new
    item without reopening old work; existing databases then execute only that
    newly missing item.  The ledger survives one-command CLI process exits.
    """
    ledger_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if ledger_exists is None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )"""
        )

    applied_ids = {
        row[0] for row in conn.execute("SELECT migration_id FROM schema_migrations")
    }
    pending = [item for item in migrations if item[0] not in applied_ids]
    if not pending:
        return []

    conn.execute("PRAGMA journal_mode=WAL")
    completed: list[str] = []
    for migration_id, apply in pending:
        apply()
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
            (migration_id, applied_at),
        )
        completed.append(migration_id)
    conn.commit()
    return completed
