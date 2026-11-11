"""Shared, read-only input checks and budget conclusions for the two risk views."""

from __future__ import annotations

import math

from a_share_research.policy.v0 import risk_gates


def finite_number(value: object) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    except OverflowError:
        return False


def positive_number(value: object) -> bool:
    return finite_number(value) and value > 0


def valid_shares(value: object) -> bool:
    return type(value) is int and positive_number(value)


def valid_quote(quote, today: str) -> bool:
    return bool(
        quote
        and positive_number(quote.price)
        and quote.quote_date == today
        and risk_gates._parse_time(quote.quote_as_of) is not None
        and isinstance(quote.source, str)
        and quote.source.strip()
        and not quote.conflicted
    )


def policy(position: float | None = None, portfolio: float | None = None) -> dict:
    overrides = position is not None or portfolio is not None
    values = {
        "max_position_risk_pct": 2.0 if position is None else position,
        "max_portfolio_risk_pct": 8.0 if portfolio is None else portfolio,
    }
    if not all(positive_number(value) for value in values.values()):
        raise ValueError("risk limits must be positive finite numbers")
    return {
        "policy_id": "cli_override" if overrides else "compatibility_default",
        "source": "cli_override" if overrides else "compatibility_default",
        **values,
        "field_sources": {
            "max_position_risk_pct": "compatibility_default"
            if position is None
            else "cli",
            "max_portfolio_risk_pct": "compatibility_default"
            if portfolio is None
            else "cli",
        },
    }


def assess(
    positions: list[dict],
    portfolio_value: float | None,
    limits: dict,
    *,
    account_scope: str | None = None,
    denominator_source: str = "cli",
) -> dict:
    """Keep proven breaches even when the complete risk total is unknown."""
    known = [item["stop_risk"] for item in positions if item["stop_risk"] is not None]
    # Portfolio attribution is known only when every position's split is known.
    gains = [item.get("stop_risk_unrealized_gain") for item in positions]
    known_gain = None if None in gains else sum(gains)
    lower_bound = sum(known)
    if not finite_number(lower_bound):
        lower_bound = None
    complete = len(known) == len(positions) and lower_bound is not None
    denominator_valid = positive_number(portfolio_value)
    total = lower_bound if complete else None
    pct = total / portfolio_value * 100 if complete and denominator_valid else None
    if pct is not None and not finite_number(pct):
        pct = None
    breaches = []
    if denominator_valid:
        for item in positions:
            amount = item["stop_risk"]
            if (
                amount is not None
                and amount / portfolio_value * 100 > limits["max_position_risk_pct"]
            ):
                breaches.append(
                    _breach(
                        item["code"],
                        amount,
                        item.get("stop_risk_unrealized_gain"),
                        portfolio_value,
                        limits["max_position_risk_pct"],
                        account_scope,
                        denominator_source,
                    )
                )
        if (
            lower_bound is not None
            and lower_bound / portfolio_value * 100 > limits["max_portfolio_risk_pct"]
        ):
            breaches.append(
                _breach(
                    None,
                    lower_bound,
                    known_gain,
                    portfolio_value,
                    limits["max_portfolio_risk_pct"],
                    account_scope,
                    denominator_source,
                )
            )
    return {
        "total_stop_risk": total,
        "total_stop_risk_pct": pct,
        "known_stop_risk_lower_bound": lower_bound,
        "risk_budget_status": (
            "over_budget" if breaches else "within_budget" if pct is not None else None
        ),
        "breaches": breaches,
    }


def _breach(
    code, amount, gain, denominator, limit, account_scope, denominator_source
) -> dict:
    scope = "position" if code is not None else "portfolio"
    # Attribution: how much of the at-risk amount is unrealized gain above the
    # rule reference cost versus principal between that cost and the stop.
    drivers = (
        f"principal={amount - gain:.6g} CNY; unrealized_gain={gain:.6g} CNY; "
        if gain is not None
        else "principal=unknown; unrealized_gain=unknown; "
    )
    return {
        "code": code,
        "detail": (
            f"scope={scope}; amount={amount:.6g} CNY; {drivers}"
            f"ratio={amount / denominator * 100:.6g}%; limit={limit:.6g}%; "
            f"denominator={denominator:.6g} CNY; "
            f"denominator_source={denominator_source}; "
            f"account_scope={account_scope or 'unknown'}"
        ),
    }
