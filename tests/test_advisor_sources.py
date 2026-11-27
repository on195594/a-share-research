"""Boundary checks over captured daily_basic and report_rc responses; no upstream calls."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
import requests

from a_share_research import market, db, sources, valuation
from support.market_replay import CALENDAR, DATA, ROWS, DailyReports, budget_calls

MARKET = json.loads((DATA / "600519-financials.json").read_text())["market"]


def test_report_rc_boundary_preserves_verified_np_unit_and_dates():
    rows = market.report_rc_rows(pd.DataFrame(ROWS), code="603606")
    assert len(rows) == 69
    huatai = next(r for r in rows if r["org_name"] == "华泰证券" and r["quarter"] == "2028Q4"
                 and r["report_date"] == "2026-10-30")
    assert huatai["np"] == 217800
    assert huatai["np"] / 10000 == 21.78
    assert "tp" not in huatai and "report_title" not in huatai


def test_report_rc_boundary_filters_company_and_rejects_schema():
    assert market.report_rc_rows(pd.DataFrame(ROWS), code="600519") == []
    with pytest.raises(sources.SourceError, match="缺少"):
        market.report_rc_rows(pd.DataFrame(ROWS).drop(columns="np"), code="603606")


class CapturedReportRC:
    """Replay the saved real response at the existing cache boundary."""
    def __init__(self):
        self.calls = 0

    def query(self, api, **params):
        assert api == "report_rc" and params["ts_code"] == "603606.SH"
        self.calls += 1
        return pd.DataFrame(ROWS)


def test_historical_report_rc_uses_existing_raw_cache(monkeypatch):
    monkeypatch.delenv("TUSHARE_CACHE", raising=False)
    first, second = CapturedReportRC(), CapturedReportRC()
    params = {"ts_code": "603606.SH", "start_date": "20260701", "end_date": "20261031"}
    original = market.query(first, "report_rc", **params)
    restored = market.query(second, "report_rc", **params)
    pd.testing.assert_frame_equal(original, restored, check_exact=True)
    assert first.calls == 1 and second.calls == 0


def test_pe_reference_requires_distinct_positive_trading_days():
    # This saved market response is only one day; repeating it cannot meet coverage.
    for frame in (pd.DataFrame([MARKET]), pd.DataFrame([MARKET] * 250)):
        with pytest.raises(sources.SourceError, match="仅 1 个交易日，少于 250"):
            sources.pe_quantiles(frame, "600519", MARKET["trade_date"])


@pytest.mark.parametrize("value", [None, 0, -1, float("inf")])
def test_pe_reference_drops_nonpositive_and_missing_observations(value):
    frame = pd.DataFrame([MARKET])
    frame["pe_ttm"] = value  # Mutation of the captured row, not a fabricated history.
    with pytest.raises(sources.SourceError, match="仅 0 个交易日"):
        sources.pe_quantiles(frame, "600519", MARKET["trade_date"])


def test_pe_reference_rejects_wrong_company():
    with pytest.raises(sources.SourceError, match="其他公司"):
        sources.pe_quantiles(pd.DataFrame([MARKET]), "603606", MARKET["trade_date"])


@pytest.fixture
def daily_reports(monkeypatch):
    monkeypatch.delenv("TUSHARE_CACHE", raising=False)
    monkeypatch.delenv("REPORT_RC_DAILY_BUDGET", raising=False)
    # These are daily-budget/cache replays. Dedicated tests below exercise the
    # real minute pacing with controlled wall time and independent processes.
    monkeypatch.setattr(market, "REPORT_RC_MIN_INTERVAL", 0)
    monkeypatch.setenv("TUSHARE_TOKEN", "captured-response-only")
    pro = DailyReports()
    monkeypatch.setattr(market, "client", lambda: pro)
    return pro


def test_window_budget_accumulates_across_calls_and_recent_days_first(daily_reports):
    first = sources.report_rc_window("2026-09-18", days=1, code="603606")
    assert daily_reports.calls == ["20260918", "20260917"]
    assert first["covered_days"] == 2 and budget_calls() == 2
    second = sources.report_rc_window("2026-09-18", code="603606")
    expected = sorted([d for d in CALENDAR if "20260620" <= d <= "20260918"], reverse=True)
    # Both populated and successfully queried empty days are shared coverage.
    assert daily_reports.calls[2:] == expected[2:8]
    assert budget_calls() == market.REPORT_RC_DAILY_BUDGET == 8
    assert second["covered_days"] == 8
    assert second["missing_days"] == expected[8:]
    assert "20260917" in second["empty_days"]
    assert "预算" in second["reason"]
    third = sources.report_rc_window("2026-09-18", code="603606")
    assert budget_calls() == 8 and len(daily_reports.calls) == 8
    assert third["covered_days"] == 8  # Empty days no longer starve older missing days.
    captured = pd.DataFrame(ROWS)
    expected_rows = market.report_rc_rows(captured.loc[captured["report_date"].isin(expected[:8])], code="603606")
    assert third["rows"] == sorted(expected_rows, key=lambda row: row["report_date"], reverse=True)


def test_budget_override_and_older_cache_remain_visible_after_stop(daily_reports, monkeypatch):
    market.query(daily_reports, "report_rc", start_date="20260909", end_date="20260909")
    monkeypatch.setenv("REPORT_RC_DAILY_BUDGET", "1")
    result = sources.report_rc_window("2026-09-18", code="603606")
    assert result["covered_days"] == 1 and "20260909" not in result["missing_days"]
    assert daily_reports.calls == ["20260909"] and budget_calls() == 1
    assert {r["report_date"] for r in result["rows"]} == {"2026-09-09"}


@pytest.mark.parametrize("message", ["频次超出", "限频", "超限", "每分钟最多访问1次", "每小时最多访问10次", "每天最多访问10次"])
def test_rate_limit_stops_immediately_but_keeps_cached_days(daily_reports, message, monkeypatch):
    market.query(daily_reports, "report_rc", start_date="20260909", end_date="20260909")
    daily_reports.error = message
    # Any old 61-second retry, or other waiting, fails this test immediately.
    def no_wait(seconds):
        pytest.fail(f"unexpected sleep: {seconds}")
    monkeypatch.setattr(time, "sleep", no_wait)
    result = sources.report_rc_window("2026-09-18", code="603606")
    assert daily_reports.calls == ["20260909", "20260918"] and budget_calls() == 2
    assert result["covered_days"] == 1 and message in result["reason"]
    assert "20260909" not in result["missing_days"]


def test_window_filters_company_and_marks_cap_without_dropping_rows(daily_reports, monkeypatch):
    count = sum(r["report_date"] == "20260918" for r in ROWS)
    # Lower the threshold to this real day's row count; never invent a 3000-row response.
    monkeypatch.setattr(market, "REPORT_RC_ROW_CAP", count)
    result = sources.report_rc_window("2026-09-18", days=0, code="603606")
    assert len(result["rows"]) == count and result["truncated_days"] == ["20260918"]
    assert {r["ts_code"] for r in result["rows"]} == {"603606.SH"}
    other = sources.report_rc_window("2026-09-18", days=0, code="600519")
    assert other["rows"] == [] and other["covered_days"] == 1
    assert other["truncated_days"] == ["20260918"] and budget_calls() == 1
    monkeypatch.setattr(market, "REPORT_RC_ROW_CAP", count + 1)
    assert sources.report_rc_window("2026-09-18", days=0)["truncated_days"] == []


def test_window_excludes_today_and_future_and_resets_budget_on_shanghai_day(daily_reports, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 18, 0, 1, tzinfo=db.SHANGHAI)

        @classmethod
        def now(cls, tz=None):
            assert tz == db.SHANGHAI
            return cls.current

    monkeypatch.setattr(sources, "datetime", Clock)
    monkeypatch.setattr(market, "datetime", Clock)
    monkeypatch.setenv("REPORT_RC_DAILY_BUDGET", "1")
    result = sources.report_rc_window("2026-09-19", days=3)
    assert daily_reports.calls == ["20260917"] and result["trading_days"] == 2
    Clock.current += timedelta(days=1)
    result = sources.report_rc_window("2026-09-18", days=0)
    assert daily_reports.calls == ["20260917", "20260918"] and result["covered_days"] == 1
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        assert conn.execute("SELECT day,calls FROM report_rc_budget ORDER BY day").fetchall() == [
            ("2026-09-18", 1), ("2026-09-19", 1)]


@pytest.mark.parametrize("budget,code,status", [(0, "603606", "unavailable"), (1, "603606", "partial"),
                                                (1, "600519", "partial"), (8, "600519", "none"), (8, "603606", "ok")])
def test_consensus_coverage_states_from_daily_responses(daily_reports, monkeypatch, budget, code, status):
    monkeypatch.setenv("REPORT_RC_DAILY_BUDGET", str(budget))
    window = sources.report_rc_window("2026-08-26", days=1, code=code)
    result = valuation.consensus(window["rows"], [2026, 2027, 2028], "2026-08-26", coverage=window)
    assert result["status"] == status
    assert result["coverage"] == {k: window[k] for k in ("trading_days", "covered_days", "truncated_days")}
    assert bool(result["years"]) == (budget > 0 and code == "603606")


def test_budget_is_atomic_across_independent_processes(daily_reports):
    script = """
from a_share_research import market
market.REPORT_RC_MIN_INTERVAL = 0  # Isolate the daily-cap race from minute pacing.
accepted = 0
for _ in range(4):
    try:
        market._reserve_report_rc_call()
        accepted += 1
    except market.ReportRCBudgetExhausted:
        pass
print(accepted)
"""
    children = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env={**os.environ, "DATA_DIR": str(db.DATA)}) for _ in range(4)]
    outputs = [child.communicate(timeout=30) for child in children]
    assert all(child.returncode == 0 for child in children), outputs
    assert sum(int(out) for out, _ in outputs) == 8 and budget_calls() == 8


def test_research_passes_market_date_and_company_and_replay_never_fetches(daily_reports, monkeypatch):
    from support.valuation import assumptions, material

    from a_share_research import research

    original = sources.report_rc_window
    calls = []

    def window(as_of, **kwargs):
        calls.append((as_of, kwargs))
        return original(as_of, **kwargs)

    monkeypatch.setattr(sources, "report_rc_window", window)
    mat = material()
    result = research.calculate_valuation(assumptions(), mat)
    assert calls == [(mat.market_date, {"code": mat.code})]
    assert result["consensus"]["status"] == "partial"
    assert result["consensus"]["coverage"]["covered_days"] == 8
    research.calculate_valuation(assumptions(), mat, replay=True)
    assert len(calls) == 1 and budget_calls() == 8


def test_minute_slot_is_atomic_across_processes_and_restart(daily_reports):
    script = """
from a_share_research import market
try:
    market._reserve_report_rc_call()
    print(1)
except market.ReportRCCoolingDown:
    print(0)
"""
    env = {**os.environ, "DATA_DIR": str(db.DATA)}
    children = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env) for _ in range(4)]
    outputs = [child.communicate(timeout=30) for child in children]
    assert all(child.returncode == 0 for child in children), outputs
    assert sum(int(out) for out, _ in outputs) == 1 and budget_calls() == 1
    restarted = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert restarted.returncode == 0 and restarted.stdout.strip() == "0"
    assert budget_calls() == 1


def test_recent_empty_day_retries_daily(daily_reports, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 18, 9, tzinfo=db.SHANGHAI)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(sources, "datetime", Clock)
    monkeypatch.setattr(market, "datetime", Clock)
    params = dict(start_date="20260917", end_date="20260917")
    market.query(daily_reports, "report_rc", **params)
    with closing(market.tushare_connect()) as conn:
        conn.execute("UPDATE tushare_cache SET created_at=? WHERE key=?",
                     (Clock.current.isoformat(), market._tushare_cache_key("report_rc", params)))
    Clock.current += timedelta(hours=23)
    assert market.cached_response("report_rc", params).empty
    Clock.current += timedelta(hours=2)
    assert market.cached_response("report_rc", params) is None


@pytest.mark.parametrize("message,seconds", [
    ("抱歉，您每分钟最多访问该接口1次", 61),
    ("抱歉，您访问接口(report_rc)频率超限(1次/分钟)", 61),
    ("每小时最多访问10次", 3601),
    ("连接超时", 300),
])
def test_upstream_failure_sets_durable_cooldown(daily_reports, monkeypatch, message, seconds):
    monkeypatch.setattr(market, "REPORT_RC_MIN_INTERVAL", 61)
    daily_reports.error = message
    before = datetime.now(db.SHANGHAI)
    result = sources.report_rc_window("2026-09-18")
    assert daily_reports.calls == ["20260918"] and result["covered_days"] == 0
    assert (datetime.fromisoformat(market.report_rc_status()["retry_at"]) - before).total_seconds() >= seconds
    sources.report_rc_window("2026-09-18")
    assert daily_reports.calls == ["20260918"] and budget_calls() == 1
    assert market.cached_response("report_rc", dict(start_date="20260918", end_date="20260918")) is None


def test_provider_daily_limit_defers_to_next_shanghai_day(daily_reports):
    daily_reports.error = "每天最多访问10次"
    sources.report_rc_window("2026-09-18")
    retry = datetime.fromisoformat(market.report_rc_status()["retry_at"])
    assert retry.date() == datetime.now(db.SHANGHAI).date() + timedelta(days=1)
    assert retry.hour == retry.minute == retry.second == 0


def test_wrong_day_response_is_not_cached_or_counted_as_coverage(daily_reports, monkeypatch):
    original = daily_reports.query

    def wrong_day(api, **params):
        if api == "report_rc":
            return pd.DataFrame(ROWS)  # Real rows from multiple dates, not this day.
        return original(api, **params)

    monkeypatch.setattr(daily_reports, "query", wrong_day)
    result = sources.report_rc_window("2026-09-18", days=0)
    assert result["covered_days"] == 0 and result["missing_days"] == ["20260918"]
    assert "其他日期" in result["reason"]
    assert market.cached_response("report_rc", dict(start_date="20260918", end_date="20260918")) is None


def test_truncated_window_cannot_claim_complete_consensus(daily_reports, monkeypatch):
    count = sum(r["report_date"] == "20260918" for r in ROWS)
    monkeypatch.setattr(market, "REPORT_RC_ROW_CAP", count)
    window = sources.report_rc_window("2026-09-18", days=0)
    result = valuation.consensus(window["rows"], [2026, 2027, 2028], "2026-09-18", coverage=window)
    assert result["status"] == "partial"


def test_idle_backfill_resumes_one_day_per_tick_and_across_budget_days(daily_reports, monkeypatch):
    from a_share_research import consensus_backfill

    class Clock(datetime):
        current = datetime(2026, 9, 19, 9, tzinfo=db.SHANGHAI)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(sources, "datetime", Clock)
    monkeypatch.setattr(market, "datetime", Clock)
    monkeypatch.setattr(consensus_backfill, "datetime", Clock)
    monkeypatch.setattr(market, "REPORT_RC_MIN_INTERVAL", 61)
    monkeypatch.setattr(consensus_backfill, "_next_tick", 0)
    expected = sorted([d for d in CALENDAR if "20260620" <= d <= "20260918"], reverse=True)
    for n in range(1, 9):
        monkeypatch.setattr(consensus_backfill, "_next_tick", 0)
        assert consensus_backfill.tick()
        assert len(daily_reports.calls) == n and daily_reports.calls == expected[:n]
        state = consensus_backfill.status()
        assert state["backfill"]["covered_days"] == n
        assert state["pacing"]["used"] == n
        # Even with a reset in-memory timer, persisted scheduling prevents a call.
        monkeypatch.setattr(consensus_backfill, "_next_tick", 0)
        assert not consensus_backfill.tick()
        Clock.current += timedelta(seconds=62)
    assert "20260917" in state["backfill"]["empty_days"]
    monkeypatch.setattr(consensus_backfill, "_next_tick", 0)
    assert not consensus_backfill.tick() and len(daily_reports.calls) == 8
    Clock.current += timedelta(days=1)
    monkeypatch.setattr(consensus_backfill, "_next_tick", 0)
    assert consensus_backfill.tick()
    assert daily_reports.calls == expected[:9] and market.report_rc_status()["used"] == 1
    assert consensus_backfill.status()["backfill"]["covered_days"] == 9


def test_idle_backfill_complete_window_waits_without_new_upstream_calls(daily_reports, monkeypatch):
    from a_share_research import consensus_backfill

    class Clock(datetime):
        current = datetime(2026, 9, 19, 9, tzinfo=db.SHANGHAI)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(sources, "datetime", Clock)
    monkeypatch.setattr(market, "datetime", Clock)
    monkeypatch.setattr(consensus_backfill, "datetime", Clock)
    monkeypatch.setattr(consensus_backfill, "_next_tick", 0)
    monkeypatch.setenv("REPORT_RC_DAILY_BUDGET", "100")
    window = sources.report_rc_window("2026-09-18")
    assert not window["missing_days"]
    calls = list(daily_reports.calls)
    assert consensus_backfill.tick()
    assert consensus_backfill.status()["backfill"]["status"] == "complete"
    assert daily_reports.calls == calls
    Clock.current += timedelta(seconds=62)
    monkeypatch.setattr(consensus_backfill, "_next_tick", 0)
    assert not consensus_backfill.tick()
    assert daily_reports.calls == calls


@pytest.mark.parametrize("historical", [False, True])
def test_cached_query_reuses_only_historical_frames(monkeypatch, historical):
    monkeypatch.setenv("TUSHARE_CACHE", "1")
    frame = pd.DataFrame([MARKET])
    end = MARKET["trade_date"] if historical else datetime.now(db.SHANGHAI).date().isoformat()
    params = {"ts_code": "600519.SH", "end_date": end}
    calls = []

    def fetcher():
        calls.append(1)
        return frame.copy()

    original = market.cached_query("daily_basic", params, fetcher)
    restored = market.cached_query("daily_basic", params, fetcher)
    pd.testing.assert_frame_equal(original, restored)
    restored.iloc[0, restored.columns.get_loc("close")] = -1  # Mutation must not leak into the cache.
    pd.testing.assert_frame_equal(market.cached_query("daily_basic", params, fetcher), frame)
    assert len(calls) == (1 if historical else 3)
    assert (db.DATA / "tushare_cache.db").exists() is historical


@pytest.mark.parametrize("invalid", [None, "not-a-frame", "empty"])
def test_cached_query_does_not_persist_invalid_results(monkeypatch, invalid):
    monkeypatch.setenv("TUSHARE_CACHE", "1")
    value = pd.DataFrame([MARKET]).iloc[:0] if invalid == "empty" else invalid
    market.cached_query("daily_basic", {"end_date": MARKET["trade_date"]}, lambda: value)
    assert not (db.DATA / "tushare_cache.db").exists()


def test_cached_query_disabled_and_failure_do_not_write(monkeypatch):
    params = {"end_date": MARKET["trade_date"]}
    calls = []
    for _ in range(2):
        market.cached_query("daily_basic", params, lambda: calls.append(1) or pd.DataFrame([MARKET]))
    assert len(calls) == 2 and not (db.DATA / "tushare_cache.db").exists()
    monkeypatch.setenv("TUSHARE_CACHE", "1")

    def fail():
        raise ConnectionError("upstream disconnected")

    with pytest.raises(ConnectionError):
        market.cached_query("daily_basic", params, fail)
    assert not (db.DATA / "tushare_cache.db").exists()


@pytest.mark.parametrize("age", [0, 14, 15])
def test_combined_valuation_fetches_daily_basic_once(monkeypatch, age):
    close_day = date.fromisoformat(MARKET["trade_date"])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.combine(close_day + timedelta(days=age), cls.min.time(), tz)

    monkeypatch.setattr(sources, "datetime", Clock)
    monkeypatch.setenv("TUSHARE_TOKEN", "captured-response-only")
    calls = []
    # Replay a captured raw row at the HTTP boundary through the real provider.
    body = json.dumps({"code": 0, "data": {"fields": list(MARKET), "items": [list(MARKET.values())]}}).encode()

    def post(session, url, json, timeout):
        calls.append(json)
        reply = requests.Response()
        reply.status_code, reply._content = 200, body
        return reply

    monkeypatch.setattr(requests.Session, "post", post)
    if age > 14:
        with pytest.raises(sources.SourceError, match="no valuation rows"):
            sources.valuation_with_history("600519")
        return
    latest, history = sources.valuation_with_history("600519")
    assert latest == MARKET
    # A single captured day cannot supply 250 distinct PE observations; this
    # remains a nonfatal evidence gap in gather and must not trigger another fetch.
    with pytest.raises(sources.SourceError, match="少于 250"):
        sources.pe_quantiles(history, "600519", latest["trade_date"])
    assert len(calls) == 1 and calls[0]["api_name"] == "daily_basic"
    # The one window still reaches five years before a close that is two weeks old.
    boundary = close_day.replace(year=close_day.year - 5)
    assert calls[0]["params"]["start_date"] <= boundary.strftime("%Y%m%d")


def test_metadata_only_window_matches_full_window_without_rows(daily_reports):
    sources.report_rc_window("2026-09-18", days=1)
    full = sources.report_rc_window("2026-09-18", days=1)
    metadata = sources.report_rc_window("2026-09-18", days=1, include_rows=False)
    assert full["rows"] and full["empty_days"] == ["20260917"]
    assert metadata.pop("retry_at") and full.pop("retry_at")
    assert metadata == full | {"rows": []}


def test_cache_follows_data_path(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_CACHE", "1")
    params = {"end_date": MARKET["trade_date"]}
    market.cached_query("daily_basic", params, lambda: pd.DataFrame([MARKET]))
    with closing(market.tushare_connect()) as owned:
        assert owned.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    original = db.DATA
    monkeypatch.setattr(db, "DATA", tmp_path / "other-data")
    assert market.cached_response("daily_basic", params) is None
    other = pd.DataFrame([MARKET | {"close": MARKET["close"] + 1}])
    market.cached_query("daily_basic", params, lambda: other)
    pd.testing.assert_frame_equal(market.cached_response("daily_basic", params), other)
    monkeypatch.setattr(db, "DATA", original)
    pd.testing.assert_frame_equal(market.cached_response("daily_basic", params), pd.DataFrame([MARKET]))
