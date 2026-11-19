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
