"""Point-in-time names and backtest dates from captured TuShare records."""

import json
import pickle
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import backtest
from a_share_research.backtest import NAME_FIELDS, Market, _on

NAMES = pd.DataFrame([
    {"ts_code": "600145.SH", "name": "退市新亿", "start_date": "20220330", "end_date": None},
    {"ts_code": "600145.SH", "name": "*ST新亿", "start_date": "20150701", "end_date": "20220329"},
    {"ts_code": "600145.SH", "name": "*ST国创", "start_date": "20140505", "end_date": "20150630"},
    {"ts_code": "600145.SH", "name": "国创能源", "start_date": "20120829", "end_date": "20140504"},
])

# 000418.SZ as the old offset-paged bulk download returned it: the row in force since 20061009 was lost.
PAGED_000418 = pd.DataFrame([
    {"ts_code": "000418.SZ", "name": "G小天鹅", "start_date": "20060807", "end_date": "20061008"},
    {"ts_code": "000418.SZ", "name": "小天鹅A", "start_date": "20040512", "end_date": "20060806"},
])
SINGLE_000418 = pd.concat([pd.DataFrame([
    {"ts_code": "000418.SZ", "name": "小天鹅A", "start_date": "20061009", "end_date": None}]), PAGED_000418])


def test_name_in_force_on_a_day():
    name = lambda day: _on(NAMES, day, "start_date", "end_date")["name"].get("600145.SH")
    assert name("20130101") == "国创能源"
    assert name("20150630") == "*ST国创"
    assert name("20150701") == "*ST新亿"
    assert name("20261023") == "退市新亿"
    assert name("20110101") is None


def test_missing_dated_name_is_looked_up_per_stock_never_taken_from_today(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "CACHE", tmp_path)
    m = Market.__new__(Market)
    m.pro, m.names = None, pd.concat([NAMES, PAGED_000418])
    backtest.cache_path("namechange", {"ts_code": "000418.SZ", "fields": NAME_FIELDS}).write_bytes(
        pickle.dumps(SINGLE_000418))
    backtest.cache_path("namechange", {"ts_code": "600000.SH", "fields": NAME_FIELDS}).write_bytes(
        pickle.dumps(SINGLE_000418.iloc[0:0]))
    names = m.name_on(["000418.SZ", "600145.SH", "600000.SH"], "20160129")
    assert names.to_dict() == {"600145.SH": "*ST新亿", "000418.SZ": "小天鹅A"}  # 600000.SH: no dated name, left out


def test_only_completed_months_with_entry_bars_are_selected():
    captured = json.loads((Path(__file__).parent / "data/backtest-dates.json").read_text())
    m = Market.__new__(Market)
    m.days = captured["days"]
    assert m.month_ends("202609", "202610") == ["20260930"]
    assert m.next_day("20260930") == "20261008"
    # Even on a real month-end, entry must wait for the next month's first session.
    m.days = [d for d in m.days if d <= "20260930"]
    assert m.month_ends("202609", "202609") == []
    with pytest.raises(backtest.ScreenError, match="尚无入场交易日"):
        backtest.batch(m, "20260930", 4)


def test_saved_batch_is_recomputed_only_when_a_missing_horizon_is_due():
    captured = json.loads((Path(__file__).parent / "data/backtest-dates.json").read_text())
    m = Market.__new__(Market)
    complete, partial = captured["batches"]
    m.days = captured["days"]
    assert backtest.batch_is_current(m, complete)
    assert backtest.batch_is_current(m, partial)  # 2025-10's 12m is not due yet.
    # Replay the earlier observation cutoff with an actual later batch result.
    due = complete["horizons"]["12m"]["date"]
    missing = complete | {"horizons": {"6m": complete["horizons"]["6m"]}}
    m.days = [d for d in captured["days"] if d < due]
    assert backtest.batch_is_current(m, missing)
    m.days = [d for d in captured["days"] if d <= due]
    assert not backtest.batch_is_current(m, missing)
    assert backtest.batch_is_current(m, complete)
