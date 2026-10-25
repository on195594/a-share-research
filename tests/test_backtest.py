"""Dated records in force on a day, from TuShare namechange for 600145.SH captured 2026-10-25."""

import pandas as pd

from a_share_research.backtest import _on

NAMES = pd.DataFrame([
    {"ts_code": "600145.SH", "name": "退市新亿", "start_date": "20220330", "end_date": None},
    {"ts_code": "600145.SH", "name": "*ST新亿", "start_date": "20150701", "end_date": "20220329"},
    {"ts_code": "600145.SH", "name": "*ST国创", "start_date": "20140505", "end_date": "20150630"},
    {"ts_code": "600145.SH", "name": "国创能源", "start_date": "20120829", "end_date": "20140504"},
])


def test_name_in_force_on_a_day():
    name = lambda day: _on(NAMES, day, "start_date", "end_date")["name"].get("600145.SH")
    assert name("20130101") == "国创能源"
    assert name("20150630") == "*ST国创"
    assert name("20150701") == "*ST新亿"
    assert name("20261023") == "退市新亿"
    assert name("20110101") is None
