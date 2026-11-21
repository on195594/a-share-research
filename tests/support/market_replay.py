"""Captured market prices, trading sessions and sell-side reports."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pandas as pd

from a_share_research import db

PRICE_DATA = json.loads((Path(__file__).resolve().parents[1] / "data" / "600900-61d.json").read_text())
DATA = Path(__file__).resolve().parents[1] / "data"
ROWS = json.loads((DATA / "report_rc_603606_20261031.json").read_text())
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


def budget_calls():
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        return conn.execute("SELECT calls FROM report_rc_budget WHERE day=?",
                            (datetime.now(db.SHANGHAI).date().isoformat(),)).fetchone()[0]
