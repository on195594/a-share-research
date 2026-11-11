from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pandas as pd

from a_share_research.core.market_data import (
    EMPTY_RESPONSE,
    MISSING_COLUMNS,
    SCHEMA_CHANGED,
    SOURCE_STALE,
    MarketDataResult,
    now,
)
from a_share_research.core.providers.tushare_common import (
    TushareProviderBase,
    compact_date,
    invalid_argument_result,
    parse_date,
    replace_frame_result,
    schema_failure_result,
)

DAILY_SOURCE = "tushare.daily"
INDEX_DAILY_SOURCE = "tushare.index_daily"
TRADE_CAL_SOURCE = "tushare.trade_cal"
TUSHARE_VOLUME_UNIT = "hand"


class TushareMarketDataProvider(TushareProviderBase):
    """Tushare Pro implementation of the market-data provider boundary."""

    def fetch_close_price(self, code: str, target_date: str) -> MarketDataResult[float]:
        """Close on or before target_date, within the preceding 10 calendar days."""
        try:
            target = parse_date(target_date)
            start = target - timedelta(days=10)
        except (ValueError, OverflowError) as exc:
            return invalid_argument_result(DAILY_SOURCE, exc)
        result = self._fetch_daily(code, compact_date(start), compact_date(target), "close_price")
        if result.value is None or result.value.empty:
            return _scalar_failure(result, DAILY_SOURCE)
        return _scalar_price_result(result, target)

    def fetch_index_bars(self, symbol: str, start_date: str, end_date: str) -> MarketDataResult[pd.DataFrame]:
        """Index closing points only; total-return indices need not have OHLC/volume."""
        try:
            ts_code = to_tushare_index_code(symbol)
            start = parse_date(start_date)
            end = parse_date(end_date)
            if start > end:
                raise ValueError("start_date must not be after end_date")
        except ValueError as exc:
            return invalid_argument_result(INDEX_DAILY_SOURCE, exc)
        result = self._request_frame(
            INDEX_DAILY_SOURCE,
            "index_daily",
            {"ts_code": ts_code, "start_date": compact_date(start), "end_date": compact_date(end),
             "fields": "trade_date,close"},
            {"trade_date", "close"},
        )
        normalized = _normalize_requested_bars(result, "index_bars", end)
        return _validate_range(normalized, start, end)

    def fetch_trade_calendar(self, start_date: str, end_date: str) -> MarketDataResult[pd.DataFrame]:
        try:
            start = parse_date(start_date)
            end = parse_date(end_date)
            if start > end:
                raise ValueError("start_date must not be after end_date")
        except ValueError as exc:
            return invalid_argument_result(TRADE_CAL_SOURCE, exc)
        result = self._request_frame(
            TRADE_CAL_SOURCE,
            "trade_cal",
            {
                "exchange": "SSE",
                "is_open": "1",
                "start_date": compact_date(start),
                "end_date": compact_date(end),
                "fields": "cal_date",
            },
            {"cal_date"},
        )
        if result.value is None:
            return result
        try:
            normalized = result.value[["cal_date"]].rename(columns={"cal_date": "date"}).copy()
            normalized["date"] = normalized["date"].map(_iso_date)
            normalized = normalized.sort_values("date").reset_index(drop=True)
        except Exception as exc:
            return schema_failure_result(result, exc)
        return replace_frame_result(result, normalized, None)

    def _fetch_daily(self, code: str, start_date: str, end_date: str, purpose: str) -> MarketDataResult[pd.DataFrame]:
        try:
            ts_code = to_tushare_stock_code(code)
        except ValueError as exc:
            return invalid_argument_result(DAILY_SOURCE, exc)
        raw = self._request_frame(
            DAILY_SOURCE,
            "daily",
            {
                "ts_code": ts_code,
                "start_date": start_date,
                "end_date": end_date,
                "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
            },
            {"trade_date", "open", "high", "low", "close"},
        )
        result = _normalize_requested_bars(raw, purpose, parse_date(end_date))
        return _validate_range(result, parse_date(start_date), parse_date(end_date))


def to_tushare_stock_code(code: str) -> str:
    normalized = code.strip().upper()
    if normalized.endswith((".SH", ".SZ", ".BJ")):
        return normalized
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"unsupported stock code: {code}")
    if normalized.startswith("6"):
        return f"{normalized}.SH"
    if normalized.startswith(("0", "3")):
        return f"{normalized}.SZ"
    if normalized.startswith(("4", "8", "92")):
        return f"{normalized}.BJ"
    raise ValueError(f"unsupported stock code: {code}")


def to_tushare_index_code(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        code, exchange = normalized.rsplit(".", 1)
        if exchange in {"SH", "SZ", "CSI"} and len(code) == 6 and (
            code.isdigit() or (exchange == "CSI" and code[0] == "H" and code[1:].isdigit())
        ):
            return normalized
        raise ValueError(f"unsupported index symbol: {symbol}")
    if normalized in {"000300", "SH000300"}:
        return "000300.SH"
    if len(normalized) == 6 and normalized.isdigit():
        suffix = "SH" if normalized.startswith("0") else "SZ"
        return f"{normalized}.{suffix}"
    raise ValueError(f"unsupported index symbol: {symbol}")


def _normalize_tushare_bars(df: Any, source: str, purpose: str) -> MarketDataResult[pd.DataFrame]:
    fetched_at = now()
    if df is None:
        return MarketDataResult(None, "failed", source, fetched_at, error_code=EMPTY_RESPONSE)
    if not isinstance(df, pd.DataFrame):
        return MarketDataResult(
            None,
            "failed",
            source,
            fetched_at,
            error_code=SCHEMA_CHANGED,
            error_message=f"expected pandas.DataFrame, got {type(df).__name__}",
        )
    if df.empty:
        return MarketDataResult(None, "failed", source, fetched_at, error_code=EMPTY_RESPONSE)
    normalized = df.rename(columns={"trade_date": "date", "vol": "volume"}).copy()
    if purpose == "index_bars":
        # Discard non-requested fields rather than inventing tradable OHLC/volume.
        normalized = normalized[[col for col in ("date", "close") if col in normalized]]
    required = {"date", "close"} if purpose == "index_bars" else {"date", "open", "high", "low", "close"}
    missing = required - set(normalized.columns)
    if missing:
        return MarketDataResult(
            None,
            "failed",
            source,
            fetched_at,
            error_code=MISSING_COLUMNS,
            error_message=f"missing columns: {sorted(missing)}",
        )
    keep = [col for col in ["date", "open", "high", "low", "close", "volume"] if col in normalized.columns]
    try:
        normalized["date"] = normalized["date"].map(_iso_date)
        for col in [c for c in ["open", "high", "low", "close", "volume"] if c in normalized.columns]:
            normalized[col] = pd.to_numeric(normalized[col], errors="raise")
            invalid = normalized[col].map(
                lambda value: not math.isfinite(float(value)) or value < 0 or (col != "volume" and value == 0)
            )
            if invalid.any():
                raise ValueError(f"invalid {col} value")
        if normalized["date"].duplicated().any():
            raise ValueError("duplicate observation date")
        if purpose != "index_bars":
            inconsistent = (
                (normalized["low"] > normalized["high"])
                | ~normalized["open"].between(normalized["low"], normalized["high"])
                | ~normalized["close"].between(normalized["low"], normalized["high"])
            )
            if inconsistent.any():
                raise ValueError("inconsistent OHLC values")
    except Exception as exc:
        return MarketDataResult(None, "failed", source, fetched_at, error_code=SCHEMA_CHANGED, error_message=str(exc))
    return MarketDataResult(
        normalized[keep].sort_values("date").reset_index(drop=True),
        "ok",
        source,
        fetched_at,
        adjusted="none",
        volume_unit="unknown" if purpose == "index_bars" else TUSHARE_VOLUME_UNIT,
    )


def _normalize_requested_bars(
    raw: MarketDataResult[pd.DataFrame],
    purpose: str,
    reference_date: date,
) -> MarketDataResult[pd.DataFrame]:
    if raw.value is None:
        return raw
    normalized = _normalize_tushare_bars(raw.value, raw.source, purpose)
    if normalized.value is None:
        return MarketDataResult(
            None,
            "failed",
            raw.source,
            raw.fetched_at,
            error_code=normalized.error_code,
            error_message=normalized.error_message,
            request_fingerprint=raw.request_fingerprint,
            row_count=raw.row_count,
        )
    source_as_of = str(normalized.value["date"].max())
    return MarketDataResult(
        normalized.value,
        raw.status,
        raw.source,
        raw.fetched_at,
        adjusted=normalized.adjusted,
        volume_unit=normalized.volume_unit,
        freshness_days=max(0, (reference_date - date.fromisoformat(source_as_of)).days),
        source_as_of=source_as_of,
        request_fingerprint=raw.request_fingerprint,
        row_count=len(normalized.value),
    )


def _validate_range(
    result: MarketDataResult[pd.DataFrame], start: date, end: date
) -> MarketDataResult[pd.DataFrame]:
    if result.value is not None and (
        result.value["date"].min() < start.isoformat()
        or result.value["date"].max() > end.isoformat()
    ):
        return replace(result, value=None, status="failed", error_code=SOURCE_STALE,
                       error_message="source observation is outside requested date range")
    return result



def _scalar_failure(result: MarketDataResult[pd.DataFrame], source: str) -> MarketDataResult[float]:
    return MarketDataResult(
        None,
        "failed",
        result.source or source,
        result.fetched_at,
        error_code=result.error_code,
        error_message=result.error_message,
        adjusted=result.adjusted,
        volume_unit=result.volume_unit,
        source_as_of=result.source_as_of,
        request_fingerprint=result.request_fingerprint,
        row_count=result.row_count,
    )



def _scalar_price_result(
    result: MarketDataResult[pd.DataFrame], requested_date: date
) -> MarketDataResult[float]:
    assert result.value is not None and not result.value.empty
    row = result.value.iloc[-1]
    freshness = (requested_date - parse_date(str(row["date"]))).days
    if freshness < 0:
        return MarketDataResult(
            None,
            "failed",
            result.source,
            result.fetched_at,
            error_code=SOURCE_STALE,
            error_message="source observation is after requested date",
            adjusted=result.adjusted,
            volume_unit=result.volume_unit,
            source_as_of=result.source_as_of,
            request_fingerprint=result.request_fingerprint,
            row_count=result.row_count,
        )
    return MarketDataResult(
        float(row["close"]),
        "ok" if freshness == 0 else "degraded",
        result.source,
        result.fetched_at,
        fallback_reason=None if freshness == 0 else "NEAREST_AVAILABLE_PRICE",
        freshness_days=freshness,
        adjusted=result.adjusted,
        volume_unit=result.volume_unit,
        source_as_of=result.source_as_of,
        request_fingerprint=result.request_fingerprint,
        row_count=result.row_count,
    )


def _iso_date(value: Any) -> str:
    return parse_date(str(value)).isoformat()
