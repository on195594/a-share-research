from __future__ import annotations

import pandas as pd
import pytest

from a_share_research.core.market_data import EMPTY_RESPONSE, INVALID_ARGUMENT, MISSING_COLUMNS, SCHEMA_CHANGED
from a_share_research.core.providers.tushare_valuation import (
    DAILY_BASIC_SOURCE,
    VALUATION_COLUMNS,
    TushareValuationProvider,
)


class Client:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def daily_basic(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame


def frame():
    rows = [{column: "2.4" for column in VALUATION_COLUMNS} for _ in range(2)]
    rows[0].update(ts_code="600036.SH", trade_date="20260717", pe_ttm=None)
    rows[1].update(ts_code="600036.SH", trade_date="20260716", close="44.02")
    return pd.DataFrame(rows)


def test_history_normalizes_dates_values_and_preserves_missing_pe():
    client = Client(frame())
    result = TushareValuationProvider(token="fake-token", client=client).fetch_valuation_history(
        "600036", "2016-07-18", "2026-07-17"
    )
    assert result.status == "ok"
    assert result.source == DAILY_BASIC_SOURCE
    assert result.source_as_of == "2026-07-17"
    assert result.row_count == 2
    assert result.request_fingerprint and result.fetched_at
    assert result.value["trade_date"].tolist() == ["2026-07-16", "2026-07-17"]
    assert result.value.iloc[0]["close"] == pytest.approx(44.02)
    assert pd.isna(result.value.iloc[1]["pe_ttm"])
    assert client.calls == [{
        "ts_code": "600036.SH", "start_date": "20160718", "end_date": "20260717",
        "fields": ",".join(VALUATION_COLUMNS),
    }]


@pytest.mark.parametrize(("code", "start", "end"), [
    ("bad", "2026-07-16", "2026-07-17"),
    ("600036", "bad", "2026-07-17"),
    ("600036", "2026-07-18", "2026-07-17"),
])
def test_invalid_request_never_calls_client(code, start, end):
    client = Client(frame())
    result = TushareValuationProvider(token="fake-token", client=client).fetch_valuation_history(code, start, end)
    assert result.error_code == INVALID_ARGUMENT
    assert client.calls == []


@pytest.mark.parametrize(("value", "error"), [
    (frame().drop(columns="pb"), MISSING_COLUMNS),
    (frame().assign(pb="bad"), SCHEMA_CHANGED),
    (frame().assign(trade_date="bad"), SCHEMA_CHANGED),
    (frame().iloc[:0], EMPTY_RESPONSE),
])
def test_history_failure_is_structured(value, error):
    result = TushareValuationProvider(token="fake-token", client=Client(value)).fetch_valuation_history(
        "600036", "2016-07-18", "2026-07-17"
    )
    assert result.status == "failed"
    assert result.value is None
    assert result.error_code == error
    assert result.source == DAILY_BASIC_SOURCE
    assert result.request_fingerprint
