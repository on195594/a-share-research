"""Pure, fail-closed P3 liquidity-shock gate.

The module deliberately contains no database, network, or CLI access.  Adapters
normalise provider facts before calling these functions; missing or untrusted
facts remain ``incomplete`` rather than being inferred as safe.  The P0-P2
fundamentals gates were removed on 2026-11-15 after their evidence writers were
retired.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

CST = timezone(timedelta(hours=8))

OFFICIAL_SOURCE_MARKERS = (
    "official",
    "exchange",
    "sse",
    "szse",
    "bse",
    "csrc",
    "证监会",
    "交易所",
    "公司公告",
    "年报",
    "审计",
)
_UNTRUSTED_SOURCE_MARKERS = ("news", "search", "media", "雪球", "东方财富", "新闻")


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _sources(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _official(sources: list[str]) -> bool:
    if not sources:
        return False
    lowered = " ".join(sources).lower()
    return not any(
        marker.lower() in lowered for marker in _UNTRUSTED_SOURCE_MARKERS
    ) and any(marker.lower() in lowered for marker in OFFICIAL_SOURCE_MARKERS)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)


def market_snapshot_status(
    snapshot: Mapping[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """Validate P3 market snapshot freshness and coverage without fetching it."""
    snap = snapshot or {}
    count = _number(snap.get("limit_down_count"))
    count_valid = count is not None and count >= 0 and count.is_integer()
    universe = _number(snap.get("universe", snap.get("eligible")))
    eligible = _number(snap.get("eligible_count", snap.get("eligible")))
    universe_valid = universe is not None and universe > 0 and universe.is_integer()
    eligible_valid = (
        eligible is not None
        and eligible >= 0
        and eligible.is_integer()
        and eligible <= universe
        if universe is not None
        else False
    )
    sources = _sources(snap.get("sources", snap.get("source")))
    as_of = _parse_time(snap.get("as_of"))
    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    age = (current - as_of).total_seconds() if as_of else None
    final = bool(snap.get("is_final", snap.get("final")))
    complete = (
        count_valid
        and universe_valid
        and eligible_valid
        and bool(snap.get("coverage", snap.get("coverage_status")))
        and bool(snap.get("status", snap.get("snapshot_status")))
        and bool(sources)
        and _official(sources)
        and as_of is not None
        and (final or age is not None and 0 <= age <= 120)
    )
    if final and as_of is not None and as_of.date() != current.date():
        complete = False
    lower_bound_confirmed = bool(
        count_valid
        and count > 500
        and _official(sources)
        and as_of is not None
        and (
            final
            and as_of.date() == current.date()
            or not final
            and age is not None
            and 0 <= age <= 120
        )
    )
    return {
        "status": "clear" if complete else "incomplete",
        "complete": complete,
        "lower_bound_confirmed": lower_bound_confirmed,
        "limit_down_count": int(count) if count_valid else None,
        "lower_bound": int(count) if count_valid else None,
        "as_of": snap.get("as_of"),
        "sources": sources,
        "reason_code": "market_snapshot_valid"
        if complete
        else "market_snapshot_incomplete",
    }


def liquidity_shock_gate(
    payload: Mapping[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Pure P3 one-shot defer state machine for a second-stop candidate."""
    data: dict[str, Any] = dict(payload or {})
    data.update(kwargs)
    if not data.get("stop_loss_triggered"):
        return {
            "status": "not_applicable",
            "action_eligible": False,
            "reason_code": "stop_loss_20_not_triggered",
        }
    raw_quote = data.get("quote")
    quote = (
        dict(raw_quote)
        if isinstance(raw_quote, Mapping)
        else {
            name: getattr(raw_quote, name, None)
            for name in (
                "price",
                "source",
                "suspended",
                "limit_down_locked",
                "trading_status",
            )
        }
        if raw_quote is not None
        else {}
    )
    price = _number(quote.get("price", data.get("price")))
    line = _number(data.get("stop_loss_20"))
    if price is None or line is None or line <= 0:
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "quote_or_stop_line_missing",
        }
    market = market_snapshot_status(data.get("market_snapshot"), data.get("now"))
    existing = (
        data.get("existing_defer")
        if isinstance(data.get("existing_defer"), Mapping)
        else {}
    )
    started = _parse_time(existing.get("defer_started_at"))
    now = (
        data.get("now") if isinstance(data.get("now"), datetime) else datetime.now(CST)
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    if started is None and market.get("lower_bound_confirmed") is True:
        started = now
    if started is not None:
        line = _number(existing.get("original_stop_loss_20")) or line
        due = started + timedelta(hours=24)
        result = {
            "status": "deferred",
            "action_eligible": False,
            "reason_code": "price_stop2_liquidity_defer",
            "defer_started_at": started.isoformat(),
            "review_due": due.isoformat(),
            "original_price": existing.get("original_price", price),
            "original_stop_loss_20": existing.get("original_stop_loss_20", line),
            "market_snapshot": data.get("market_snapshot"),
        }
        if now < due:
            return result
        trading_status = quote.get("trading_status")
        if (
            quote.get("suspended") is None
            or quote.get("limit_down_locked") is None
            or trading_status is None
            or not quote.get("source")
        ):
            result.update(
                status="incomplete", reason_code="review_quote_tradability_missing"
            )
            return result
        if (
            quote.get("suspended")
            or quote.get("limit_down_locked")
            or str(trading_status).lower() not in {"trading", "active", "可交易"}
        ):
            result.update(status="untradeable", reason_code="review_quote_untradeable")
            return result
        recovered = price > line
        result.update(
            status="clear",
            action_eligible=not recovered,
            reason_code="stop_loss_20_recovered"
            if recovered
            else "stop_loss_20_reconfirmed",
        )
        return result
    if not market["complete"] and not market["lower_bound_confirmed"]:
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "market_snapshot_incomplete",
        }
    if market["lower_bound_confirmed"]:
        return {
            "status": "deferred",
            "action_eligible": False,
            "reason_code": "price_stop2_liquidity_defer",
            "defer_started_at": now.isoformat(),
            "review_due": (now + timedelta(hours=24)).isoformat(),
            "original_price": price,
            "original_stop_loss_20": line,
            "market_snapshot": data.get("market_snapshot"),
        }
    if (
        quote.get("suspended") is None
        or quote.get("limit_down_locked") is None
        or quote.get("trading_status") is None
        or not quote.get("source")
    ):
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "quote_tradability_missing",
        }
    if (
        quote.get("suspended")
        or quote.get("limit_down_locked")
        or str(quote.get("trading_status")).lower()
        not in {"trading", "active", "可交易"}
    ):
        return {
            "status": "untradeable",
            "action_eligible": False,
            "reason_code": "quote_untradeable",
        }
    return {
        "status": "clear",
        "action_eligible": True,
        "reason_code": "stop_loss_20_clear",
        "price": price,
        "stop_loss_20": line,
    }
