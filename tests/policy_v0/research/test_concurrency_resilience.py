"""Regression tests for cache boundaries, corrupt legacy rows and write races."""

from __future__ import annotations

import threading
from contextlib import closing, contextmanager
from datetime import timedelta

import pytest

from a_share_research.policy.v0 import (
    commands_admin,
    commands_holdings,
    db,
    domain,
    store,
)
from policy_v0.helpers import set_valid_fundamentals


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "concurrency-resilience.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
    yield db_file


def test_cleanup_compare_and_delete_preserves_concurrently_refreshed_rows(
    monkeypatch,
) -> None:
    now = domain.utc_now()
    old = (now - timedelta(days=30)).isoformat()
    fresh = now.isoformat()
    set_valid_fundamentals("600000", "测试公司", "制造", {"roe": 10}, ttl=1)
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

