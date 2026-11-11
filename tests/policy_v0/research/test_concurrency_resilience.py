"""Regression tests for cache boundaries, corrupt legacy rows and write races."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from datetime import timedelta

import pytest

from a_share_research.policy.v0 import (
    cache,
    commands_admin,
    commands_holdings,
    commands_monitor,
    db,
    domain,
    store,
)
from policy_v0.helpers import valid_fundamentals_payload


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "concurrency-resilience.db"
    monkeypatch.setenv("CACHE_DB_PATH", str(db_file))
    yield db_file


def test_quote_snapshot_max_age_exact_boundary(monkeypatch) -> None:
    now = domain.utc_now()
    monkeypatch.setattr(domain, "utc_now", lambda: now)
    store.record_quote_snapshot(
        "BOUNDARY",
        10.0,
        domain.cst_today(),
        "10:00:00",
        "sina",
        {"sina": {"price": 10.0}},
        False,
        fetched_at=(now - timedelta(minutes=30)).isoformat(),
    )
    store.record_quote_snapshot(
        "TOO_OLD",
        10.0,
        domain.cst_today(),
        "10:00:00",
        "sina",
        {"sina": {"price": 10.0}},
        False,
        fetched_at=(now - timedelta(minutes=30, microseconds=1)).isoformat(),
    )

    assert (
        store.get_latest_quote_snapshot(
            "BOUNDARY", max_age=store.QUOTE_SNAPSHOT_MAX_AGE
        )
        is not None
    )
    assert (
        store.get_latest_quote_snapshot("TOO_OLD", max_age=store.QUOTE_SNAPSHOT_MAX_AGE)
        is None
    )


def test_qualitative_only_status_survives_unknown_then_clears_on_known_bank() -> None:
    store.update_qualitative_only_security("601318", "保险公司", "保险")
    store.update_qualitative_only_security("601318", "未知名称", "未知")

    with db.db_session() as conn:
        retained = conn.execute(
            "SELECT name, industry FROM qualitative_only_securities WHERE code=?",
            ("601318",),
        ).fetchone()
    assert retained == ("保险公司", "保险")

    store.update_qualitative_only_security("601318", "测试银行", "股份制银行")
    with db.db_session() as conn:
        cleared = conn.execute(
            "SELECT 1 FROM qualitative_only_securities WHERE code=?", ("601318",)
        ).fetchone()
    assert cleared is None


def test_cleanup_compare_and_delete_preserves_concurrently_refreshed_rows(
    monkeypatch,
) -> None:
    now = domain.utc_now()
    old = (now - timedelta(days=30)).isoformat()
    fresh = now.isoformat()
    store.set_fundamentals(
        "600000", "测试公司", "制造", valid_fundamentals_payload({"roe": 10}), ttl=1
    )
    with db.db_session() as conn:
        conn.execute(
            "UPDATE stock_fundamentals SET updated_at=? WHERE code=?",
            (old, "600000"),
        )
        conn.execute(
            """INSERT INTO analysis_results
               (code, date, result, created_at)
               VALUES ('600000','2026-01-01','old',?)""",
            (old,),
        )
        conn.commit()

    class RefreshBeforeDeleteConnection:
        def __init__(self, conn):
            self.conn = conn
            self.fund_refreshed = False
            self.analysis_refreshed = False

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            if (
                normalized.startswith("DELETE FROM stock_fundamentals")
                and not self.fund_refreshed
            ):
                self.conn.execute(
                    "UPDATE stock_fundamentals SET updated_at=? WHERE code=?",
                    (fresh, "600000"),
                )
                self.fund_refreshed = True
            if (
                normalized.startswith("DELETE FROM analysis_results")
                and not self.analysis_refreshed
            ):
                self.conn.execute(
                    """UPDATE analysis_results SET result='fresh', created_at=?
                       WHERE code='600000' AND date='2026-01-01' """,
                    (fresh,),
                )
                self.analysis_refreshed = True
            return self.conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextmanager
    def hooked_session(timeout: float = 30.0):
        conn = db.get_db(timeout)
        try:
            yield RefreshBeforeDeleteConnection(conn)
        finally:
            conn.close()

    monkeypatch.setattr(db, "db_session", hooked_session)
    commands_admin.cmd_cleanup()

    with closing(db.get_db()) as conn:
        fund = conn.execute(
            "SELECT updated_at FROM stock_fundamentals WHERE code=?", ("600000",)
        ).fetchone()
        analysis = conn.execute(
            """SELECT result, created_at FROM analysis_results
               WHERE code='600000' AND date='2026-01-01' """
        ).fetchone()
    assert fund == (fresh,)
    assert analysis == ("fresh", fresh)


def test_cleanup_retains_unparseable_legacy_analysis_timestamp(capsys) -> None:
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO analysis_results
               (code, date, result, created_at)
               VALUES ('LEGACY','2020-01-01','legacy','not-an-iso-timestamp')"""
        )
        conn.commit()

    commands_admin.cmd_cleanup()

    assert "无需清理" in capsys.readouterr().out
    with db.db_session() as conn:
        assert conn.execute(
            "SELECT result FROM analysis_results WHERE code='LEGACY'"
        ).fetchone() == ("legacy",)


def test_cleanup_prunes_preexisting_quote_snapshot_overflow(capsys) -> None:
    now = domain.utc_now()
    overflow = store.QUOTE_SNAPSHOT_RETENTION_PER_CODE + 9
    with db.db_session() as conn:
        conn.executemany(
            """INSERT INTO quote_snapshots
               (code, price, quote_date, quote_time, fetched_at, source,
                verification_sources, degraded, valid)
               VALUES ('OVERFLOW',10.0,?,?,?,?,?,0,1)""",
            [
                (
                    domain.cst_today(),
                    "10:00:00",
                    (now - timedelta(seconds=index)).isoformat(),
                    "sina",
                    '{"sina":{"price":10.0}}',
                )
                for index in range(overflow)
            ],
        )
        conn.commit()

    commands_admin.cmd_cleanup()

    assert "超限行情快照" in capsys.readouterr().out
    with db.db_session() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM quote_snapshots WHERE code='OVERFLOW'"
            ).fetchone()[0]
            == store.QUOTE_SNAPSHOT_RETENTION_PER_CODE
        )


def test_watchlist_tolerates_corrupt_legacy_json_fields() -> None:
    now = domain.utc_now_iso()
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO stock_fundamentals
               (code, name, industry, data, updated_at, ttl_hours)
               VALUES ('CORRUPT','损坏缓存','制造','{bad-json',?,24)""",
            (now,),
        )
        conn.execute(
            """INSERT INTO analysis_results
               (code, date, result, created_at, flags, score_breakdown)
               VALUES ('CORRUPT',?,'legacy',?,'{bad-flags','[wrong-type]')""",
            (domain.cst_today(), now),
        )
        conn.commit()

    rows = commands_admin.get_watchlist_rows()

    assert len(rows) == 1
    assert rows[0]["code"] == "CORRUPT"
    assert rows[0]["flags"] == []
    assert rows[0]["score_breakdown"] is None
    assert rows[0]["pe_ttm"] is None


def test_close_holding_rejects_invalid_date_without_mutation() -> None:
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO holdings (code, cost_price, buy_date) VALUES ('600519',100,'2026-01-01')"
        )
        conn.commit()

    with pytest.raises(SystemExit):
        commands_holdings.cmd_close_holding(["600519", "120", "2026-02-30"])

    with db.db_session() as conn:
        row = conn.execute(
            "SELECT exit_price, exit_date FROM holdings WHERE code='600519'"
        ).fetchone()
    assert row == (None, None)


def test_concurrent_quote_snapshot_writes_are_lossless_and_latest_wins() -> None:
    db.get_db().close()
    now = domain.utc_now()
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    errors: list[BaseException] = []

    def write_snapshot(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            store.record_quote_snapshot(
                "600000",
                100.0 + index,
                domain.cst_today(),
                "10:00:00",
                "sina",
                {"sina": {"price": 100.0 + index}},
                False,
                fetched_at=(now + timedelta(microseconds=index)).isoformat(),
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=write_snapshot, args=(index,))
        for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    with db.db_session() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM quote_snapshots WHERE code='600000'"
        ).fetchone()[0]
    assert count == worker_count
    assert store.get_latest_quote_snapshot("600000")["price"] == 107.0


def test_concurrent_flag_appends_are_lossless_under_contention() -> None:
    now = domain.utc_now_iso()
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO analysis_results (code, date, result, created_at)
               VALUES ('600036',?,'report',?)""",
            (domain.cst_today(), now),
        )
        conn.commit()
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    errors: list[BaseException] = []

    def append_flag(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            commands_monitor.cmd_set_flag(["600036", "yellow", f"flag-{index}"])
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=append_flag, args=(index,))
        for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    with db.db_session() as conn:
        raw = conn.execute(
            "SELECT flags FROM analysis_results WHERE code='600036'"
        ).fetchone()[0]
    flags = json.loads(raw)
    assert len(flags) == worker_count
    assert {flag["reason"] for flag in flags} == {
        f"flag-{index}" for index in range(worker_count)
    }


def test_many_concurrent_close_requests_consume_distinct_lots() -> None:
    worker_count = 8
    with db.db_session() as conn:
        conn.executemany(
            """INSERT INTO holdings (code, cost_price, buy_date)
               VALUES ('600519', ?, ?)""",
            [
                (100.0 + index, f"2026-01-{index + 1:02d}")
                for index in range(worker_count)
            ],
        )
        conn.commit()
    barrier = threading.Barrier(worker_count)
    errors: list[BaseException] = []

    def close_lot(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            commands_holdings.cmd_close_holding(["600519", str(120.0 + index)])
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=close_lot, args=(index,))
        for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    with db.db_session() as conn:
        rows = conn.execute(
            """SELECT exit_price, exit_date FROM holdings
               WHERE code='600519' ORDER BY id"""
        ).fetchall()
    assert all(exit_date is not None for _, exit_date in rows)
    assert {exit_price for exit_price, _ in rows} == {
        120.0 + index for index in range(worker_count)
    }


def test_schema_migration_is_safe_across_processes(tmp_path) -> None:
    db_path = tmp_path / "cross-process-migration.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE stock_fundamentals (
                code TEXT PRIMARY KEY, name TEXT, industry TEXT, data JSON,
                updated_at TEXT, ttl_hours INTEGER DEFAULT 24);
            CREATE TABLE analysis_results (
                code TEXT, date TEXT, result TEXT, created_at TEXT,
                PRIMARY KEY (code, date));
            CREATE TABLE holdings (
                code TEXT PRIMARY KEY, name TEXT, cost_price REAL, shares INTEGER,
                buy_date TEXT, buy_score INTEGER, stop_loss_15 REAL,
                stop_loss_20 REAL, notes TEXT, updated_at TEXT,
                exit_price REAL, exit_date TEXT);
            INSERT INTO holdings (code, cost_price) VALUES ('600519', 1800.0);
        """)

    env = os.environ.copy()
    env["CACHE_DB_PATH"] = str(db_path)
    command = [
        sys.executable,
        "-c",
        "from a_share_research.policy.v0 import db; connection = db.get_db(); connection.close()",
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=os.fspath(os.path.dirname(cache.__file__)),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = [process.communicate(timeout=30) for process in processes]

    failures = [
        (process.returncode, stderr)
        for process, (_, stderr) in zip(processes, results)
        if process.returncode != 0
    ]
    assert not failures
    with closing(sqlite3.connect(db_path)) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(holdings)").fetchall()
        }
        holding = conn.execute(
            "SELECT cost_price FROM holdings WHERE code='600519'"
        ).fetchone()
        qualitative_table = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='qualitative_only_securities' """
        ).fetchone()
    assert "id" in columns
    assert holding == (1800.0,)
    assert qualitative_table == (1,)
