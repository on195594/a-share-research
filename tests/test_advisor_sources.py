"""Boundary checks over captured daily_basic and report_rc responses; no upstream calls."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import db, sources, valuation

DATA = Path(__file__).parent / "data"
ROWS = json.loads((DATA / "report_rc_603606_20261031.json").read_text())
MARKET = json.loads((DATA / "600519-financials.json").read_text())["market"]


def test_report_rc_boundary_preserves_verified_np_unit_and_dates():
    rows = sources.report_rc_rows(pd.DataFrame(ROWS), "603606")
    assert len(rows) == 69
    huatai = next(r for r in rows if r["org_name"] == "华泰证券" and r["quarter"] == "2028Q4"
                 and r["report_date"] == "2026-10-30")
    assert huatai["np"] == 217800
    assert huatai["np"] / 10000 == 21.78
    assert "tp" not in huatai and "report_title" not in huatai


def test_report_rc_boundary_filters_company_and_rejects_schema():
    assert sources.report_rc_rows(pd.DataFrame(ROWS), "600519") == []
    with pytest.raises(sources.SourceError, match="缺少"):
        sources.report_rc_rows(pd.DataFrame(ROWS).drop(columns="np"), "603606")


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
    original = sources.tushare_query(first, "report_rc", **params)
    restored = sources.tushare_query(second, "report_rc", **params)
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


def test_report_rc_limit_message_classification():
    assert sources._report_rc_limited(Exception("抱歉，您每分钟最多访问该接口1次"))
    assert not sources._report_rc_limited(Exception("没有访问权限"))


CALENDAR = json.loads((DATA / "backtest-dates.json").read_text())["days"]


class DailyReports:
    """对真实 report_rc 响应按 report_date 切分，模拟全市场逐日响应；不改预测值。"""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def query(self, api, **params):
        if api == "trade_cal":
            assert params["exchange"] == "SSE" and params["is_open"] == "1"
            return pd.DataFrame({"cal_date": [d for d in CALENDAR if params["start_date"] <= d <= params["end_date"]]})
        assert api == "report_rc"
        assert set(params) == {"start_date", "end_date"}
        assert params["start_date"] == params["end_date"]
        self.calls.append(params["start_date"])
        if self.error:
            raise RuntimeError(self.error)
        return pd.DataFrame([r for r in ROWS if r["report_date"] == params["start_date"]],
                            columns=pd.DataFrame(ROWS).columns)


@pytest.fixture
def daily_reports(monkeypatch):
    monkeypatch.delenv("TUSHARE_CACHE", raising=False)
    monkeypatch.delenv("REPORT_RC_DAILY_BUDGET", raising=False)
    monkeypatch.setenv("TUSHARE_TOKEN", "captured-response-only")
    pro = DailyReports()
    monkeypatch.setattr(sources.ts, "pro_api", lambda token: pro)
    return pro


def budget_calls():
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        return conn.execute("SELECT calls FROM report_rc_budget WHERE day=?",
                            (datetime.now(db.SHANGHAI).date().isoformat(),)).fetchone()[0]


def test_window_budget_accumulates_across_calls_and_recent_days_first(daily_reports):
    first = sources.report_rc_window("2026-09-18", days=1, code="603606")
    assert daily_reports.calls == ["20260918", "20260917"]
    assert first["covered_days"] == 2 and budget_calls() == 2
    second = sources.report_rc_window("2026-09-18", code="603606")
    expected = sorted([d for d in CALENDAR if "20260620" <= d <= "20260918"], reverse=True)
    # The nonempty 18th is cached; the empty 17th is deliberately fetched again.
    assert daily_reports.calls[2:] == expected[1:7]
    assert budget_calls() == sources.REPORT_RC_DAILY_BUDGET == 8
    assert second["covered_days"] == 7
    assert second["missing_days"] == expected[7:]
    assert "预算" in second["reason"]
    third = sources.report_rc_window("2026-09-18", code="603606")
    assert budget_calls() == 8 and len(daily_reports.calls) == 8
    assert third["covered_days"] == 1  # Empty responses are not persistent coverage.
    assert third["rows"] == sources.report_rc_rows(pd.DataFrame(ROWS).query('report_date == "20260918"'), "603606")


def test_budget_override_and_older_cache_remain_visible_after_stop(daily_reports, monkeypatch):
    sources.tushare_query(daily_reports, "report_rc", start_date="20260909", end_date="20260909")
    monkeypatch.setenv("REPORT_RC_DAILY_BUDGET", "1")
    result = sources.report_rc_window("2026-09-18", code="603606")
    assert result["covered_days"] == 1 and "20260909" not in result["missing_days"]
    assert daily_reports.calls == ["20260909"] and budget_calls() == 1
    assert {r["report_date"] for r in result["rows"]} == {"2026-09-09"}


@pytest.mark.parametrize("message", ["频次超出", "限频", "超限", "每分钟最多访问1次", "每小时最多访问10次", "每天最多访问10次"])
def test_rate_limit_stops_immediately_but_keeps_cached_days(daily_reports, message, monkeypatch):
    sources.tushare_query(daily_reports, "report_rc", start_date="20260909", end_date="20260909")
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
    monkeypatch.setattr(sources, "REPORT_RC_ROW_CAP", count)
    result = sources.report_rc_window("2026-09-18", days=0, code="603606")
    assert len(result["rows"]) == count and result["truncated_days"] == ["20260918"]
    assert {r["ts_code"] for r in result["rows"]} == {"603606.SH"}
    other = sources.report_rc_window("2026-09-18", days=0, code="600519")
    assert other["rows"] == [] and other["covered_days"] == 1
    assert other["truncated_days"] == ["20260918"] and budget_calls() == 1
    monkeypatch.setattr(sources, "REPORT_RC_ROW_CAP", count + 1)
    assert sources.report_rc_window("2026-09-18", days=0)["truncated_days"] == []


def test_window_excludes_today_and_future_and_resets_budget_on_shanghai_day(daily_reports, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 18, 0, 1, tzinfo=db.SHANGHAI)

        @classmethod
        def now(cls, tz=None):
            assert tz == db.SHANGHAI
            return cls.current

    monkeypatch.setattr(sources, "datetime", Clock)
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
from a_share_research import sources
accepted = 0
for _ in range(4):
    try:
        sources._reserve_report_rc_call()
        accepted += 1
    except sources.ReportRCBudgetExhausted:
        pass
print(accepted)
"""
    children = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env={**os.environ, "DATA_DIR": str(db.DATA)}) for _ in range(4)]
    outputs = [child.communicate(timeout=30) for child in children]
    assert all(child.returncode == 0 for child in children), outputs
    assert sum(int(out) for out, _ in outputs) == 8 and budget_calls() == 8


def test_research_passes_market_date_and_company_and_replay_never_fetches(daily_reports, monkeypatch):
    from a_share_research import research
    from test_valuation import material
    from support.valuation import assumptions

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
