from __future__ import annotations

from typing import Any

import pandas as pd

from a_share_research.core.market_data import MarketDataResult
from a_share_research.core.providers.tushare_common import (
    TushareProviderBase,
    compact_date,
    invalid_argument_result,
    normalize_date_columns,
    normalize_numeric_columns,
    replace_frame_result,
    schema_failure_result,
)
from a_share_research.core.providers.tushare_quotes import to_tushare_stock_code

DAILY_BASIC_SOURCE = "tushare.daily_basic"
VALUATION_FIELDS = (
    "ts_code,trade_date,close,pe,pe_ttm,pb,ps,ps_ttm,"
    "dv_ratio,dv_ttm,total_mv,circ_mv"
)
VALUATION_COLUMNS = tuple(VALUATION_FIELDS.split(","))
VALUATION_NUMERIC_COLUMNS = tuple(column for column in VALUATION_COLUMNS if column not in {"ts_code", "trade_date"})


class TushareValuationProvider(TushareProviderBase):
    """TuShare daily_basic provider for per-stock historical valuations."""

    def fetch_valuation_history(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> MarketDataResult[pd.DataFrame]:
        try:
            start = compact_date(start_date)
            end = compact_date(end_date)
            if start > end:
                raise ValueError("start_date must not be after end_date")
            params = {
                "ts_code": to_tushare_stock_code(code),
                "start_date": start,
                "end_date": end,
                "fields": VALUATION_FIELDS,
            }
        except ValueError as exc:
            return invalid_argument_result(DAILY_BASIC_SOURCE, exc)
        return self._fetch(params)

    def _fetch(self, params: dict[str, Any]) -> MarketDataResult[pd.DataFrame]:
        result = self._request_frame(
            DAILY_BASIC_SOURCE,
            "daily_basic",
            params,
            set(VALUATION_COLUMNS),
        )
        if result.value is None:
            return result
        try:
            normalized = normalize_date_columns(result.value, ("trade_date",))
            normalized = normalize_numeric_columns(normalized, VALUATION_NUMERIC_COLUMNS)
            normalized = normalized.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        except Exception as exc:
            return schema_failure_result(result, exc)
        source_as_of = str(normalized["trade_date"].max())
        return replace_frame_result(result, normalized, source_as_of)
