"""Thread-local web connections against SQLite and captured production records."""

from concurrent.futures import ThreadPoolExecutor
import importlib
import sqlite3
import sys
from threading import Barrier

import pytest

import a_share_research
from a_share_research import db
from support.continuation import RECORDS, load


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8627")
    monkeypatch.setenv("SESSION_SECRET", "isolated-web-connection-" * 2)
    monkeypatch.setattr(a_share_research, "web", None, raising=False)
    monkeypatch.delitem(sys.modules, "a_share_research.web", raising=False)
    module = importlib.import_module("a_share_research.web")
    load(module.conn)
    try:
        yield module
    finally:
        module.conn.close()


def test_connections_are_reused_per_thread_and_close_independently(web):
    conn = web.conn
    main_connection = conn.execute("SELECT 1").connection
    changes = conn.total_changes

    def create_run():
        connection = conn.execute("SELECT 1").connection
        assert conn.execute("SELECT 1").connection is connection
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        before = conn.total_changes
        with db.transaction(conn):
            run_id = db.create_run(conn, RECORDS[0]["report"]["code"])
        assert conn.total_changes == before + 1
        return connection, run_id

    with ThreadPoolExecutor(max_workers=1) as pool:
        first, first_id = pool.submit(create_run).result(timeout=10)
        second, second_id = pool.submit(create_run).result(timeout=10)
        assert first is second and first is not main_connection
        assert first_id != second_id
        assert conn.execute("SELECT code FROM runs WHERE id=?", (second_id,)).fetchone()[0] == RECORDS[0]["report"]["code"]
        assert conn.total_changes == changes
        pool.submit(lambda: conn.close()).result(timeout=10)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            pool.submit(lambda: conn.execute("SELECT 1")).result(timeout=10)
    assert conn.execute("SELECT 1").connection is main_connection


def test_other_thread_cannot_read_commit_rollback_or_close_an_open_transaction(web):
    conn = web.conn
    before = list(conn.iterdump())
    report_id = RECORDS[0]["report"]["id"]
    ready = Barrier(2, timeout=10)
    written = Barrier(2, timeout=10)
    observed = Barrier(2, timeout=10)

    def writer():
        try:
            conn.execute("SELECT 1")  # Open both connections before taking the write lock.
            ready.wait()
            with pytest.raises(RuntimeError, match="rollback isolated write"):
                with db.transaction(conn):
                    conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
                    written.wait()
                    observed.wait()
                    assert conn.in_transaction
                    assert conn.execute("SELECT id FROM reports WHERE id=?", (report_id,)).fetchone() is None
                    raise RuntimeError("rollback isolated write")
        finally:
            conn.close()

    def reader():
        try:
            conn.execute("SELECT 1")
            ready.wait()
            written.wait()
            assert not conn.in_transaction
            assert conn.execute("SELECT id FROM reports WHERE id=?", (report_id,)).fetchone()[0] == report_id
            conn.commit()
            conn.rollback()
        finally:
            conn.close()
        observed.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer), pool.submit(reader)]
        for future in futures:
            future.result(timeout=15)
    assert list(conn.iterdump()) == before
