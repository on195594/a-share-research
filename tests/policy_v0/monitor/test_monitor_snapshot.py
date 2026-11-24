from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from a_share_research.policy.v0 import (
    cache,
    commands_holdings,
    commands_monitor,
    db,
    domain,
    market_quotes,
)
from policy_v0.helpers import mock_price_quotes
from support.policy_replay import _quotes, _run, _seed_holding

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "monitor"


def test_local_snapshot_keeps_one_wal_version(
    isolated_cache_database, monkeypatch
) -> None:
    holding_id = _seed_holding(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database, timeout=0) as writer:
        original_session = db.read_only_db_session

        def update_after_first_read(sql):
            if "SELECT COUNT(*) FROM holdings" in sql:
                writer.execute("UPDATE holdings SET shares=200")
                writer.execute(
                    """INSERT INTO holding_alerts
                       (holding_id, code, level, category, reason_code, reason,
                        status, evidence, opened_at, updated_at)
                       VALUES (?, '600036', 'yellow', 'unverified',
                               'technical_review', 'new version', 'active',
                               'fixture', ?, ?)""",
                    (holding_id, domain.utc_now().isoformat(), domain.utc_now().isoformat()),
                )
                writer.commit()

        @contextmanager
        def interrupted_read():
            with original_session() as reader:
                reader.set_trace_callback(update_after_first_read)
                yield reader

        monkeypatch.setattr(db, "read_only_db_session", interrupted_read)
        local = commands_monitor._load_monitor_local_snapshot()
        assert local["holdings"][0]["shares"] == 100
        assert local["holdings"][0]["alerts"] == []
        assert writer.execute("SELECT shares FROM holdings").fetchone() == (200,)
        assert writer.execute("SELECT COUNT(*) FROM holding_alerts").fetchone() == (1,)



def test_local_snapshot_releases_transaction_and_connection(
    isolated_cache_database, monkeypatch
) -> None:
    _seed_holding(isolated_cache_database)
    original_session = db.read_only_db_session

    @contextmanager
    def failing_read():
        with original_session() as reader:
            reader.set_authorizer(
                lambda action, table, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "holding_alerts"
                else sqlite3.SQLITE_OK
            )
            try:
                yield reader
            finally:
                assert not reader.in_transaction

    with monkeypatch.context() as patch:
        patch.setattr(db, "read_only_db_session", failing_read)
        with pytest.raises(sqlite3.DatabaseError):
            commands_monitor._load_monitor_local_snapshot()
    assert commands_monitor._load_monitor_local_snapshot()["holdings"]


def test_clean_five_holding_fast_gate_does_not_write(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    positions = [
        ("600036", 500),
        ("600900", 800),
        ("603606", 500),
        ("000963", 800),
        ("002050", 500),
    ]
    for code, shares in positions:
        _seed_holding(isolated_cache_database, code, shares=shares)
    with sqlite3.connect(isolated_cache_database) as conn:
        legacy = {"regulatory_gate": {"status": "blocked", "action_eligible": False, "reason_code": "legacy"}}
        conn.execute(
            """INSERT INTO stock_fundamentals (code, name, industry, data, updated_at, ttl_hours)
               VALUES ('600036', 'fixture', 'fixture', ?, '2000-01-01T00:00:00+00:00', 1)""",
            (json.dumps(legacy),),
        )
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(isolated_cache_database.read_bytes()).hexdigest()
    mock_price_quotes(monkeypatch, _quotes)

    # Synthetic clean scenario: 62,000 risk on 1M stays inside both budgets.
    result, payload = _run(capsys, ["--portfolio-value", "1000000"])

    assert result == 0
    assert payload["data_status"] == "complete"
    assert payload["valuation_status"] == "exact"
    assert payload["review_status"] == "cleared"
    assert payload["action_status"] == "no_action"
    assert payload["stop_reason"] == "clean_fast_gate"
    assert payload["escalations"] == [] and payload["data_gaps"] == []
    assert "risk_gates" not in payload["holdings"][0]
    assert payload["quote_coverage"] == {"active": 5, "complete": True, "priced": 5}
    assert payload["manifest"]["research_subagents"] == 0
    assert hashlib.sha256(isolated_cache_database.read_bytes()).hexdigest() == before
    assert cache.COMMAND_CLASSIFICATION["monitor-snapshot"] == "R1"


def test_missing_industry_comparison_blocks_clean_fast_gate(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            codes[0]: market_quotes.PriceQuote(
                100,
                domain.cst_today(),
                "10:00:00",
                "sina",
                previous_close=100,
                industry_change_pct=None,
            )
        },
    )

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 1
    assert payload["data_status"] == "partial"
    assert payload["valuation_status"] == "exact"
    assert payload["review_status"] == "review_required"
    assert payload["action_status"] == "review_candidate"
    assert payload["stop_reason"] is None
    assert {
        (gap["code"], gap["field"], gap["minimum_action"])
        for gap in payload["data_gaps"]
    } >= {("600036", "industry_change_pct", "supply a same-day industry comparison")}


@pytest.mark.parametrize(
    ("case", "reason", "expected_data", "expected_review"),
    [
        ("stop1", "price_stop_1", "complete", "review_required"),
        ("stop2", "price_stop_2", "partial", "review_required"),
        ("daily", "daily_drop", "complete", "review_required"),
        ("relative", "relative_underperformance", "complete", "review_required"),
        ("alert", "alert_review_due", "complete", "review_required"),
        ("l3_due", "l3_review_due", "complete", "review_required"),
        ("l3_candidate", "l3_candidate", "complete", "review_required"),
        ("quote_gap", "quote_gap", "unavailable", "blocked"),
        ("denominator", "denominator_missing", "partial", "review_required"),
        ("conflict", "data_conflict", "conflicted", "blocked"),
    ],
)
def test_escalation_paths_are_stable_and_fail_closed(
    case,
    reason,
    expected_data,
    expected_review,
    isolated_cache_database,
    monkeypatch,
    capsys,
) -> None:
    kwargs = {}
    if case == "l3_due":
        kwargs["next_review"] = "2000-01-01"
    if case == "l3_candidate":
        kwargs.update(l3_status="triggered", l3_action="reduce")
    _seed_holding(isolated_cache_database, **kwargs)
    if case == "conflict":
        _seed_holding(isolated_cache_database, holding_id=20)
    if case == "alert":
        with sqlite3.connect(isolated_cache_database) as conn:
            conn.execute(
                """INSERT INTO holding_alerts
                   (holding_id, code, level, category, reason_code, reason, status,
                    evidence, opened_at, review_due, updated_at)
                   VALUES (1, '600036', 'yellow', 'unverified', 'fixture-alert',
                           'fixture', 'active', 'fixture', ?, '2000-01-01', ?)""",
                (domain.utc_now().isoformat(), domain.utc_now().isoformat()),
            )

    def fetch(codes: list[str]) -> dict:
        quotes = _quotes(codes)
        quote = quotes["600036"]
        if case == "stop1":
            quotes["600036"] = market_quotes.PriceQuote(
                84,
                domain.cst_today(),
                "10:00:00",
                "fixture",
                previous_close=84,
                industry_change_pct=0,
            )
        elif case == "stop2":
            quotes["600036"] = market_quotes.PriceQuote(
                79,
                domain.cst_today(),
                "10:00:00",
                "fixture",
                previous_close=79,
                industry_change_pct=0,
            )
        elif case == "daily":
            quotes["600036"] = market_quotes.PriceQuote(
                96.9,
                domain.cst_today(),
                "10:00:00",
                "fixture",
                previous_close=100,
                industry_change_pct=-3.1,
            )
        elif case == "relative":
            quotes["600036"] = market_quotes.PriceQuote(
                99,
                domain.cst_today(),
                "10:00:00",
                "fixture",
                previous_close=100,
                industry_change_pct=1.1,
            )
        elif case == "quote_gap":
            quotes["600036"] = None
        else:
            quotes["600036"] = quote
        return quotes

    mock_price_quotes(monkeypatch, fetch)
    argv = [] if case == "denominator" else ["--portfolio-value", "100000"]
    result, payload = _run(capsys, argv)

    assert reason in {item["reason_code"] for item in payload["escalations"]}
    assert payload["data_status"] == expected_data
    assert payload["review_status"] == expected_review
    assert payload["stop_reason"] is None
    assert result == (0 if expected_data == "complete" else 1)
    if case == "denominator":
        assert payload["account"]["denominator_status"] == "missing"
        assert payload["holdings"][0]["portfolio_weight_pct"] is None
        assert payload["holdings"][0]["stop_risk_pct"] is None
    if case == "quote_gap":
        assert payload["valuation_status"] == "unavailable"
        assert payload["quote_coverage"]["complete"] is False
    if case == "conflict":
        assert payload["action_status"] != "trade_candidate"


def test_partial_quotes_preserve_priced_position_lower_bound(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database, "600036")
    _seed_holding(isolated_cache_database, "600900")

    def fetch(codes: list[str]) -> dict:
        quotes = _quotes(codes)
        quotes["600900"] = None
        return quotes

    mock_price_quotes(monkeypatch, fetch)
    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 1
    assert payload["data_status"] == "partial"
    assert payload["valuation_status"] == "priced_positions_lower_bound"
    assert payload["quote_coverage"] == {"active": 2, "complete": False, "priced": 1}
    assert {gap["code"] for gap in payload["data_gaps"] if gap["field"] == "quote"} == {
        "600900"
    }


@pytest.mark.parametrize("l3_state", ["pending", "legacy", "invalid"])
def test_unresolved_l3_never_clears(
    l3_state, isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(
        isolated_cache_database,
        l3_status=l3_state if l3_state in {"pending", "watch"} else "not_triggered",
    )
    with sqlite3.connect(isolated_cache_database) as conn:
        if l3_state == "legacy":
            conn.execute("DELETE FROM holding_thesis_versions")
        elif l3_state == "invalid":
            conn.execute("UPDATE holding_l3_conditions SET temporary_exit_rule=NULL")
    mock_price_quotes(monkeypatch, _quotes)

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 1
    assert payload["data_status"] == "partial"
    assert payload["review_status"] == "review_required"
    assert payload["action_status"] == "review_candidate"
    assert payload["stop_reason"] is None
    assert payload["holdings"][0]["l3"]["contract"] == (
        l3_state if l3_state in {"legacy", "invalid"} else "versioned"
    )
    assert "l3_candidate" in {item["reason_code"] for item in payload["escalations"]}


def test_verified_watch_l3_is_complete_but_never_clears(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database, l3_status="watch")
    mock_price_quotes(monkeypatch, _quotes)

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 0
    assert payload["data_status"] == "complete"
    assert payload["review_status"] == "review_required"
    assert payload["action_status"] == "review_candidate"
    assert payload["stop_reason"] is None
    reasons = {item["reason_code"] for item in payload["escalations"]}
    assert "l3_watch" in reasons
    assert "l3_candidate" not in reasons
    watch = next(i for i in payload["escalations"] if i["reason_code"] == "l3_watch")
    assert watch["candidate"] == "review"
    assert "next_review" in watch["detail"] and "no new risk" in watch["detail"]
    assert watch["evidence_cadence"] == "unspecified"
    assert not any(gap["field"] == "l3" for gap in payload["data_gaps"])


def test_legacy_l3_and_stale_quote_are_not_clean(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute("DELETE FROM holding_l3_conditions")
        conn.execute("DELETE FROM holding_thesis_versions")
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            codes[0]: market_quotes.PriceQuote(100, "2000-01-01", "15:00:00", "fixture")
        },
    )

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 1
    assert payload["data_status"] == "stale"
    assert payload["review_status"] == "blocked"
    assert payload["holdings"][0]["l3"]["contract"] == "legacy"
    assert payload["stop_reason"] is None


def test_closed_history_is_excluded_and_reopened_lifecycle_is_current(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database, holding_id=1, closed=True)
    _seed_holding(isolated_cache_database, holding_id=2)
    mock_price_quotes(monkeypatch, _quotes)

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 0
    assert [item["id"] for item in payload["holdings"]] == [2]
    assert payload["manifest"]["active_holdings"] == 1
    assert payload["manifest"]["closed_holdings"] == 1
    with sqlite3.connect(isolated_cache_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone() == (2,)


def test_missing_database_returns_machine_readable_unavailable_without_creating_it(
    tmp_path, monkeypatch, capsys
) -> None:
    database = tmp_path / "missing.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))

    first, first_payload = _run(capsys, ["--portfolio-value", "100000"])
    second, second_payload = _run(capsys, ["--portfolio-value", "100000"])

    assert (first, second) == (1, 1)
    for payload in (first_payload, second_payload):
        assert payload["data_status"] == "unavailable"
        assert payload["valuation_status"] == "unavailable"
        assert payload["review_status"] == "blocked"
        assert payload["action_status"] == "review_candidate"
    assert not database.exists()


def test_frozen_five_holding_replay_freezes_new_risk_without_trade_candidate(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    fixture = json.loads((FIXTURES / "five-holding-snapshot.json").read_text())
    _seed_holding(isolated_cache_database, "000963", shares=800, closed=True)
    holding_ids = {
        item["code"]: _seed_holding(
            isolated_cache_database,
            item["code"],
            shares=item["shares"],
        )
        for item in fixture["holdings"]
    }
    due_items = [item for item in fixture["holdings"] if item.get("review_due")]
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.executemany(
            """INSERT INTO holding_alerts
               (holding_id, code, level, category, reason_code, reason, status,
                evidence, opened_at, review_due, updated_at)
               VALUES (?, ?, 'yellow', 'unverified', 'technical_review',
                       'existing technical signal requires review', 'active',
                       'fixture', ?, '2000-01-01', ?)""",
            [
                (
                    holding_ids[item["code"]],
                    item["code"],
                    domain.utc_now().isoformat(),
                    domain.utc_now().isoformat(),
                )
                for item in due_items
            ],
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(isolated_cache_database.read_bytes()).hexdigest()
    quote_calls: list[list[str]] = []

    def fetch(codes: list[str]) -> dict:
        quote_calls.append(codes)
        return _quotes(codes)

    mock_price_quotes(monkeypatch, fetch)

    result, payload = _run(
        capsys, ["--portfolio-value", str(fixture["portfolio_value"])]
    )

    expected = {item["code"]: item["shares"] for item in fixture["holdings"]}
    assert result == 0
    assert {item["code"]: item["shares"] for item in payload["holdings"]} == expected
    assert len(payload["holdings"]) == 5
    assert payload["manifest"]["active_holdings"] == 5
    assert payload["manifest"]["closed_holdings"] == 1
    assert quote_calls == [sorted(expected)]
    assert payload["data_status"] == "complete"
    assert payload["review_status"] == "review_required"
    assert payload["action_status"] == "review_candidate"
    assert payload["requires_user_confirmation"] is False
    assert {(item["code"], item["reason_code"]) for item in payload["escalations"]} == {
        ("603606", "alert_review_due"),
        ("002050", "alert_review_due"),
        *((code, "risk_budget_exceeded") for code in expected),
        (None, "risk_budget_exceeded"),
    }
    assert payload["manifest"]["research_subagents"] == 0
    assert payload["manifest"]["writes"] is False
    assert hashlib.sha256(isolated_cache_database.read_bytes()).hexdigest() == before


def test_production_adapter_derives_industry_change_in_the_same_batch(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database, "600036")

    class FakeProvider:
        def read_cached_industry_map(self):
            return SimpleNamespace(
                status="ok",
                value={"600036": "fixture", "600000": "fixture"},
            )

    monkeypatch.setattr(commands_holdings, "TushareFundamentalsProvider", FakeProvider)
    calls: list[list[str]] = []

    def raw_batch(codes: list[str]) -> dict:
        calls.append(codes)
        return {
            "600036": (98.0, domain.cst_today(), "10:00:00", 100.0),
            "600000": (100.0, domain.cst_today(), "10:00:00", 100.0),
        }

    monkeypatch.setattr(
        commands_holdings.market_quotes, "fetch_sina_batch_quotes", raw_batch
    )
    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 0
    assert calls == [["600000", "600036"]]
    assert payload["holdings"][0]["quote"]["industry_change_pct"] == -1.0
    assert payload["holdings"][0]["quote"]["industry_source"] == "sina.industry_mean"
    assert payload["holdings"][0]["quote"]["industry_as_of"] == (
        f"{domain.cst_today()}T10:00:00+08:00"
    )


@pytest.mark.parametrize("previous_close", [None, 0.0, float("nan")])
def test_invalid_previous_close_fails_closed(
    isolated_cache_database, monkeypatch, capsys, previous_close
) -> None:
    _seed_holding(isolated_cache_database)
    quote = market_quotes.PriceQuote(
        100,
        domain.cst_today(),
        "10:00:00",
        "fixture",
        previous_close=previous_close,
        industry_change_pct=0,
    )
    mock_price_quotes(monkeypatch, lambda _codes: {"600036": quote})

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 1
    assert payload["data_status"] == "partial"
    assert payload["review_status"] == "review_required"
    assert payload["action_status"] == "review_candidate"
    assert payload["stop_reason"] != "clean_fast_gate"
    assert any(gap["field"] == "previous_close" for gap in payload["data_gaps"])


def test_missing_stop_loss_makes_account_risk_budget_unknown(
    isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute("UPDATE holdings SET stop_loss_20=NULL WHERE code='600036'")
        conn.commit()
    mock_price_quotes(monkeypatch, lambda _codes: _quotes(["600036"]))

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 1
    assert payload["account"]["total_stop_risk"] is None
    assert payload["account"]["total_stop_risk_pct"] is None
    assert payload["account"]["risk_budget_status"] is None


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "stale",
        "malformed",
        "invalid_member",
        "nan_epoch",
        "cap",
        "holdings_cap",
        "missing_close",
        "stale_member",
        "invalid_time",
    ],
)
def test_monitor_industry_unavailable_stays_within_bounded_requests(
    case, monkeypatch
) -> None:
    failed_cache = case in {"absent", "stale", "malformed", "invalid_member", "nan_epoch"}

    class FakeProvider:
        def read_cached_industry_map(self):
            return SimpleNamespace(
                status="failed" if failed_cache else "ok",
                value=None if failed_cache else {"600036": "bank", "600000": "bank"},
            )

    monkeypatch.setattr(commands_holdings, "TushareFundamentalsProvider", FakeProvider)
    if case in {"cap", "holdings_cap"}:
        monkeypatch.setattr(commands_holdings, "_MONITOR_SYMBOL_LIMIT", 1)
    calls = []

    def fetch(codes):
        calls.append(codes)
        return {
            code: (
                100.0,
                "2000-01-01"
                if case == "stale_member" and code == "600000"
                else domain.cst_today(),
                "invalid"
                if case == "invalid_time" and code == "600000"
                else "10:00:00",
                None if case == "missing_close" and code == "600000" else 100.0,
            )
            for code in codes
        }

    monkeypatch.setattr(
        commands_holdings.market_quotes, "fetch_sina_batch_quotes", fetch
    )
    codes = ["600036", "600000"] if case == "holdings_cap" else ["600036"]
    quotes, industry_result = commands_holdings.fetch_monitor_price_quotes(codes)

    if case == "holdings_cap":
        assert calls == []
        assert quotes == dict.fromkeys(codes)
        assert industry_result is None
    else:
        expanded = case in {"missing_close", "stale_member", "invalid_time"}
        assert calls == [["600000", "600036"] if expanded else ["600036"]]
        quote = quotes["600036"]
        assert quote is not None
        assert quote.industry_change_pct is None
        assert quote.industry_source is None
        assert quote.industry_as_of is None
        assert industry_result is not None


def test_failed_cached_industry_states_never_network_or_write(tmp_path, monkeypatch) -> None:
    class FakeProvider:
        def read_cached_industry_map(self):
            return SimpleNamespace(status="failed", value=None, error_code="CACHE_MISSING")

        def fetch_industry_map(self):
            pytest.fail("cache-only consumer must not use network API")

    monkeypatch.setattr(commands_holdings, "TushareFundamentalsProvider", FakeProvider)
    monkeypatch.setattr(commands_holdings.market_quotes, "fetch_sina_batch_quotes", dict.fromkeys)

    _, result = commands_holdings.fetch_monitor_price_quotes(["600036"])

    assert result.status == "failed"
    assert result.error_code == "CACHE_MISSING"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        (None, "complete"),
        ("CACHE_MISSING", "unavailable"),
        ("CACHE_STALE", "stale"),
        ("CACHE_CORRUPT", "invalid"),
        ("CACHE_MALFORMED", "invalid"),
        ("CACHE_FUTURE_TIMESTAMP", "invalid"),
        ("CACHE_READ_FAILED", "unavailable"),
    ],
)
def test_monitor_snapshot_preserves_industry_cache_status(
    error_code, expected_status, isolated_cache_database
) -> None:
    _seed_holding(isolated_cache_database)
    result = SimpleNamespace(
        status="ok" if error_code is None else "failed",
        value={"600036": "bank"} if error_code is None else None,
        error_code=error_code,
        freshness_days=2 if error_code in {None, "CACHE_STALE"} else None,
        source="tushare.stock_basic",
        fetched_at="2026-09-13T00:00:00+08:00",
    )

    payload = commands_monitor.build_monitor_snapshot(
        commands_monitor._load_monitor_local_snapshot(),
        _quotes(["600036"]),
        100000,
        industry_result=result,
    )

    assert payload["industry_context"]["status"] == expected_status
    assert payload["industry_context"]["error_code"] == error_code
    industry_gaps = [
        gap for gap in payload["data_gaps"] if gap["field"] == "industry_context"
    ]
    assert bool(industry_gaps) is (error_code is not None)
    if error_code is not None:
        assert error_code in industry_gaps[0]["minimum_action"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("shares", None),
        ("stop_loss_20", 0),
        ("price", float("nan")),
    ],
)
def test_partial_account_stop_risk_is_never_reported_as_within_budget(
    field, value, isolated_cache_database
) -> None:
    _seed_holding(isolated_cache_database, "600036")
    _seed_holding(isolated_cache_database, "600000")
    local = commands_monitor._load_monitor_local_snapshot()
    quotes = _quotes(["600000", "600036"])
    if field == "price":
        quotes["600000"] = (
            None
            if value is None
            else market_quotes.PriceQuote(
                value,
                domain.cst_today(),
                "10:00:00",
                "fixture",
                previous_close=100,
                industry_change_pct=0,
            )
        )
    else:
        local["holdings"][0][field] = value

    payload = commands_monitor.build_monitor_snapshot(local, quotes, 100000)

    assert payload["account"]["total_stop_risk"] is None
    assert payload["account"]["total_stop_risk_pct"] is None
    assert payload["account"]["risk_budget_status"] is None
    assert payload["data_status"] != "complete"
    if field == "stop_loss_20":
        assert payload["valuation_status"] == "exact"


@pytest.mark.parametrize(
    "framework,path,target,price,reached",
    [
        ("A通用", None, None, 124.99, False),
        ("A通用", None, None, 125, True),
        ("E消费", None, None, 124.99, False),
        ("E消费", None, None, 125, True),
        ("F科技", None, None, 124.99, False),
        ("F科技", None, None, 125, True),
        ("B银行", None, None, 125, False),
        ("D公用", None, None, 125, False),
        ("A通用", "C", None, 124.99, False),
        ("A通用", "C", None, 125, True),
        ("A通用", "B", 10, 109.99, False),
        ("A通用", "B", 10, 110, True),
        ("D公用", "B", 20, 120, True),
    ],
)
def test_tier_review_price_entrances(
    framework,
    path,
    target,
    price,
    reached,
    isolated_cache_database,
    monkeypatch,
    capsys,
) -> None:
    _seed_holding(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute("UPDATE holdings SET framework=?, cost_price=50", (framework,))
        conn.execute(
            "UPDATE holding_tier_state SET exit_path=?, exit_target_pct=?",
            (path, target),
        )
        before = conn.execute("SELECT * FROM holding_tier_state").fetchall()
    mock_price_quotes(monkeypatch, lambda codes: _quotes(codes, price=price))

    # Isolate Tier routing from the separately tested budget upgrade.
    result, payload = _run(capsys, ["--portfolio-value", "300000"])

    assert result == 0
    assert payload["action_status"] == ("review_candidate" if reached else "no_action")
    assert payload["stop_reason"] == (None if reached else "clean_fast_gate")
    assert bool(payload["escalations"]) is reached
    assert all(
        item["reason_code"] == "tier_review_due" for item in payload["escalations"]
    )
    assert all(item["candidate"] == "review" for item in payload["escalations"])
    assert payload["requires_user_confirmation"] is False
    with sqlite3.connect(isolated_cache_database) as conn:
        assert conn.execute("SELECT * FROM holding_tier_state").fetchall() == before


@pytest.mark.parametrize("change", [
    "DELETE FROM holding_tier_state",
    "UPDATE holdings SET reference_cost=NULL",
    "UPDATE holding_tier_state SET exit_path='B', exit_target_pct=0",
    "UPDATE holding_tier_state SET exit_path='A'",
])
def test_tier_missing_configuration_or_evidence_fails_closed(
    change, isolated_cache_database, monkeypatch, capsys
) -> None:
    _seed_holding(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute(change)
    mock_price_quotes(monkeypatch, _quotes)

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert result == 1
    assert payload["data_status"] == "partial"
    assert payload["action_status"] == "review_candidate"
    assert payload["stop_reason"] is None
    assert any(item["field"] == "tier" for item in payload["data_gaps"])
    assert any(
        item["reason_code"] == "tier_review_due" for item in payload["escalations"]
    )


@pytest.mark.parametrize("status", ["completed", "exempted"])
@pytest.mark.parametrize("tier2", ["pending", "completed"])
@pytest.mark.parametrize("tier3", ["pending", "completed"])
def test_later_tier_pending_does_not_block_clean_gate_or_repeat_tier1(
    status,
    tier2,
    tier3,
    isolated_cache_database,
    monkeypatch,
    capsys,
) -> None:
    _seed_holding(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute(
            """UPDATE holding_tier_state SET tier1_status=?,
               tier2_status=?, tier3_status=?""",
            (status, tier2, tier3),
        )
        before = conn.execute("SELECT * FROM holding_tier_state").fetchall()
    mock_price_quotes(monkeypatch, lambda codes: _quotes(codes, price=125))
    result, payload = _run(capsys, ["--portfolio-value", "300000"])
    assert result == 0
    assert payload["data_status"] == "complete"
    assert payload["review_status"] == "cleared"
    assert payload["action_status"] == "no_action"
    assert payload["data_gaps"] == []
    assert payload["escalations"] == []
    assert payload["stop_reason"] == "clean_fast_gate"
    assert payload["requires_user_confirmation"] is False
    assert payload["manifest"]["writes"] is False
    with sqlite3.connect(isolated_cache_database) as conn:
        assert conn.execute("SELECT * FROM holding_tier_state").fetchall() == before


@pytest.mark.parametrize(
    "first,second,price,reason",
    [
        (None, 80, 100, None),
        (None, 80, 79, "price_stop_2"),
        (85, None, 84, "price_stop_1"),
        (0, 80, 100, None),
    ],
)
def test_stop_tiers_use_their_own_validated_fields(
    first,
    second,
    price,
    reason,
    isolated_cache_database,
    monkeypatch,
    capsys,
) -> None:
    _seed_holding(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute(
            "UPDATE holdings SET stop_loss_15=?, stop_loss_20=?", (first, second)
        )
    mock_price_quotes(monkeypatch, lambda codes: _quotes(codes, price=price))
    result, payload = _run(capsys, ["--portfolio-value", "100000"])
    assert result == 1
    assert payload["data_status"] == "partial"
    assert payload["holdings"][0]["quote"]["price"] == price
    reasons = {item["reason_code"] for item in payload["escalations"]}
    assert reasons & {"price_stop_1", "price_stop_2"} == ({reason} if reason else set())
    assert payload["stop_reason"] is None


@pytest.mark.parametrize("bad_field", ["quote_time", "quote_date"])
def test_invalid_holding_timestamp_blocks_monitor_with_sufficient_industry_coverage(
    bad_field,
    isolated_cache_database,
    monkeypatch,
    capsys,
) -> None:
    _seed_holding(isolated_cache_database)
    codes = ["600036", "600000", "600001", "600002", "600003"]

    class FakeProvider:
        def read_cached_industry_map(self):
            return SimpleNamespace(status="ok", value=dict.fromkeys(codes, "bank"))

    monkeypatch.setattr(commands_holdings, "TushareFundamentalsProvider", FakeProvider)
    raw = {code: (100.0, domain.cst_today(), "10:00:00", 100.0) for code in codes}
    raw["600036"] = (
        100.0,
        "invalid" if bad_field == "quote_date" else domain.cst_today(),
        "25:00:00" if bad_field == "quote_time" else "10:00:00",
        100.0,
    )
    calls = []

    def fetch(requested):
        calls.append(requested)
        return raw

    monkeypatch.setattr(
        commands_holdings.market_quotes, "fetch_sina_batch_quotes", fetch
    )
    quotes, industry_result = commands_holdings.fetch_monitor_price_quotes(["600036"])
    quote = quotes["600036"]

    assert industry_result is not None
    assert quote.industry_change_pct == 0
    calls.clear()

    result, payload = _run(capsys, ["--portfolio-value", "100000"])

    assert calls == [sorted(codes)]
    assert result == 1
    assert payload["data_status"] in {"unavailable", "stale"}
    assert payload["review_status"] == "blocked"
    assert payload["quote_coverage"]["priced"] == 0
    assert payload["holdings"][0]["quote"]["price"] is None
    assert payload["holdings"][0]["quote"]["status"] in {"unavailable", "stale"}
    assert payload["stop_reason"] is None
    assert any(item["reason_code"] == "quote_gap" for item in payload["escalations"])


@pytest.mark.parametrize(
    "cadence", ["quarterly", "semiannual", "annual", "event", "unspecified"]
)
def test_snapshot_l3_due_includes_evidence_cadence(
    isolated_cache_database, monkeypatch, capsys, cadence
) -> None:
    _seed_holding(isolated_cache_database, next_review="2000-01-01")
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute("UPDATE holding_l3_conditions SET evidence_cadence=?", (cadence,))
    mock_price_quotes(monkeypatch, _quotes)
    result, payload = _run(capsys, ["--portfolio-value", "100000"])
    assert result == 0
    assert payload["holdings"][0]["l3"]["conditions"][0]["evidence_cadence"] == cadence
    due = [
        item
        for item in payload["escalations"]
        if item["reason_code"] == "l3_review_due"
    ]
    assert len(due) == 1
    assert due[0]["evidence_cadence"] == cadence
