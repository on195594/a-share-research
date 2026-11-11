"""SQLite connection ownership and read-only request scope."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from a_share_research.policy.v0 import paths, schema
from a_share_research.policy.v0.schema_ledger import schema_process_lock

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_INITIALIZED = False
_SCHEMA_INITIALIZED_PATH = ""
_READ_ONLY_REQUEST = False


def is_read_only() -> bool:
    return _READ_ONLY_REQUEST


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
    return connection


def get_db(timeout: float = 30.0) -> sqlite3.Connection:
    global _SCHEMA_INITIALIZED, _SCHEMA_INITIALIZED_PATH
    if is_read_only():
        return _read_only_connection(timeout)
    database_path = str(paths.cache_db_path())
    try:
        paths.ensure_db_parent(database_path)
    except PermissionError as exc:
        raise sqlite3.OperationalError(str(exc)) from exc
    connection = sqlite3.connect(database_path, timeout=timeout)
    connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout * 1000))}")
    if not _SCHEMA_INITIALIZED or _SCHEMA_INITIALIZED_PATH != database_path:
        try:
            with _SCHEMA_LOCK:
                if not _SCHEMA_INITIALIZED or _SCHEMA_INITIALIZED_PATH != database_path:
                    with schema_process_lock(database_path):
                        schema.bootstrap_database_schema(connection)
                        _SCHEMA_INITIALIZED = True
                        _SCHEMA_INITIALIZED_PATH = database_path
        except BaseException:
            connection.close()
            raise
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
