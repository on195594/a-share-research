"""Seed rows for the legacy v0 cache tables and quote fakes."""

from __future__ import annotations

import json
from typing import Any

from a_share_research.policy.v0 import db, domain


def set_valid_fundamentals(
    code: str,
    name: str,
    industry: str,
    data: dict[str, Any],
    ttl: int | None = None,
) -> None:
    """Seed one legacy fundamentals row (production writers were retired 11-15)."""
    with db.db_session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO stock_fundamentals
               (code, name, industry, data, updated_at, ttl_hours)
               VALUES (?,?,?,?,?,?)""",
            (code, name, industry, json.dumps(data, ensure_ascii=False),
             domain.utc_now_iso(), 24 if ttl is None else ttl),
        )
        conn.commit()


def record_valid_quote(code: str, price: float = 10.0, *, fetched_at: str | None = None) -> None:
    """Seed one legacy quote snapshot row."""
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO quote_snapshots
               (code, price, quote_date, quote_time, fetched_at, source,
                verification_sources, degraded, valid)
               VALUES (?,?,?,?,?,?,?,0,1)""",
            (code, price, domain.cst_today(), "10:00:00", fetched_at or domain.utc_now_iso(),
             "sina", json.dumps({"sina": {"price": price}})),
        )
        conn.commit()


def seed_indicator(key: str, value: float, as_of: str, source: str = "test") -> None:
    """Seed one legacy market indicator row."""
    with db.db_session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_indicator_snapshots
               (indicator_key, value, as_of, fetched_at, source, status)
               VALUES (?,?,?,?,?,'ok')""",
            (key, value, as_of, domain.utc_now_iso(), source),
        )
        conn.commit()


def seed_qualitative_only(code: str, name: str, industry: str) -> None:
    """Seed one legacy qualitative-only security row."""
    with db.db_session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO qualitative_only_securities
               (code, name, industry, updated_at) VALUES (?,?,?,?)""",
            (code, name, industry, domain.utc_now_iso()),
        )
        conn.commit()


def mock_price_quotes(monkeypatch, fetch):
    """Supply fixture quotes to both risk views without an industry provider."""
    from a_share_research.policy.v0 import commands_holdings, market_quotes

    monkeypatch.setattr(market_quotes, "fetch_current_price_quotes", fetch)
    monkeypatch.setattr(
        commands_holdings,
        "fetch_monitor_price_quotes",
        lambda codes: (fetch(codes), None),
    )
