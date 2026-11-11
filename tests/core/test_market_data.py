from pathlib import Path

import a_share_research.core
from a_share_research.core.market_data import MarketDataResult


def test_market_data_result_defaults() -> None:
    result = MarketDataResult(1.23, "ok", "test.source", "2026-06-23T10:00:00")

    assert result.value == 1.23
    assert result.status == "ok"
    assert result.adjusted == "none"
    assert result.volume_unit == "unknown"
    assert result.error_code is None


def test_providers_package_exports() -> None:
    from a_share_research.core import providers

    expected_exports = [
        "TushareMarketDataProvider",
        "TushareFundamentalsProvider",
        "TushareValuationProvider",
        "TushareFinancialProvider",
        "TushareDividendProvider",
        "TushareProviderBase",
        "TushareRateLimiter",
        "QuoteObservation",
        "validate_quote_observation",
        "to_tushare_index_code",
        "to_tushare_stock_code",
    ]
    assert sorted(providers.__all__) == sorted(expected_exports)
    for name in expected_exports:
        assert hasattr(providers, name), f"providers package missing export: {name}"


def test_market_data_exports_every_error_code() -> None:
    from typing import get_args

    from a_share_research.core import market_data

    codes = set(get_args(market_data.MarketDataErrorCode))
    assert codes <= set(market_data.__all__)
    assert all(getattr(market_data, code) == code for code in codes)


def test_package_ships_py_typed_marker() -> None:
    assert (Path(a_share_research.core.__file__).parent / "py.typed").is_file()


def test_now_is_timezone_aware_shanghai() -> None:
    from datetime import datetime, timedelta

    from a_share_research.core.market_data import now, shanghai_today

    parsed = datetime.fromisoformat(now())
    assert parsed.utcoffset() == timedelta(hours=8)
    assert shanghai_today() in {parsed.date(), parsed.date() + timedelta(days=1)}
