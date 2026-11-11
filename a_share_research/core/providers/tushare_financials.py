from __future__ import annotations

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

FINA_INDICATOR_SOURCE = "tushare.fina_indicator"
INCOME_SOURCE = "tushare.income"
BALANCE_SHEET_SOURCE = "tushare.balancesheet"
CASHFLOW_SOURCE = "tushare.cashflow"
DIVIDEND_SOURCE = "tushare.dividend"

COMMON_FINANCIAL_COLUMNS = (
    "ts_code",
    "endpoint",
    "ann_date",
    "f_ann_date",
    "end_date",
    "report_type",
    "comp_type",
    "end_type",
    "update_flag",
)
STATEMENT_REQUIRED_COLUMNS = {
    "ts_code",
    "ann_date",
    "f_ann_date",
    "end_date",
    "report_type",
    "comp_type",
    "end_type",
    "update_flag",
}
INDICATOR_REQUIRED_COLUMNS = {"ts_code", "ann_date", "end_date", "update_flag"}
DIVIDEND_REQUIRED_COLUMNS = {"ts_code", "end_date", "ann_date", "div_proc"}
FINANCIAL_DATE_COLUMNS = ("ann_date", "f_ann_date", "end_date")
DIVIDEND_DATE_COLUMNS = (
    "end_date",
    "ann_date",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
    "base_date",
)
DIVIDEND_NUMERIC_COLUMNS = (
    "stk_div",
    "stk_bo_rate",
    "stk_co_rate",
    "cash_div",
    "cash_div_tax",
    "base_share",
)
FINA_INDICATOR_FIELDS = (
    "ts_code,ann_date,end_date,update_flag,roe_waa,netprofit_yoy,"
    "debt_to_assets,grossprofit_margin,bps,or_yoy,dt_netprofit_yoy"
)


class TushareFinancialProvider(TushareProviderBase):
    """TuShare financial statement and indicator provider."""

    def fetch_indicator_history(
        self,
        code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
    ) -> MarketDataResult[pd.DataFrame]:
        return self._fetch_history(
            "fina_indicator",
            FINA_INDICATOR_SOURCE,
            INDICATOR_REQUIRED_COLUMNS,
            code,
            start_date,
            end_date,
            period,
        )

    def fetch_income_history(
        self,
        code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
    ) -> MarketDataResult[pd.DataFrame]:
        return self._fetch_statement(
            "income", INCOME_SOURCE, code, start_date, end_date, period
        )

    def fetch_balance_history(
        self,
        code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
    ) -> MarketDataResult[pd.DataFrame]:
        return self._fetch_statement(
            "balancesheet", BALANCE_SHEET_SOURCE, code, start_date, end_date, period
        )

    def fetch_cashflow_history(
        self,
        code: str,
        start_date: str | None = None,
        end_date: str | None = None,
        period: str | None = None,
    ) -> MarketDataResult[pd.DataFrame]:
        return self._fetch_statement(
            "cashflow", CASHFLOW_SOURCE, code, start_date, end_date, period
        )

    def _fetch_statement(
        self,
        endpoint: str,
        source: str,
        code: str,
        start_date: str | None,
        end_date: str | None,
        period: str | None,
    ) -> MarketDataResult[pd.DataFrame]:
        return self._fetch_history(
            endpoint,
            source,
            STATEMENT_REQUIRED_COLUMNS,
            code,
            start_date,
            end_date,
            period,
        )

    def _fetch_history(
        self,
        endpoint: str,
        source: str,
        required_columns: set[str],
        code: str,
        start_date: str | None,
        end_date: str | None,
        period: str | None,
    ) -> MarketDataResult[pd.DataFrame]:
        try:
            params = _history_params(code, start_date, end_date, period)
            if endpoint == "fina_indicator":
                params["fields"] = FINA_INDICATOR_FIELDS
        except ValueError as exc:
            return invalid_argument_result(source, exc)
        result = self._request_frame(source, endpoint, params, required_columns)
        if result.value is None:
            return result
        return _normalize_financial_result(result, endpoint)


class TushareDividendProvider(TushareProviderBase):
    """TuShare dividend event provider."""

    def fetch_dividend_history(self, code: str) -> MarketDataResult[pd.DataFrame]:
        try:
            params = {"ts_code": to_tushare_stock_code(code)}
        except ValueError as exc:
            return invalid_argument_result(DIVIDEND_SOURCE, exc)
        result = self._request_frame(
            DIVIDEND_SOURCE,
            "dividend",
            params,
            DIVIDEND_REQUIRED_COLUMNS,
            allow_empty=True,
        )
        if result.value is None:
            return result
        return _normalize_dividend_result(result)


def _history_params(
    code: str,
    start_date: str | None,
    end_date: str | None,
    period: str | None,
) -> dict[str, str]:
    params = {"ts_code": to_tushare_stock_code(code)}
    if start_date:
        params["start_date"] = compact_date(start_date)
    if end_date:
        params["end_date"] = compact_date(end_date)
    if params.get("start_date", "") > params.get("end_date", "99999999"):
        raise ValueError("start_date must not be after end_date")
    if period:
        params["period"] = compact_date(period)
    return params


def _normalize_financial_result(
    result: MarketDataResult[pd.DataFrame],
    endpoint: str,
) -> MarketDataResult[pd.DataFrame]:
    assert result.value is not None
    try:
        normalized = result.value.copy()
        normalized["endpoint"] = endpoint
        for column in COMMON_FINANCIAL_COLUMNS:
            if column not in normalized.columns:
                normalized[column] = pd.NA
        normalized = normalize_date_columns(normalized, FINANCIAL_DATE_COLUMNS)
        normalized = normalized.sort_values(
            ["end_date", "ann_date"], na_position="last"
        ).reset_index(drop=True)
    except Exception as exc:
        return schema_failure_result(result, exc)
    source_as_of = _financial_source_as_of(normalized, endpoint)
    return replace_frame_result(result, normalized, source_as_of)


def _normalize_dividend_result(
    result: MarketDataResult[pd.DataFrame],
) -> MarketDataResult[pd.DataFrame]:
    assert result.value is not None
    try:
        normalized = normalize_date_columns(result.value, DIVIDEND_DATE_COLUMNS)
        normalized = normalize_numeric_columns(normalized, DIVIDEND_NUMERIC_COLUMNS)
        normalized = normalized.sort_values(
            ["end_date", "ann_date"], na_position="last"
        ).reset_index(drop=True)
    except Exception as exc:
        return schema_failure_result(result, exc)
    source_as_of = _coalesced_maximum_date(normalized, "imp_ann_date", "ann_date")
    return replace_frame_result(result, normalized, source_as_of)


def _financial_source_as_of(frame: pd.DataFrame, endpoint: str) -> str | None:
    if endpoint == "fina_indicator":
        return _maximum_date(frame, "ann_date")
    return _coalesced_maximum_date(frame, "f_ann_date", "ann_date")


def _coalesced_maximum_date(
    frame: pd.DataFrame,
    primary: str,
    fallback: str,
) -> str | None:
    if frame.empty or fallback not in frame.columns:
        return None
    values = (
        frame[primary].combine_first(frame[fallback])
        if primary in frame.columns
        else frame[fallback]
    )
    values = values.dropna()
    return str(values.max()) if not values.empty else None


def _maximum_date(frame: pd.DataFrame, column: str) -> str | None:
    if frame.empty or column not in frame.columns:
        return None
    values = frame[column].dropna()
    return str(values.max()) if not values.empty else None
