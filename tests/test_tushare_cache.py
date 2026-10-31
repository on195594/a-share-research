"""SQLite cache round trips over captured TuShare financial and market responses."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import db, screening, sources

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
    with closing(sqlite3.connect(db.DATA / "tushare_cache.db")) as conn:
        row = conn.execute("SELECT key, data, created_at FROM tushare_cache").fetchone()
        assert len(row[0]) == 64 and json.loads(row[1])["columns"] == list(original.columns)
        assert datetime.fromisoformat(row[2]).tzinfo is not None


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


def test_disable_does_not_create_database(pro, monkeypatch):
    monkeypatch.setenv("TUSHARE_CACHE", "0")
    screening._query(pro, "income", ts_code="000651.SZ")
    assert not (db.DATA / "tushare_cache.db").exists()


@pytest.mark.parametrize("params,cached", [
    ({"trade_date": MARKET["trade_date"]}, True),
    ({"end_date": MARKET["trade_date"]}, True),
    ({}, False),
    ({"trade_date": "00000060"}, False),
])
def test_only_bounded_historical_market_data_is_cached(pro, params, cached):
    for _ in range(2):
        screening._query(pro, "daily", **params)
    assert len(pro.calls) == (1 if cached else 2)


@pytest.mark.parametrize("days", [0, 1])
def test_current_and_future_market_dates_are_live(pro, days):
    day = (datetime.now(db.SHANGHAI).date() + timedelta(days=days)).strftime("%Y%m%d")
    for _ in range(2):
        screening._query(pro, "daily", trade_date=day)
    assert len(pro.calls) == 2


def test_open_financial_query_key_expires_each_day(pro):
    params = {"ts_code": "000651.SZ"}
    key = sources._tushare_cache_key("income", params)
    today = datetime.now(db.SHANGHAI).date().isoformat()
    import hashlib
    assert key == hashlib.sha256(json.dumps(
        {"api": "income", "params": params, "as_of": today}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert sources._tushare_cache_key("stock_basic", {"end_date": FIN["as_of"]}) is None


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
