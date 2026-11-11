"""Consumer-facing price contracts, using injected clients only."""
from __future__ import annotations

import pandas as pd
import pytest

from a_share_research.core.market_data import (
    EMPTY_RESPONSE, INVALID_ARGUMENT, MISSING_COLUMNS, SCHEMA_CHANGED, SOURCE_STALE,
)
from a_share_research.core.providers.tushare_quotes import TushareMarketDataProvider


class Client:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def index_daily(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame

    daily = index_daily
    trade_cal = index_daily


def bars(days=("20260623", "20260622")):
    return pd.DataFrame({"trade_date": days, "open": 10, "high": 11,
                         "low": 9, "close": 10.5, "vol": 100})


@pytest.mark.parametrize("symbol", ["H00300.CSI", "H00922.CSI", "000300.SH", "399001.SZ"])
def test_index_close_only_contract(symbol):
    client = Client(pd.DataFrame({"trade_date": ["20260623", "20260622"],
                                  "close": [120.0, 110.0], "vol": [None, None]}))
    result = TushareMarketDataProvider(token="fake-token", client=client).fetch_index_bars(
        symbol, "2026-06-01", "2026-06-24"
    )
    assert result.status == "ok"
    assert list(result.value) == ["date", "close"]
    assert result.value["date"].tolist() == ["2026-06-22", "2026-06-23"]
    assert result.source == "tushare.index_daily"
    assert result.source_as_of == "2026-06-23"
    assert result.freshness_days == 1
    assert result.volume_unit == "unknown"
    assert result.row_count == 2 and result.request_fingerprint and result.fetched_at
    assert client.calls == [{"ts_code": symbol, "start_date": "20260601",
                             "end_date": "20260624", "fields": "trade_date,close"}]


@pytest.mark.parametrize(("symbol", "start", "end"), [
    ("bad.CSI", "2026-06-01", "2026-06-24"),
    ("H00300.SH", "2026-06-01", "2026-06-24"),
    ("H00300.CSI", "bad", "2026-06-24"),
    ("H00300.CSI", "2026-06-25", "2026-06-24"),
])
def test_index_invalid_request_never_calls_client(symbol, start, end):
    client = Client(None)
    result = TushareMarketDataProvider(token="fake-token", client=client).fetch_index_bars(symbol, start, end)
    assert result.error_code == INVALID_ARGUMENT
    assert not client.calls


@pytest.mark.parametrize(("frame", "error"), [
    (pd.DataFrame(columns=["trade_date", "close"]), EMPTY_RESPONSE),
    (pd.DataFrame({"trade_date": ["20260623"]}), MISSING_COLUMNS),
    (pd.DataFrame({"trade_date": ["bad"], "close": [100]}), SCHEMA_CHANGED),
    (pd.DataFrame({"trade_date": ["20260623"] * 2, "close": [100, 101]}), SCHEMA_CHANGED),
    (pd.DataFrame({"trade_date": ["20260625"], "close": [100]}), SOURCE_STALE),
    (pd.DataFrame({"trade_date": ["20260531"], "close": [100]}), SOURCE_STALE),
    *[(pd.DataFrame({"trade_date": ["20260623"], "close": [v]}), SCHEMA_CHANGED)
      for v in (None, float("inf"), 0, -1)],
])
def test_index_fails_closed(frame, error):
    result = TushareMarketDataProvider(token="fake-token", client=Client(frame)).fetch_index_bars(
        "H00300.CSI", "2026-06-01", "2026-06-24"
    )
    assert result.status == "failed" and result.value is None
    assert result.error_code == error
    assert result.request_fingerprint


@pytest.mark.parametrize(("target", "status", "age"), [
    ("2026-06-23", "ok", 0), ("2026-06-27", "degraded", 4),
])
def test_nearest_close_retains_observation_date(target, status, age):
    client = Client(bars())
    result = TushareMarketDataProvider(token="fake-token", client=client).fetch_close_price("600036", target)
    assert result.status == status and result.value == 10.5
    assert result.source_as_of == "2026-06-23"
    assert result.freshness_days == age
    assert result.fallback_reason == ("NEAREST_AVAILABLE_PRICE" if age else None)
    assert result.row_count == 2 and result.request_fingerprint
    assert client.calls[0]["end_date"] == target.replace("-", "")


@pytest.mark.parametrize(("frame", "error"), [
    (bars().iloc[:0], EMPTY_RESPONSE),
    (bars(("20260610",)), SOURCE_STALE),
    (bars(("20260628",)), SOURCE_STALE),
])
def test_nearest_close_never_uses_unbounded_or_future_price(frame, error):
    result = TushareMarketDataProvider(token="fake-token", client=Client(frame)).fetch_close_price("600036", "2026-06-27")
    assert result.status == "failed" and result.value is None
    assert result.error_code == error


def test_calendar_success_contract():
    client = Client(pd.DataFrame({"cal_date": ["20260623", "20260622"]}))
    result = TushareMarketDataProvider(token="fake-token", client=client).fetch_trade_calendar("2026-06-01", "2026-06-24")
    assert result.status == "ok"
    assert result.value["date"].tolist() == ["2026-06-22", "2026-06-23"]
    assert result.source == "tushare.trade_cal"
    # Calendar describes scheduled sessions, not a price observation.
    assert result.source_as_of is None
    assert result.row_count == 2 and result.request_fingerprint and result.fetched_at
    assert client.calls == [{"exchange": "SSE", "is_open": "1", "start_date": "20260601",
                             "end_date": "20260624", "fields": "cal_date"}]
