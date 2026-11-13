"""P0 regression coverage for strict data, quote, scoring and refresh contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from a_share_research.policy.v0 import commands_admin, db, domain, store
from policy_v0.helpers import valid_fundamentals_payload

NOW = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
PROJECT_ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "p0.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
    monkeypatch.setattr(domain, "utc_now", lambda: NOW)
    yield str(db_file)


def record_quote(
    code: str, price: float = 100.0, fetched_at: str | None = None
) -> None:
    store.record_quote_snapshot(
        code,
        price,
        "2026-07-14",
        "10:00:00",
        "sina",
        {"sina": {"price": price, "quote_date": "2026-07-14"}},
        False,
        fetched_at=fetched_at or NOW.isoformat(),
    )


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


def test_runtime_version_is_frozen_at_last_release() -> None:
    from a_share_research.policy import v0

    assert v0.__version__ == "0.1.43"
    for skill in ("a-stock-research", "a-stock-monitor", "a-stock-qa"):
        text = (PROJECT_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "\nversion:" not in text


def test_research_routes_product_cycle_analysis_to_grounded_reference() -> None:
    skill_root = PROJECT_ROOT / "a-stock-research"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    reference = (skill_root / "references" / "catalyst-cycle-analysis.md").read_text(
        encoding="utf-8"
    )

    assert "references/catalyst-cycle-analysis.md" in skill
    assert "公司被正式点名或另有公司公告时，才能映射为公司订单" in reference
    assert "客户保密时不要从行业生态名单反推公司订单" in reference
    assert "映射为公司订单" not in skill
    for contract in (
        "延续研究也不能沿用早先盘中价格",
        "强制反证",
        "AI 服务器平台出货不等于公司供应关系",
        "条件区间，不是目标价",
    ):
        assert contract in reference


@pytest.mark.parametrize(
    "period", ["2025年报", "2026半年报", "2026Q1", "2026Q2", "2026Q3"]
)
def test_fundamentals_accepts_supported_data_periods(period: str) -> None:
    store.set_fundamentals(
        "600000", "测试", "制造", valid_fundamentals_payload({"roe": 12.0}, period)
    )

    assert store.get_fundamentals("600000")["data_period"] == period


@pytest.mark.parametrize("period", [None, "", "2026", "2026Q4", "2026年中报"])
def test_fundamentals_rejects_missing_or_invalid_data_period(period) -> None:
    payload = valid_fundamentals_payload({"roe": 12.0})
    payload["data_period"] = period

    with pytest.raises(ValueError, match="data_period"):
        store.set_fundamentals("600000", "测试", "制造", payload)


def test_fundamentals_requires_provenance_and_null_reason_for_every_field() -> None:
    missing_provenance = valid_fundamentals_payload({"roe": 12.0})
    missing_provenance["field_provenance"].pop("roe")
    missing_reason = valid_fundamentals_payload({"gross_margin": None})
    missing_reason["null_reasons"].clear()

    with pytest.raises(ValueError, match="field_provenance"):
        store.set_fundamentals("1", "测试", "制造", missing_provenance)
    with pytest.raises(ValueError, match="null_reasons"):
        store.set_fundamentals("2", "测试", "制造", missing_reason)


def test_legacy_naive_timestamp_is_interpreted_as_asia_shanghai(monkeypatch) -> None:
    monkeypatch.setattr(
        domain, "utc_now", lambda: datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
    )

    assert domain.is_expired("2026-07-14T09:00:00", 2) is False
    assert domain.is_expired("2026-07-14T07:59:59", 2) is True


def test_market_indicator_24_hour_boundary(monkeypatch) -> None:
    store.set_market_indicator_snapshot(
        "bond_yield_10y",
        1.8,
        "2026-07-13",
        "fixture",
        fetched_at=(NOW - timedelta(hours=24)).isoformat(),
    )
    assert (
        store.get_market_indicator_snapshot(
            "bond_yield_10y", max_age=timedelta(hours=24)
        )
        is not None
    )
    monkeypatch.setattr(domain, "utc_now", lambda: NOW + timedelta(microseconds=1))
    assert (
        store.get_market_indicator_snapshot(
            "bond_yield_10y", max_age=timedelta(hours=24)
        )
        is None
    )


def test_older_market_indicator_write_cannot_replace_newer_snapshot() -> None:
    store.set_market_indicator_snapshot(
        "bond_yield_10y",
        1.7,
        "2026-07-14",
        "new-source",
        fetched_at=NOW.isoformat(),
    )
    store.set_market_indicator_snapshot(
        "bond_yield_10y",
        9.9,
        "2026-07-13",
        "late-old-source",
        fetched_at=(NOW - timedelta(hours=1)).isoformat(),
    )

    snapshot = store.get_market_indicator_snapshot("bond_yield_10y")

    assert snapshot["value"] == 1.7
    assert snapshot["source"] == "new-source"


def test_future_market_and_quote_snapshots_cannot_replace_current_data() -> None:
    future = (
        NOW + store.MAX_SNAPSHOT_CLOCK_SKEW + timedelta(microseconds=1)
    ).isoformat()
    store.set_market_indicator_snapshot(
        "bond_yield_10y", 1.7, "2026-07-14", "fixture", fetched_at=NOW.isoformat()
    )
    record_quote("FUTURE", fetched_at=NOW.isoformat())

    with pytest.raises(ValueError, match="future"):
        store.set_market_indicator_snapshot(
            "bond_yield_10y", 9.9, "2026-07-15", "bad-clock", fetched_at=future
        )
    with pytest.raises(ValueError, match="future"):
        record_quote("FUTURE", 999.0, fetched_at=future)

    assert store.get_market_indicator_snapshot("bond_yield_10y")["value"] == 1.7
    assert store.get_latest_quote_snapshot("FUTURE")["price"] == 100.0


@pytest.mark.parametrize(
    "price", [True, 0.0, -1.0, float("nan"), float("inf"), float("-inf")]
)
def test_quote_snapshot_rejects_non_positive_or_non_finite_prices(price) -> None:
    with pytest.raises(ValueError, match="invalid validated quote snapshot"):
        record_quote("INVALID", price)


def test_quote_snapshot_retention_is_bounded_per_code() -> None:
    for index in range(store.QUOTE_SNAPSHOT_RETENTION_PER_CODE + 7):
        record_quote(
            "BOUNDED",
            100.0 + index,
            fetched_at=(NOW - timedelta(seconds=index)).isoformat(),
        )

    with db.db_session() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM quote_snapshots WHERE code='BOUNDED'"
        ).fetchone()[0]

    assert count == store.QUOTE_SNAPSHOT_RETENTION_PER_CODE


def test_unsupported_financial_watchlist_rows_are_terminal_qualitative_only() -> None:
    for code, industry in [("HOLDINS", "保险"), ("PENDBRK", "证券公司")]:
        store.update_qualitative_only_security(code, "金融公司", industry)
        record_quote(code)
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


def test_human_facing_fundamentals_timestamp_is_converted_to_cst(capsys) -> None:
    store.set_fundamentals(
        "600000", "测试", "制造", valid_fundamentals_payload({"roe": 12.0})
    )
    with db.db_session() as conn:
        conn.execute(
            "UPDATE stock_fundamentals SET updated_at='2026-07-13T04:09:00+00:00' "
            "WHERE code='600000'"
        )
        conn.commit()

    assert (
        store.get_fundamentals("600000")["_cache_meta"]["updated_at"]
        == "2026-07-13 12:09"
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
    payload = valid_fundamentals_payload({"roe": 10})
    store.set_fundamentals("EXPIRED", "过期基本面", "制造", payload, ttl=1)
    with db.db_session() as conn:
        conn.execute(
            "UPDATE stock_fundamentals SET updated_at=? WHERE code=?",
            ((NOW - timedelta(hours=2)).isoformat(), "EXPIRED"),
        )
        conn.commit()
    record_quote("PENDING", fetched_at=(NOW - timedelta(hours=24)).isoformat())
    record_quote(
        "TOO_LATE", fetched_at=(NOW - timedelta(hours=24, microseconds=1)).isoformat()
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
