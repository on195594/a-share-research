from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from a_share_research.core.market_data import (
    INVALID_ARGUMENT,
    MISSING_COLUMNS,
    SCHEMA_CHANGED,
)
from a_share_research.core.providers.tushare_quotes import (
    DAILY_SOURCE,
    TRADE_CAL_SOURCE,
    TushareMarketDataProvider,
    _normalize_tushare_bars,
    to_tushare_stock_code,
)


class _DummyClient:
    def __init__(self, value: Any) -> None:
        self._value = value

    def daily(self, **kwargs: Any) -> Any:
        return self._value

    def trade_cal(self, **kwargs: Any) -> Any:
        return self._value


def test_tushare_stock_code_supports_beijing_exchange() -> None:
    assert to_tushare_stock_code("920000") == "920000.BJ"
    assert to_tushare_stock_code("430047.BJ") == "430047.BJ"


def test_tushare_normalizer_requires_dataframe():
    result = _normalize_tushare_bars(
        [{"trade_date": "20260623", "close": "10.5"}],
        DAILY_SOURCE,
        "close_price",
    )

    assert result.status == "failed"
    assert result.error_code == SCHEMA_CHANGED


def test_tushare_normalizer_requires_ohlc_for_close_price():
    result = _normalize_tushare_bars(
        pd.DataFrame({"trade_date": ["20260623"], "close": [10.5]}),
        DAILY_SOURCE,
        "close_price",
    )

    assert result.status == "failed"
    assert result.error_code == MISSING_COLUMNS


@pytest.mark.parametrize("bad_close", [float("nan"), float("inf"), 0.0, -1.0])
def test_tushare_normalizer_rejects_invalid_prices(bad_close: float):
    result = _normalize_tushare_bars(
        pd.DataFrame(
            {
                "trade_date": ["20260623"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [bad_close],
            }
        ),
        DAILY_SOURCE,
        "close_price",
    )

    assert result.status == "failed"
    assert result.error_code == SCHEMA_CHANGED


def test_tushare_normalizer_rejects_inconsistent_ohlc():
    result = _normalize_tushare_bars(
        pd.DataFrame(
            {
                "trade_date": ["20260623"],
                "open": [10.0],
                "high": [9.0],
                "low": [8.0],
                "close": [10.5],
            }
        ),
        DAILY_SOURCE,
        "close_price",
    )

    assert result.status == "failed"
    assert result.error_code == SCHEMA_CHANGED


def test_fetch_close_price_rejects_malformed_source_date():
    provider = TushareMarketDataProvider(
        token="fake-token",
        client=_DummyClient(
            pd.DataFrame(
                {
                    "trade_date": ["2026-06-1a"],
                    "open": [10.0],
                    "high": [11.0],
                    "low": [9.0],
                    "close": [10.5],
                    "vol": [100.0],
                }
            )
        ),
    )

    result = provider.fetch_close_price("600036", "2026-06-23")

    assert result.status == "failed"
    assert result.error_code == SCHEMA_CHANGED


def test_tushare_trade_calendar_non_dataframe_fails_schema_changed():
    provider = TushareMarketDataProvider(token="fake-token", client=_DummyClient({"cal_date": []}))

    result = provider.fetch_trade_calendar("2026-06-01", "2026-06-23")

    assert result.status == "failed"
    assert result.source == TRADE_CAL_SOURCE
    assert result.error_code == SCHEMA_CHANGED


def test_scalar_price_rejects_malformed_date():
    provider = TushareMarketDataProvider(token="fake-token")
    result = provider.fetch_close_price("600036", "not-a-date")
    assert result.status == "failed"
    assert result.error_code == INVALID_ARGUMENT
    assert result.value is None
    assert result.error_message is not None
