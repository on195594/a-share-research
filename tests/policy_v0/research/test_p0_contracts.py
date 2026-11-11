"""P0 regression coverage for strict data, quote, scoring and refresh contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest

from a_share_research.policy.v0 import (
    commands_admin,
    commands_analysis,
    db,
    domain,
    fetcher,
    store,
)
from policy_v0.helpers import (
    set_valid_analysis,
    valid_decision_payload,
    valid_fundamentals_payload,
)

SUBJECTIVE_CATEGORIES = {
    "A": ("护城河", "行业地位"),
    "B": ("护城河", "行业地位"),
    "C": ("护城河", "行业地位"),
    "D": ("特许经营稀缺性", "行业地位"),
    "E": ("品牌渠道", "行业地位"),
    "F": ("护城河", "行业地位"),
}
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


def scoring_input(framework: str, *, cycle: bool | None = None) -> dict:
    letter = framework[0]
    return {
        "schema_version": 1,
        "metrics": {},
        "subjective_assessments": [
            {
                "category": category,
                "rating": "优档",
                "evidence": [f"{category} fixture evidence"],
                "confidence": "高",
            }
            for category in SUBJECTIVE_CATEGORIES[letter]
        ],
        "cycle_stage": (
            {"stage": "上行期", "rationale": "fixture cycle evidence"}
            if cycle is True or cycle is None and letter in {"B", "C", "D"}
            else None
        ),
    }


def run_set_analysis(
    monkeypatch, code: str, framework: str, score: int | None = 60
) -> None:
    record_quote(code)
    set_valid_analysis(
        monkeypatch,
        code=code,
        framework=framework,
        score=score,
        cycle=framework[0] in {"B", "C", "D"},
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


def test_set_analysis_uses_latest_snapshot_without_requesting_another_quote(
    monkeypatch,
) -> None:
    run_set_analysis(monkeypatch, "600000", "A", score=60)

    with db.db_session() as conn:
        analysis = conn.execute(
            "SELECT quote_price, quote_as_of, quote_source FROM analysis_results WHERE code=?",
            ("600000",),
        ).fetchone()
        snapshot = conn.execute(
            "SELECT source, verification_sources, degraded FROM quote_snapshots WHERE code=?",
            ("600000",),
        ).fetchone()
    assert analysis == (100.0, "2026-07-14T10:00:00", "sina")
    assert snapshot[0] == "sina"
    assert json.loads(snapshot[1])["sina"]["price"] == 100.0
    assert snapshot[2] == 0


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


def test_fetcher_payload_writes_complete_provenance_and_excludes_global_bond() -> None:
    results = {
        "roe_3y_avg": 12.0,
        "pe_static": 18.0,
        "pe_ttm": 18.0,
        "pe_percentile_5y": 25.0,
        "ps_ttm": 3.2,
        "ps_percentile_5y": 40.0,
        "_pe_percentile_windows": {
            "5": {"sample_start": "2021-07-14", "sample_end": "2026-07-14"},
        },
        "_ps_percentile_window": {
            "sample_start": "2021-07-30",
            "sample_end": "2026-07-14",
            "valid_months": 60,
            "basis": "ps_ttm",
            "source": "tushare.daily_basic",
        },
        "latest_report_snapshot": {
            "report_period": "2026Q1",
            "announcement_date": "2026-04-25",
            "source": "tushare.fina_indicator+income+balancesheet",
            "is_newer_than_annual": True,
            "fields": {},
        },
        "_quote_as_of": "2026-07-14T10:00:00",
        "price_change_5d": 3.25,
        "_price_change_5d_as_of": "2026-07-11",
    }
    fetcher._build_cache_payload("600000", "测试", "制造", results, {}, "2025年报")

    with db.db_session() as conn:
        stored = json.loads(
            conn.execute(
                "SELECT data FROM stock_fundamentals WHERE code=?", ("600000",)
            ).fetchone()[0]
        )
    business_fields = set(stored) - {"data_period", "null_reasons", "field_provenance"}
    assert "bond_yield_10y" not in stored
    assert set(stored["field_provenance"]) == business_fields
    assert stored["field_provenance"]["roe_3y_avg"]["status"] == "ok"
    assert stored["field_provenance"]["pb"]["status"] == "missing"
    assert stored["null_reasons"]["pb"]
    assert stored["pe_static"] == stored["pe_ttm"] == 18.0
    assert stored["field_provenance"]["pe_static"]["as_of"] == "2026-07-14T10:00:00"
    assert stored["price_change_5d"] == 3.25
    assert stored["field_provenance"]["price_change_5d"] == {
        "source": "computed",
        "as_of": "2026-07-11",
        "status": "ok",
    }
    assert stored["field_provenance"]["pe_percentile_5y"]["window_years"] == "5"
    assert (
        stored["field_provenance"]["pe_percentile_5y"]["basis"]
        == "annual_eps_disclosure_lag_adjusted"
    )
    assert stored["field_provenance"]["pe_percentile_5y"]["price_basis"] == "raw_close"
    assert (
        stored["field_provenance"]["pe_percentile_5y"]["split_policy"]
        == "detected_event_eps_adjustment"
    )
    assert (
        stored["field_provenance"]["pe_percentile_5y"]["sample_start"] == "2021-07-14"
    )
    assert stored["field_provenance"]["pe_percentile_5y"]["sample_end"] == "2026-07-14"
    assert stored["ps_ttm"] == 3.2
    assert stored["ps_percentile_5y"] == 40.0
    assert stored["field_provenance"]["ps_percentile_5y"]["basis"] == "ps_ttm"
    assert stored["field_provenance"]["ps_percentile_5y"]["valid_months"] == 60
    assert (
        stored["field_provenance"]["ps_percentile_5y"]["source"]
        == "tushare.daily_basic"
    )
    assert stored["field_provenance"]["latest_report_snapshot"]["status"] == "ok"
    assert (
        stored["field_provenance"]["latest_report_snapshot"]["source"]
        == "tushare.fina_indicator+income+balancesheet"
    )
    assert stored["field_provenance"]["latest_report_snapshot"]["as_of"] == "2026-04-25"
    assert stored["latest_report_snapshot"]["report_period"] == "2026Q1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025", "2025年报"),
        ("2026Q1", "2026Q1"),
        ("2026-06-30", "2026半年报"),
        ("2026-09-30", "2026Q3"),
    ],
)
def test_fetcher_derives_data_period_from_actual_report_column(raw, expected) -> None:
    import pandas as pd

    assert fetcher._extract_data_period(pd.DataFrame({"报告期": [raw]})) == expected


def test_fetcher_refuses_fundamentals_write_when_report_period_unknown() -> None:
    with pytest.raises(SystemExit):
        fetcher._build_cache_payload("600000", "测试", "制造", {}, {}, None)

    with db.db_session() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM stock_fundamentals").fetchone()[0] == 0
        )


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


@pytest.mark.parametrize(
    "token,expected",
    [
        ("A", "A通用"),
        ("B", "B银行"),
        ("C", "C资源"),
        ("D", "D公用"),
        ("E", "E消费"),
        ("F", "F科技"),
    ],
)
def test_set_analysis_accepts_all_framework_aliases(
    monkeypatch, token: str, expected: str
) -> None:
    run_set_analysis(monkeypatch, str(ord(token)).zfill(6), token)

    with db.db_session() as conn:
        stored = conn.execute("SELECT framework FROM analysis_results").fetchone()[0]
    assert stored == expected


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"schema_version": 2},
        {"stock_code": "60000"},
        {"framework": "unknown"},
        {"framework_score": True},
        {"framework_score": 81},
    ],
)
def test_set_analysis_rejects_invalid_decision_without_analysis_write(
    monkeypatch, invalid_fields
) -> None:
    record_quote("600000")
    decision = valid_decision_payload("600000")
    decision.update(invalid_fields)
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(decision)))

    with pytest.raises(SystemExit):
        commands_analysis.cmd_set_analysis([])
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0] == 0


@pytest.mark.parametrize("framework", ["A", "B", "C", "D", "E", "F"])
def test_each_framework_requires_its_normalized_subjective_categories(
    capsys, framework: str
) -> None:
    code = str(ord(framework)).zfill(6)
    store.set_fundamentals(code, "fixture", "fixture", valid_fundamentals_payload({}))
    scoring = scoring_input(framework)

    commands_analysis.cmd_score_fundamentals([code, framework, json.dumps(scoring)])
    complete = json.loads(capsys.readouterr().out)
    assert not any(
        item.startswith("subjective:") for item in complete["missing_inputs"]
    )

    missing_category = SUBJECTIVE_CATEGORIES[framework][0]
    scoring["subjective_assessments"] = scoring["subjective_assessments"][1:]
    commands_analysis.cmd_score_fundamentals([code, framework, json.dumps(scoring)])
    incomplete = json.loads(capsys.readouterr().out)
    assert f"subjective:{missing_category}" in incomplete["missing_inputs"]


@pytest.mark.parametrize("framework", ["B", "C", "D"])
def test_cycle_frameworks_fail_closed_without_structured_cycle_stage(
    capsys, framework: str
) -> None:
    code = str(ord(framework)).zfill(6)
    store.set_fundamentals(code, "fixture", "fixture", valid_fundamentals_payload({}))

    commands_analysis.cmd_score_fundamentals(
        [code, framework, json.dumps(scoring_input(framework, cycle=False))]
    )
    result = json.loads(capsys.readouterr().out)

    assert result["complete"] is False
    assert "cycle_stage" in result["missing_inputs"]


def test_c_valuation_conflict_is_incomplete_and_rejects_a_total_score(
    monkeypatch,
) -> None:
    record_quote("601899")
    conflict = valid_decision_payload(
        "601899", "C", score=None, cycle=True, conflict=True
    )
    invalid = conflict | {"framework_score": 48}
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(invalid)))

    with pytest.raises(SystemExit):
        commands_analysis.cmd_set_analysis([])
    with db.db_session() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM analysis_results WHERE code=?", ("601899",)
            ).fetchone()[0]
            == 0
        )

    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(conflict)))
    commands_analysis.cmd_set_analysis([])
    with pytest.raises(SystemExit):
        commands_analysis.cmd_set_score_breakdown(
            [
                "601899",
                json.dumps(
                    {
                        "fundamentals": {"subtotal": 44},
                        "timing": {"subtotal": 4},
                        "total": 48,
                    }
                ),
            ]
        )
    incomplete_breakdown = {
        "fundamentals": {"subtotal": 44},
        "timing": {"subtotal": None},
        "total": None,
    }
    commands_analysis.cmd_set_score_breakdown(
        ["601899", json.dumps(incomplete_breakdown)]
    )
    with db.db_session() as conn:
        stored = conn.execute(
            "SELECT score, scoring_status, score_breakdown FROM analysis_results WHERE code=?",
            ("601899",),
        ).fetchone()
    assert stored[:2] == (None, "incomplete")
    assert json.loads(stored[2]) == incomplete_breakdown


def test_d_without_bond_snapshot_is_incomplete_and_accepts_null_timing(
    monkeypatch,
) -> None:
    record_quote("600900")
    decision = valid_decision_payload("600900", "D", score=None, cycle=True)
    decision["block_reason"] = ["bond_yield_missing"]
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(decision)))
    commands_analysis.cmd_set_analysis([])
    breakdown = {
        "fundamentals": {"subtotal": 45},
        "timing": {"subtotal": None},
        "total": None,
    }
    commands_analysis.cmd_set_score_breakdown(["600900", json.dumps(breakdown)])

    with db.db_session() as conn:
        row = conn.execute(
            "SELECT score, scoring_status, score_breakdown FROM analysis_results WHERE code=?",
            ("600900",),
        ).fetchone()
    assert row[0] is None
    assert row[1] == "incomplete"
    assert json.loads(row[2])["timing"]["subtotal"] is None


def test_d_without_bond_snapshot_rejects_complete_score(monkeypatch) -> None:
    record_quote("600901")
    decision = valid_decision_payload("600901", "D", score=None, cycle=True)
    decision["block_reason"] = ["bond_yield_missing"]
    decision["framework_score"] = 60
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(decision)))

    with pytest.raises(SystemExit):
        commands_analysis.cmd_set_analysis([])
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0] == 0


def test_non_d_or_complete_analysis_rejects_null_timing(monkeypatch) -> None:
    run_set_analysis(monkeypatch, "600000", "A", score=60)
    breakdown = {
        "fundamentals": {"subtotal": 45},
        "timing": {"subtotal": None},
        "total": None,
    }

    with pytest.raises(SystemExit):
        commands_analysis.cmd_set_score_breakdown(["600000", json.dumps(breakdown)])


@pytest.mark.parametrize(
    ("new_price", "hit"),
    [(102.999, True), (103.0, False), (97.001, True), (97.0, False)],
)
def test_analysis_price_invalidation_three_percent_boundary(
    monkeypatch, capsys, new_price: float, hit: bool
) -> None:
    run_set_analysis(monkeypatch, "600000", "A", score=60)
    record_quote(
        "600000", new_price, fetched_at=(NOW + timedelta(seconds=1)).isoformat()
    )

    commands_analysis.cmd_check(["600000"])
    output = capsys.readouterr().out
    assert ("ANALYSIS_HIT" in output) is hit
    assert ("ANALYSIS_PRICE_STALE" in output) is (not hit)


def test_analysis_hit_reports_signed_price_decline(monkeypatch, capsys) -> None:
    run_set_analysis(monkeypatch, "600000", "A", score=60)
    record_quote("600000", 98.0, fetched_at=(NOW + timedelta(seconds=1)).isoformat())

    commands_analysis.cmd_check(["600000"])
    output = capsys.readouterr().out

    assert "ANALYSIS_HIT" in output
    assert "较分析快照:-2.00%" in output


def test_analysis_without_fresh_quote_cannot_hit(monkeypatch, capsys) -> None:
    run_set_analysis(monkeypatch, "600000", "A", score=60)
    with db.db_session() as conn:
        conn.execute("DELETE FROM quote_snapshots WHERE code='600000'")
        conn.commit()

    commands_analysis.cmd_check(["600000"])
    output = capsys.readouterr().out

    assert "ANALYSIS_HIT" not in output
    assert "ANALYSIS_PRICE_STALE" in output
    assert "FULL_MISS" in output


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
    commands_analysis.cmd_check(["600000"])
    output = capsys.readouterr().out
    assert "更新:2026-07-13 12:09" in output
    assert '"updated_at": "2026-07-13 12:09"' in output


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
