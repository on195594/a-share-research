from datetime import date, datetime, timezone

import pytest

from a_share_research.core.providers.validated_realtime_quotes import (
    QuoteObservation,
    validate_quote_observation,
)

NOW_UTC = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
TRADING_DATES = [date(2026, 7, 13), date(2026, 7, 14)]


@pytest.mark.parametrize(
    "price", [float("nan"), float("inf"), float("-inf"), True, False, 0]
)
def test_invalid_prices_fail_closed(price: float) -> None:
    error = validate_quote_observation(
        QuoteObservation(price, "2026-07-14", "09:59:00", "sina"),
        NOW_UTC,
        TRADING_DATES,
    )

    assert error == "invalid price"


def test_intraday_freshness_120_second_boundary_is_inclusive() -> None:
    assert (
        validate_quote_observation(
            QuoteObservation(10.0, "2026-07-14", "09:58:00", "sina"),
            NOW_UTC,
            TRADING_DATES,
        )
        is None
    )
    assert "stale" in validate_quote_observation(
        QuoteObservation(10.0, "2026-07-14", "09:57:59", "sina"),
        NOW_UTC,
        TRADING_DATES,
    )


def test_preopen_requires_previous_trade_date() -> None:
    preopen = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)

    assert (
        validate_quote_observation(
            QuoteObservation(10.0, "2026-07-13", "15:00:00", "sina"),
            preopen,
            TRADING_DATES,
        )
        is None
    )
    assert "stale trade date" in validate_quote_observation(
        QuoteObservation(10.0, "2026-07-14", "09:00:00", "sina"),
        preopen,
        TRADING_DATES,
    )


@pytest.mark.parametrize(
    "observation, expected",
    [
        (QuoteObservation(10.0, "bad", "09:59:00", "sina"), "invalid quote date/time"),
        (
            QuoteObservation(10.0, "2026-07-14", "bad", "sina"),
            "invalid quote date/time",
        ),
        (QuoteObservation(10.0, "2026-07-14", "09:59:00", ""), "invalid source"),
        (
            QuoteObservation(10.0, "2026-07-14", "09:59:00", "sina", ""),
            "invalid instrument name",
        ),
    ],
)
def test_invalid_observation_fields_fail_closed(
    observation: QuoteObservation,
    expected: str,
) -> None:
    assert validate_quote_observation(observation, NOW_UTC, TRADING_DATES) == expected


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_quote_observation(
            QuoteObservation(10.0, "2026-07-14", "09:59:00", "sina"),
            NOW_UTC.replace(tzinfo=None),
            TRADING_DATES,
        )
