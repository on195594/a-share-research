from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import pandas as pd

from a_share_research.core.market_data import (
    AUTH_MISSING,
    EMPTY_RESPONSE,
    INVALID_ARGUMENT,
    MISSING_COLUMNS,
    PERMISSION_DENIED,
    RATE_LIMITED,
    REMOTE_DISCONNECTED,
    SCHEMA_CHANGED,
    TIMEOUT,
    UNKNOWN_ERROR,
    MarketDataErrorCode,
    MarketDataResult,
    now,
)
T = TypeVar("T")
Clock = Callable[[], float]
Sleeper = Callable[[float], None]
ClientFactory = Callable[[str], Any]


def read_tushare_token(env_path: Path) -> str | None:
    """Read TUSHARE_TOKEN from an explicit dotenv-style file."""
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == "TUSHARE_TOKEN":
            return value.strip().strip('"').strip("'")
    return None


class TushareRateLimiter:
    """Serialized minimum-interval limiter for TuShare API calls."""

    def __init__(
        self,
        calls_per_minute: int = 180,
        clock: Clock = time.monotonic,
        sleep: Sleeper = time.sleep,
    ) -> None:
        if calls_per_minute <= 0:
            raise ValueError("calls_per_minute must be positive")
        self._minimum_interval = 60.0 / calls_per_minute
        self._clock = clock
        self._sleep = sleep
        self._last_call_at: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            current = self._clock()
            if self._last_call_at is not None:
                remaining = self._minimum_interval - (current - self._last_call_at)
                if remaining > 0:
                    self._sleep(remaining)
                    current = self._clock()
            self._last_call_at = current


_GLOBAL_RATE_LIMITER = TushareRateLimiter()



def _exception_class_names(exc: Exception) -> set[str]:
    return {cls.__name__ for cls in type(exc).mro()}


def is_transient_network_error(exc: Exception) -> bool:
    names = _exception_class_names(exc)
    return bool(
        names
        & {
            "TimeoutError",
            "Timeout",
            "ConnectTimeout",
            "ReadTimeout",
            "ConnectionError",
        }
    )


def call_with_network_retry(
    func: Callable[..., T],
    *args: Any,
    limiter: TushareRateLimiter,
    **kwargs: Any,
) -> T:
    """Retry exactly once, and only for typed transient network failures."""
    for attempt in range(2):
        limiter.wait()
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if attempt == 0 and is_transient_network_error(exc):
                continue
            raise
    raise RuntimeError("unreachable retry state")


def classify_tushare_exception(
    exc: Exception,
    token: str | None = None,
) -> tuple[MarketDataErrorCode, str]:
    message = str(exc)
    if token:
        message = message.replace(token, "[REDACTED]")
    lowered = message.lower()
    if is_transient_network_error(exc):
        code = TIMEOUT if "timeout" in " ".join(_exception_class_names(exc)).lower() else REMOTE_DISCONNECTED
    elif any(marker in message for marker in ("权限", "积分")) or "permission" in lowered:
        code = PERMISSION_DENIED
    elif any(marker in message for marker in ("频次", "限频")) or "rate limit" in lowered or "over limit" in lowered:
        code = RATE_LIMITED
    elif "timeout" in lowered or "timed out" in lowered or "time limit" in lowered:
        code = TIMEOUT
    elif "disconnect" in lowered or "connection" in lowered:
        code = REMOTE_DISCONNECTED
    elif "token" in lowered or "auth" in lowered:
        code = AUTH_MISSING
    else:
        code = UNKNOWN_ERROR
    return cast(MarketDataErrorCode, code), message


def request_fingerprint(endpoint: str, params: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"endpoint": endpoint, "params": dict(sorted(params.items()))},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_date(value: str | date) -> date:
    """Parse ``date``, ISO ``YYYY-MM-DD[...]`` or TuShare ``YYYYMMDD`` input."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if "-" in raw[:10]:
        return date.fromisoformat(raw[:10])
    return datetime.strptime(raw[:8], "%Y%m%d").date()


def compact_date(value: str | date) -> str:
    return parse_date(value).strftime("%Y%m%d")


def normalize_date_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return pd.NA
    raw = str(value).strip()
    if not raw:
        return pd.NA
    return parse_date(raw).isoformat()


def normalize_date_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    normalized = frame.copy()
    for column in columns:
        if column in normalized.columns:
            normalized[column] = normalized[column].map(normalize_date_value)
    return normalized


def normalize_numeric_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    normalized = frame.copy()
    for column in columns:
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="raise")
    return normalized


class TushareProviderBase:
    """Shared token, client, throttling, retry, and raw-frame validation."""

    def __init__(
        self,
        token: str | None = None,
        client: Any | None = None,
        client_factory: ClientFactory | None = None,
        env_path: Path | None = None,
        rate_limiter: TushareRateLimiter | None = None,
    ) -> None:
        self.token = token if token is not None else os.environ.get("TUSHARE_TOKEN") or (
            read_tushare_token(env_path) if env_path is not None else None
        )
        self._client = client
        self._client_factory = client_factory
        self._rate_limiter = rate_limiter or (_GLOBAL_RATE_LIMITER if client is None else TushareRateLimiter(10**9))

    def _request_frame(
        self,
        source: str,
        endpoint: str,
        params: Mapping[str, Any],
        required_columns: set[str],
        allow_empty: bool = False,
    ) -> MarketDataResult[pd.DataFrame]:
        fingerprint = request_fingerprint(endpoint, params)
        client_result = self._client_or_failure(source, fingerprint)
        if isinstance(client_result, MarketDataResult):
            return client_result
        try:
            method = getattr(client_result, endpoint)
            value = call_with_network_retry(method, limiter=self._rate_limiter, **dict(params))
        except Exception as exc:
            code, message = classify_tushare_exception(exc, self.token)
            return MarketDataResult(None, "failed", source, now(), error_code=code, error_message=message, request_fingerprint=fingerprint)
        return self._validate_frame(value, source, fingerprint, required_columns, allow_empty)

    def _client_or_failure(
        self,
        source: str,
        fingerprint: str,
    ) -> Any | MarketDataResult[pd.DataFrame]:
        if not self.token:
            return MarketDataResult(
                None,
                "failed",
                source,
                now(),
                error_code=AUTH_MISSING,
                error_message="TUSHARE_TOKEN is not configured",
                request_fingerprint=fingerprint,
            )
        if self._client is None:
            try:
                self._client = self._build_client()
            except Exception as exc:
                code, message = classify_tushare_exception(exc, self.token)
                return MarketDataResult(None, "failed", source, now(), error_code=code, error_message=message, request_fingerprint=fingerprint)
        return self._client

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(cast(str, self.token))
        import tushare as ts

        return ts.pro_api(cast(str, self.token))

    @staticmethod
    def _validate_frame(
        value: Any,
        source: str,
        fingerprint: str,
        required_columns: set[str],
        allow_empty: bool,
    ) -> MarketDataResult[pd.DataFrame]:
        if value is None:
            return MarketDataResult(None, "failed", source, now(), error_code=EMPTY_RESPONSE, request_fingerprint=fingerprint)
        if not isinstance(value, pd.DataFrame):
            return MarketDataResult(
                None,
                "failed",
                source,
                now(),
                error_code=SCHEMA_CHANGED,
                error_message=f"expected pandas.DataFrame, got {type(value).__name__}",
                request_fingerprint=fingerprint,
            )
        missing = required_columns - set(value.columns)
        if missing:
            return MarketDataResult(
                None,
                "failed",
                source,
                now(),
                error_code=MISSING_COLUMNS,
                error_message=f"missing columns: {sorted(missing)}",
                request_fingerprint=fingerprint,
            )
        if value.empty and not allow_empty:
            return MarketDataResult(None, "failed", source, now(), error_code=EMPTY_RESPONSE, request_fingerprint=fingerprint)
        return MarketDataResult(
            value.copy(),
            "ok",
            source,
            now(),
            request_fingerprint=fingerprint,
            row_count=len(value),
        )


def invalid_argument_result(source: str, exc: Exception) -> MarketDataResult[Any]:
    """Structured failure for caller input rejected before any remote request."""
    return MarketDataResult(
        None,
        "failed",
        source,
        now(),
        error_code=INVALID_ARGUMENT,
        error_message=str(exc),
    )


def schema_failure_result(result: MarketDataResult[Any], exc: Exception) -> MarketDataResult[Any]:
    """Structured failure for a fetched payload that could not be normalized."""
    return MarketDataResult(
        None,
        "failed",
        result.source,
        result.fetched_at,
        error_code=SCHEMA_CHANGED,
        error_message=str(exc),
        request_fingerprint=result.request_fingerprint,
        row_count=result.row_count,
    )


def replace_frame_result(
    result: MarketDataResult[pd.DataFrame],
    frame: pd.DataFrame,
    source_as_of: str | None,
) -> MarketDataResult[pd.DataFrame]:
    return MarketDataResult(
        frame,
        result.status,
        result.source,
        result.fetched_at,
        fallback_source=result.fallback_source,
        fallback_reason=result.fallback_reason,
        error_code=result.error_code,
        error_message=result.error_message,
        freshness_days=(
            result.freshness_days
            if result.freshness_days is not None
            else _freshness_days(result.fetched_at, source_as_of)
        ),
        adjusted=result.adjusted,
        volume_unit=result.volume_unit,
        source_as_of=source_as_of,
        request_fingerprint=result.request_fingerprint,
        row_count=len(frame),
    )


def _freshness_days(fetched_at: str, source_as_of: str | None) -> int | None:
    if source_as_of is None:
        return None
    try:
        return (datetime.fromisoformat(fetched_at).date() - date.fromisoformat(source_as_of[:10])).days
    except ValueError:
        return None
