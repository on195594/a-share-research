"""Realtime quote primitives and session-aware freshness validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Sequence

from a_share_research.core.market_data import SHANGHAI_TZ

__all__ = ["QuoteObservation", "validate_quote_observation"]


@dataclass(frozen=True)
class QuoteObservation:
    """Normalized price observation returned by one realtime source."""

    price: float
    quote_date: str
    quote_time: str
    source: str
    instrument_name: str | None = None

    @property
    def as_of(self) -> str:
        return f"{self.quote_date}T{self.quote_time}"


class MarketSession(str, Enum):
    PRE_OPEN = "pre_open"
    INTRADAY = "intraday"
    NON_TRADING = "non_trading"


def validate_quote_observation(
    observation: QuoteObservation,
    now: datetime,
    trading_dates: Sequence[date],
    *,
    max_intraday_age_seconds: int = 120,
) -> str | None:
    """Validate price fields and session-aware freshness for one source."""
    if (
        isinstance(observation.price, bool)
        or not isinstance(observation.price, (int, float))
        or not math.isfinite(float(observation.price))
        or observation.price <= 0
    ):
        return "invalid price"
    if not isinstance(observation.source, str) or not observation.source.strip():
        return "invalid source"
    if observation.instrument_name is not None and (
        not isinstance(observation.instrument_name, str)
        or not observation.instrument_name.strip()
    ):
        return "invalid instrument name"
    try:
        quote_date = date.fromisoformat(observation.quote_date)
        quote_time = time.fromisoformat(observation.quote_time)
    except (TypeError, ValueError):
        return "invalid quote date/time"

    now_shanghai = _ensure_aware_utc(now).astimezone(SHANGHAI_TZ)
    session, expected_date = _market_session(now_shanghai, trading_dates)
    if quote_date != expected_date:
        return f"stale trade date: expected {expected_date.isoformat()}, got {quote_date.isoformat()}"
    if session is not MarketSession.INTRADAY:
        return None

    quote_dt = datetime.combine(quote_date, quote_time, tzinfo=now_shanghai.tzinfo)
    age = (now_shanghai - quote_dt).total_seconds()
    if age < -30 or age > max_intraday_age_seconds:
        return f"stale intraday quote: age={age:.0f}s"
    return None


def _market_session(
    now_shanghai: datetime, trading_dates: Sequence[date]
) -> tuple[MarketSession, date]:
    """Return session and the trade date a valid observation must represent."""
    available = sorted(
        item for item in set(trading_dates) if item <= now_shanghai.date()
    )
    if not available:
        raise ValueError("trading calendar has no date before current time")
    today_is_trading = now_shanghai.date() in available
    current_time = now_shanghai.time().replace(tzinfo=None)
    intraday = time(9, 30) <= current_time <= time(11, 30) or time(
        13, 0
    ) <= current_time <= time(15, 0)
    if today_is_trading and intraday:
        return MarketSession.INTRADAY, now_shanghai.date()
    if today_is_trading and current_time < time(9, 30):
        previous = [item for item in available if item < now_shanghai.date()]
        if not previous:
            raise ValueError("trading calendar has no previous trade date")
        return MarketSession.PRE_OPEN, previous[-1]
    return MarketSession.NON_TRADING, available[-1]


def _ensure_aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)
