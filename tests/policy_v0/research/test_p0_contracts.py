"""Legacy v0 schema, timestamp and watchlist-candidate regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from a_share_research.policy.v0 import commands_admin, db, domain
from policy_v0.helpers import record_valid_quote, seed_qualitative_only, set_valid_fundamentals

NOW = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "p0.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
    monkeypatch.setattr(domain, "utc_now", lambda: NOW)
    yield str(db_file)


def test_schema_migration_adds_quote_tables_and_analysis_columns() -> None:
    with db.db_session() as conn:
        analysis_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(analysis_results)")
        }
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert {
        "quote_price",
        "quote_as_of",
        "quote_source",
        "scoring_status",
    } <= analysis_columns
    assert {
        "quote_snapshots",
        "market_indicator_snapshots",
        "qualitative_only_securities",
    } <= tables


def test_legacy_naive_timestamp_is_interpreted_as_asia_shanghai(monkeypatch) -> None:
    monkeypatch.setattr(
        domain, "utc_now", lambda: datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
    )

    assert domain.is_expired("2026-07-14T09:00:00", 2) is False
    assert domain.is_expired("2026-07-14T07:59:59", 2) is True


def test_unsupported_financial_watchlist_rows_are_terminal_qualitative_only() -> None:
    for code, industry in [("HOLDINS", "保险"), ("PENDBRK", "证券公司")]:
        seed_qualitative_only(code, "金融公司", industry)
        record_valid_quote(code, 100.0, fetched_at=NOW.isoformat())
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO holdings
               (code, name, cost_price, buy_date, updated_at, exit_date)
               VALUES ('HOLDINS','保险持仓',10,'2026-01-01',?,NULL)""",
            (NOW.isoformat(),),
        )
        conn.commit()

    rows = {row["code"]: row for row in commands_admin.get_watchlist_rows()}

    assert {"HOLDINS", "PENDBRK"} <= set(rows)
    for code in ("HOLDINS", "PENDBRK"):
        assert rows[code]["needs_refresh"] is False
        assert (
            rows[code]["refresh_blocked_reason"]
            == "unsupported-financial-qualitative-only"
        )


def insert_analysis(
    code: str, created_at: datetime, *, date_value: str = "2026-07-01"
) -> None:
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO analysis_results
               (code, date, result, created_at, framework, scoring_status)
               VALUES (?,?,?,?,?,'complete')""",
            (code, date_value, "legacy report", created_at.isoformat(), "A通用"),
        )
        conn.commit()


def test_watchlist_candidate_boundaries_and_expired_retention() -> None:
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO holdings
               (code, name, cost_price, buy_date, updated_at, exit_date)
               VALUES ('HOLD','持仓',10,'2026-01-01',?,NULL),
                      ('CLOSED','平仓',10,'2026-01-01',?,'2026-02-01')""",
            (NOW.isoformat(), NOW.isoformat()),
        )
        conn.commit()
    insert_analysis("RECENT", NOW - timedelta(days=14))
    insert_analysis("TOO_OLD", NOW - timedelta(days=14, microseconds=1))
    insert_analysis("EXPIRED", NOW - timedelta(days=1))
    set_valid_fundamentals("EXPIRED", "过期基本面", "制造", {"roe": 10}, ttl=1)
    with db.db_session() as conn:
        conn.execute(
            "UPDATE stock_fundamentals SET updated_at=? WHERE code=?",
            ((NOW - timedelta(hours=2)).isoformat(), "EXPIRED"),
        )
        conn.commit()
    record_valid_quote("PENDING", 100.0, fetched_at=(NOW - timedelta(hours=24)).isoformat())
    record_valid_quote(
        "TOO_LATE", 100.0, fetched_at=(NOW - timedelta(hours=24, microseconds=1)).isoformat()
    )

    rows = commands_admin.get_watchlist_rows()
    by_code = {row["code"]: row for row in rows}

    assert set(by_code) == {"HOLD", "RECENT", "EXPIRED", "PENDING"}
    assert by_code["HOLD"]["is_holding"] is True
    assert by_code["PENDING"]["eligible_reason"] == "pending-first-analysis"
    assert by_code["EXPIRED"]["cache_expired"] is True
    assert by_code["EXPIRED"]["needs_refresh"] is True
    assert [row["refresh_priority"] for row in rows] == list(range(1, len(rows) + 1))
    assert rows[0]["code"] == "HOLD"
    assert rows[1]["code"] == "PENDING"
