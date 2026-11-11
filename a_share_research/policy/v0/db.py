"""SQLite connection ownership and read-only request scope."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from a_share_research import db as app_db
from a_share_research.policy.v0 import paths

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_INITIALIZED = False
_SCHEMA_INITIALIZED_PATH = ""
_READ_ONLY_REQUEST = False


def is_read_only() -> bool:
    return (_READ_ONLY_REQUEST or os.environ.get("POLICY_V0_ANALYSIS_SHADOW") == "1"
            or bool(os.environ.get("POLICY_V0_ACCOUNT_SHADOW")))


@contextmanager
def read_only_scope(enabled: bool = True) -> Iterator[None]:
    global _READ_ONLY_REQUEST
    previous = _READ_ONLY_REQUEST
    _READ_ONLY_REQUEST = enabled
    try:
        yield
    finally:
        _READ_ONLY_REQUEST = previous


def _read_only_connection(timeout: float) -> sqlite3.Connection:
    database_path = paths.cache_db_path().resolve()
    connection = sqlite3.connect(
        database_path.as_uri() + "?mode=ro",
        uri=True,
        timeout=timeout,
    )
    connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout * 1000))}")
    account = os.environ.get("POLICY_V0_ACCOUNT_SHADOW")
    try:
        shadow_analysis = os.environ.get("POLICY_V0_ANALYSIS_SHADOW") == "1"
        if account:
            from a_share_research.policy.v0.account_store import install
            install(connection, account, seal=not shadow_analysis)
        if shadow_analysis:
            from a_share_research.account.analysis_projection import install
            install(connection, account_id=account)
    except BaseException:
        connection.close()
        raise
    return connection


def _ensure_schema(database_path: Path) -> None:
    """Create the database and bring it to this repository's schema, once per process and path."""
    global _SCHEMA_INITIALIZED, _SCHEMA_INITIALIZED_PATH
    key = str(database_path)
    if _SCHEMA_INITIALIZED and _SCHEMA_INITIALIZED_PATH == key:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_INITIALIZED and _SCHEMA_INITIALIZED_PATH == key:
            return
        try:
            paths.ensure_db_parent(database_path)
        except PermissionError as exc:
            raise sqlite3.OperationalError(str(exc)) from exc
        if database_path.exists():
            probe = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                legacy = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
            finally:
                probe.close()
            if legacy:
                # Migrating it would add the research tables to the old runtime's live database.
                raise sqlite3.OperationalError(
                    f"{database_path} is an old a-stock-agent account database; "
                    "policy v0 here only opens the application database"
                )
        app_db.connect(database_path).close()
        _SCHEMA_INITIALIZED = True
        _SCHEMA_INITIALIZED_PATH = key


def get_db(timeout: float = 30.0) -> sqlite3.Connection:
    if is_read_only():
        return _read_only_connection(timeout)
    database_path = paths.cache_db_path()
    _ensure_schema(database_path)
    connection = sqlite3.connect(str(database_path), timeout=timeout)
    connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout * 1000))}")
    return connection


@contextmanager
def db_session(timeout: float = 30.0) -> Iterator[sqlite3.Connection]:
    connection = get_db(timeout)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def read_only_db_session(timeout: float = 30.0) -> Iterator[sqlite3.Connection]:
    """Open the configured database without creating or migrating it."""
    connection = _read_only_connection(timeout)
    try:
        yield connection
    finally:
        connection.close()
