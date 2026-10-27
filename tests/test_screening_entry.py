"""Real published market replay; synthetic fault injection explicitly authorized for entry boundaries."""
import copy
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import db, screening as s

CAPTURE = json.loads((Path(__file__).parent / "data/screen-entry-market.json").read_text())
DAY, CODE = CAPTURE["trade_date"], CAPTURE["stock"]


@pytest.fixture
def market():
    class Replay:
        def __init__(self):
            self.data = copy.deepcopy(CAPTURE)

        def query(self, api, **params):
            rows = self.data[api][params["ts_code"]] if api == "index_daily" else self.data[api]
            return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["ts_code", "suspend_timing"])

    return Replay()


def test_published_entry_and_benchmarks_keep_original_formula(market):
    assert s.entry_prices(market, DAY, [CODE])[CODE] == {"filled": True, "open": 24.42, "adj": 13.2697}
    entry = s.benchmark_prices(market, DAY, DAY, "open", s.RULE["benchmarks"])
    mark = s.benchmark_prices(market, DAY, DAY, "close", s.RULE["benchmarks"])
    for code, price_code in s.PRICE_INDEX.items():
        total, price = CAPTURE["index_daily"][code][0], CAPTURE["index_daily"][price_code][0]
        assert entry[code] == {"date": DAY, "value": total["pre_close"] * price["open"] / price["pre_close"]}
        assert mark[code] == {"date": DAY, "value": total["close"]}


@pytest.mark.parametrize("code,field", [("H00906.CSI", "close"), ("H00906.CSI", "pre_close"),
                                        ("000906.SH", "open"), ("000906.SH", "pre_close")])
@pytest.mark.parametrize("value", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_bad_benchmark_numbers_are_retryable(market, code, field, value):
    market.data["index_daily"][code][0][field] = value
    with pytest.raises(s.ScreenError):
        s.benchmark_prices(market, DAY, DAY, "open", s.RULE["benchmarks"])


@pytest.mark.parametrize("code", ["H00906.CSI", "000906.SH"])
@pytest.mark.parametrize("field,value", [("trade_date", "20261022"), ("ts_code", "wrong-code")])
def test_benchmark_identity_must_match(market, code, field, value):
    market.data["index_daily"][code][0][field] = value
    with pytest.raises(s.ScreenError):
        s.benchmark_prices(market, DAY, DAY, "open", s.RULE["benchmarks"])


@pytest.mark.parametrize("pre_close,open_price", [(1e308, 1e308), (1e-300, 1e-300)])
def test_derived_benchmark_must_be_finite_positive(market, pre_close, open_price):
    market.data["index_daily"]["H00906.CSI"][0]["pre_close"] = pre_close
    market.data["index_daily"]["000906.SH"][0]["open"] = open_price
    with pytest.raises(s.ScreenError):
        s.benchmark_prices(market, DAY, DAY, "open", s.RULE["benchmarks"])


@pytest.mark.parametrize("timing,filled", [(None, False), ("", False), ("09:30-10:00", False),
                                          ("09:00-09:30", True), ("13:00-13:30", True)])
def test_opening_halt_overrides_later_daily_bar(market, timing, filled):
    market.data["suspend_d"] = [{"ts_code": CODE, "suspend_timing": timing}]
    assert s.entry_prices(market, DAY, [CODE])[CODE]["filled"] is filled


@pytest.mark.parametrize("timing", ["unknown", "10:00-09:30", "09:30-10:00,13:00-14:00", 930])
def test_unknown_suspension_interval_retries_instead_of_guessing(market, timing):
    market.data["suspend_d"] = [{"ts_code": CODE, "suspend_timing": timing}]
    with pytest.raises(s.ScreenError, match="停牌时段无效"):
        s.entry_prices(market, DAY, [CODE])


def test_invalid_benchmark_keeps_frozen_then_retries_with_frozen_rules(market, tmp_path, monkeypatch):
    class Clock(datetime):
        value = datetime.fromisoformat("2026-10-23T17:00:00+08:00")

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz)

    monkeypatch.setattr(s, "datetime", Clock)
    monkeypatch.setattr(db, "now", lambda: Clock.value.isoformat())
    monkeypatch.setattr(s, "_pro", lambda: market)
    conn = db.connect(tmp_path / "synthetic-batch.db")
    try:
        rule = json.dumps(s.RULE)
        conn.execute("INSERT INTO batches (status,created_at,rule,q20,q5,l5,frozen_at) VALUES (?,?,?,?,?,?,?)",
                     ("frozen", db.now(), rule, json.dumps([{"code": CODE}]), json.dumps([CODE]),
                      json.dumps({"codes": [CODE]}), "2026-10-22T18:00:00+08:00"))
        market.data["index_daily"]["H00906.CSI"][0]["close"] = 0
        assert s.tick(conn) is True
        failed = s.load(conn.execute("SELECT * FROM batches").fetchone())
        assert failed["status"] == "frozen" and failed["entry"] is None and failed["error"]
        market.data = copy.deepcopy(CAPTURE)
        monkeypatch.setitem(s.RULE, "benchmarks", {"H00300.CSI": "changed default"})
        assert s.tick(conn) is False
        Clock.value = datetime.fromisoformat("2026-10-23T17:30:00+08:00")
        assert s.tick(conn) is True
        row = conn.execute("SELECT * FROM batches").fetchone()
        tracked = s.load(row)
        assert row["rule"] == rule and tracked["error"] is None
        assert tracked["status"] == "tracking" and tracked["entry"]["date"] == DAY
        assert set(tracked["entry"]["benchmarks"]) == set(json.loads(rule)["benchmarks"])
        assert set(tracked["marks"]["latest"]["benchmarks"]) == set(json.loads(rule)["benchmarks"])
        assert s.returns(tracked)["date"] == DAY
    finally:
        conn.close()


LAGGED = json.loads((Path(__file__).parent / "data/screen-entry-lagged-benchmarks.json").read_text())


class LaggedReplay:
    """2026-10-26 as published at 17:41: price indexes and stocks are out, total-return indexes stop at 10-23.

    `published` adds the later-captured 10-26 total-return rows for those benchmarks, as they appeared that evening.
    """

    def __init__(self, published=()):
        self.published, self.calls = set(published), []

    def query(self, api, **params):
        self.calls.append(api)
        later = LAGGED["published_later"]
        if api == "index_daily":
            code = params["ts_code"]
            rows = LAGGED["index_daily"][code] + (later["index_daily"].get(code, []) if code in self.published else [])
            rows = [r for r in rows if params["start_date"] <= r["trade_date"] <= params["end_date"]]
        elif api == "trade_cal":
            rows = [r for r in later["trade_cal"] if params["start_date"] <= r["cal_date"] <= params["end_date"]]
        else:
            rows = LAGGED[api]
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["ts_code", "suspend_timing", "trade_date"])


def test_entry_open_uses_previous_close_while_total_return_lags():
    day = LAGGED["trade_date"]
    entry = s.benchmark_prices(LaggedReplay(), day, day, "open", s.RULE["benchmarks"])
    for code, price_code in s.PRICE_INDEX.items():
        prev_close = next(r["close"] for r in LAGGED["index_daily"][code] if r["trade_date"] == "20261023")
        price = next(r for r in LAGGED["index_daily"][price_code] if r["trade_date"] == day)
        assert entry[code] == {"date": day, "value": prev_close * price["open"] / price["pre_close"]}
    # Closes still need the day itself.
    with pytest.raises(s.ScreenError, match="未发布"):
        s.benchmark_prices(LaggedReplay(), day, day, "close", s.RULE["benchmarks"])
    assert s.benchmark_day(LaggedReplay(), day, day, s.RULE["benchmarks"]) is None
    assert s.benchmark_day(LaggedReplay(), "20261022", day, s.RULE["benchmarks"]) == "20261023"


def test_lagging_benchmarks_enter_then_mark_once_both_are_published(tmp_path, monkeypatch):
    class Clock(datetime):
        value = datetime.fromisoformat("2026-10-26T17:41:00+08:00")

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz)

    def at(moment):
        Clock.value = datetime.fromisoformat(moment)

    market = LaggedReplay()
    monkeypatch.setattr(s, "datetime", Clock)
    monkeypatch.setattr(db, "now", lambda: Clock.value.isoformat())
    monkeypatch.setattr(s, "_pro", lambda: market)
    code, day = LAGGED["stock"], LAGGED["trade_date"]
    later = LAGGED["published_later"]["index_daily"]
    conn = db.connect(tmp_path / "lagged-batch.db")

    def batch():
        return s.load(conn.execute("SELECT * FROM batches").fetchone())

    try:
        conn.execute("INSERT INTO batches (status,created_at,rule,q20,q5,l5,frozen_at) VALUES (?,?,?,?,?,?,?)",
                     ("frozen", db.now(), json.dumps(s.RULE), json.dumps([{"code": code}]), json.dumps([code]),
                      json.dumps({"codes": [code]}), "2026-10-25T00:19:04+08:00"))
        # 17:41: stocks and price indexes are out, total-return indexes are not. Enter, but no mark yet.
        assert s.tick(conn) is True
        b = batch()
        assert b["status"] == "tracking" and b["error"] is None and b["entry"]["date"] == day
        assert b["entry"]["prices"][code] == {"filled": True, "open": 24.94, "adj": 13.2697}
        entry = b["entry"]
        assert "latest" not in b["marks"] and "checked" not in b["marks"] and s.returns(b) is None
        assert s.tick(conn) is False  # throttled for RETRY_MINUTES

        # 18:12: only one benchmark has published; still no mark.
        market.published = {"H00906.CSI"}
        at("2026-10-26T18:12:00+08:00")
        assert s.tick(conn) is True
        b = batch()
        assert "latest" not in b["marks"] and "checked" not in b["marks"] and b["marks"]["attempted"] == db.now()

        # 19:31: both published. Mark the day, stop checking for it, keep the entry.
        market.published = {"H00906.CSI", "H00300.CSI"}
        at("2026-10-26T19:31:00+08:00")
        assert s.tick(conn) is True
        b = batch()
        assert b["entry"] == entry
        assert b["marks"]["checked"] == day and b["marks"]["attempted"] is None
        result = s.returns(b)
        assert result["date"] == day
        for bench in s.RULE["benchmarks"]:
            assert result["values"][bench] == later[bench][0]["close"] / entry["benchmarks"][bench]["value"] - 1
        assert s.tick(conn) is False  # done for today

        # Next evening the new day's benchmarks lag again: keep the saved mark without revaluing it.
        at("2026-10-27T17:30:00+08:00")
        market.calls.clear()
        assert s.tick(conn) is True
        b2 = batch()
        assert b2["marks"]["latest"] == b["marks"]["latest"] and b2["marks"]["checked"] == day
        assert b2["marks"]["attempted"] == db.now()
        assert not {"daily", "adj_factor"} & set(market.calls)
    finally:
        conn.close()
