"""Dated records in force on a day, from TuShare namechange captured 2026-10-25."""

import pickle

import pandas as pd

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
