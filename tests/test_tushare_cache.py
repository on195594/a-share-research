"""SQLite cache round trips over captured TuShare financial and market responses."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import market, db, screening

DATA = Path(__file__).parent / "data"
FIN = json.loads((DATA / "screen-financials.json").read_text())
MARKET = json.loads((DATA / "screen-entry-market.json").read_text())
AUDIT = json.loads((DATA / "tushare-audit.json").read_text())


class CapturedQueries:
    """Offline replay of captured frames; keep the HTTP call boundary observable."""

    def __init__(self):
        self.calls = []

    def query(self, api, **params):
        self.calls.append((api, params))
        if api == "fina_audit":
            rows = AUDIT["companies"][params["ts_code"]]
        elif api in ("fina_indicator", "income", "cashflow"):
            rows = FIN["companies"][params["ts_code"]][{"fina_indicator": "indicator"}.get(api, api)]
        else:
            rows = MARKET[api]
        frame = pd.DataFrame(rows)
        if params.get("fields"):
            frame = frame[params["fields"].split(",")]
        return frame.iloc[params.get("offset", 0):]


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.delenv("TUSHARE_CACHE")
    return CapturedQueries()


@pytest.mark.parametrize("api", ["fina_indicator", "income", "cashflow", "fina_audit"])
def test_write_and_reuse_exact_financial_frame(pro, api):
    params = {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}
    original = screening._query(pro, api, **params)
    # A new client and connection reuse the persisted response, independent of kwargs order.
    next_client = CapturedQueries()
    cached = screening._query(next_client, api, **dict(reversed(list(params.items()))))
    pd.testing.assert_frame_equal(original, cached, check_exact=True)
    assert len(pro.calls) == 1 and next_client.calls == []


def test_cache_key_separates_api_company_dates_and_fields(pro):
    variants = [
        ("income", {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}),
        ("cashflow", {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}),
        ("income", {"ts_code": "002648.SZ", "end_date": FIN["as_of"]}),
        ("income", {"ts_code": "000651.SZ", "end_date": "20261022"}),
        ("income", {"ts_code": "000651.SZ", "end_date": FIN["as_of"], "fields": "ts_code,end_date"}),
    ]
    for api, params in variants * 2:
        screening._query(pro, api, **params)
    assert len(pro.calls) == len(variants)


def test_nondefault_index_and_string_flags_survive_round_trip(pro):
    params = {"ts_code": "000651.SZ", "end_date": FIN["as_of"], "offset": 1}
    original = screening._query(pro, "fina_indicator", **params)
    restored = screening._query(pro, "fina_indicator", **params)
    pd.testing.assert_frame_equal(restored, original, check_exact=True)
    assert restored.index[0] == 1
    assert isinstance(restored.iloc[0]["update_flag"], str)


def test_disable_bypasses_existing_cache_without_writes(pro, monkeypatch):
    params = {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}
    original = screening._query(pro, "income", **params)
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        before = conn.execute("SELECT * FROM tushare_cache").fetchall()
        monkeypatch.setenv("TUSHARE_CACHE", "0")
        for _ in range(2):
            pd.testing.assert_frame_equal(screening._query(pro, "income", **params), original)
        assert conn.execute("SELECT * FROM tushare_cache").fetchall() == before
    assert len(pro.calls) == 3


@pytest.mark.parametrize("params,cached", [
    ({"trade_date": MARKET["trade_date"]}, True),
    ({"end_date": MARKET["trade_date"]}, True),
    ({}, False),
    ({"end_date": "00000060"}, False),
])
def test_only_bounded_historical_data_is_cached(pro, params, cached):
    api = "daily"
    for _ in range(2):
        screening._query(pro, api, ts_code="000651.SZ", **params)
    assert len(pro.calls) == (1 if cached else 2)
    assert (db.DATA / "tushare_cache.db").exists() == cached


@pytest.mark.parametrize(("days", "api", "bound"), [(0, "daily", "trade_date"), (1, "income", "end_date")])
def test_current_and_future_dates_are_live(pro, days, api, bound):
    day = (datetime.now(db.SHANGHAI).date() + timedelta(days=days)).strftime("%Y%m%d")
    for _ in range(2):
        screening._query(pro, api, ts_code="000651.SZ", **{bound: day})
    assert len(pro.calls) == 2
    assert not (db.DATA / "tushare_cache.db").exists()


def test_open_financial_and_unsupported_queries_have_no_cache_key():
    for api in market.FINANCIAL_APIS:
        assert market._tushare_cache_key(api, {"ts_code": "000651.SZ"}) is None
    assert market._tushare_cache_key("stock_basic", {"end_date": FIN["as_of"]}) is None


def test_cache_write_failure_returns_upstream_data(pro, caplog):
    params = {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}
    expected = screening._query(pro, "income", **params)
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        conn.execute("DELETE FROM tushare_cache")
        conn.execute("CREATE TRIGGER reject_cache BEFORE INSERT ON tushare_cache "
                     "BEGIN SELECT RAISE(ABORT, 'cache write rejected'); END")
        conn.commit()
    pd.testing.assert_frame_equal(screening._query(pro, "income", **params), expected)
    assert len(pro.calls) == 2
    assert "cache write failed" in caplog.text


def test_empty_response_is_not_cached(pro):
    for _ in range(2):
        assert screening._query(pro, "suspend_d", trade_date=MARKET["trade_date"]).empty
    assert len(pro.calls) == 2


@pytest.mark.parametrize("payload", ["{", "{}"])
def test_damaged_cache_refetches_and_repairs(pro, payload):
    params = {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}
    expected = screening._query(pro, "income", **params)
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        conn.execute("UPDATE tushare_cache SET data=?", (payload,))
        conn.commit()
    pd.testing.assert_frame_equal(screening._query(pro, "income", **params), expected)
    pd.testing.assert_frame_equal(screening._query(pro, "income", **params), expected)
    assert len(pro.calls) == 2


def test_concurrent_writes_use_separate_sqlite_connections(pro):
    def query(code):
        return screening._query(pro, "income", ts_code=code, end_date=FIN["as_of"])
    codes = ["000651.SZ", "002648.SZ"] * 4
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(query, codes))
    assert all(not frame.empty for frame in results)
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM tushare_cache").fetchone()[0] == 2


@pytest.mark.parametrize("failures", [0, 2, 3])
def test_gateway_calendar_preserves_retries_and_screen_error(pro, monkeypatch, failures):
    class InterruptedCalendar(CapturedQueries):
        attempts = 0

        def query(self, api, **params):
            self.attempts += 1
            if self.attempts <= failures:
                raise RuntimeError("calendar transport interrupted")
            return super().query(api, **params)

    replay = InterruptedCalendar()
    waits = []
    monkeypatch.setattr(market.time, "sleep", waits.append)
    if failures == 3:
        with pytest.raises(screening.ScreenError, match="^trade_cal : calendar transport interrupted$") as error:
            market.trade_days(replay, "20260101", "20261023")
        assert isinstance(error.value.__cause__, RuntimeError)
    else:
        assert market.trade_days(replay, "20260101", "20261023") == sorted(
            row["cal_date"] for row in MARKET["trade_cal"])
    assert replay.attempts == min(failures + 1, 3)
    assert waits == [1, 3][:failures]


def test_cache_probe_oserror_falls_back_to_upstream(pro, monkeypatch):
    params = {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}
    expected = screening._query(pro, "income", **params)
    orig_exists = Path.exists

    def fail_exists(self):
        if self.name == "tushare_cache.db":
            raise OSError("EIO")
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", fail_exists)
    actual = screening._query(pro, "income", **params)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    assert len(pro.calls) == 2


def test_readonly_legacy_cache_without_budget_table_serves_hit(pro, monkeypatch):
    params = {"ts_code": "000651.SZ", "end_date": FIN["as_of"]}
    expected = screening._query(pro, "income", **params)
    cache_path = db.DATA / "tushare_cache.db"
    # A fresh process meets the legacy file: no handle is held yet.
    market._cache_connection().close()
    monkeypatch.setattr(market, "_connections", threading.local())
    with closing(sqlite3.connect(cache_path)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("DROP TABLE IF EXISTS report_rc_budget")
        conn.commit()
    cache_path.chmod(0o444)
    try:
        fresh = CapturedQueries()
        cached = screening._query(fresh, "income", **params)
        pd.testing.assert_frame_equal(cached, expected, check_exact=True)
        assert fresh.calls == []
    finally:
        cache_path.chmod(0o644)
