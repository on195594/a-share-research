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
             domain.utc_now().isoformat(), 24 if ttl is None else ttl),
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
            (code, price, domain.cst_today(), "10:00:00", fetched_at or domain.utc_now().isoformat(),
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
            (key, value, as_of, domain.utc_now().isoformat(), source),
        )
        conn.commit()


def seed_qualitative_only(code: str, name: str, industry: str) -> None:
    """Seed one legacy qualitative-only security row."""
    with db.db_session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO qualitative_only_securities
               (code, name, industry, updated_at) VALUES (?,?,?,?)""",
            (code, name, industry, domain.utc_now().isoformat()),
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


def seed_position(
    code: str,
    price: float,
    shares: int | None = None,
    *,
    day: str = "2026-01-05",
    fees: float = 0.0,
    notes: str | None = None,
    framework: str = "A通用",
) -> int:
    """Seed one open position the way the retired v0 add-holding stored it."""
    sl15, sl20 = domain.get_stop_loss_pct(framework)
    cost = (price * shares + fees) / shares if shares else price
    with db.db_session() as conn:
        holding_id = conn.execute(
            """INSERT INTO holdings
               (code, cost_price, shares, buy_date, stop_loss_15, stop_loss_20, notes,
                updated_at, framework, initial_shares, main_entry_date, main_entry_basis,
                reference_cost, framework_confident)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (code, cost, shares, day, round(price * sl15, 3), round(price * sl20, 3),
             notes, domain.utc_now().isoformat(), framework, shares, day,
             cost * shares if shares else None, price),
        ).lastrowid
        if shares:
            conn.execute(
                """INSERT INTO holding_events
                   (holding_id, code, event_type, event_date, shares, price, fees, created_at)
                   VALUES (?, ?, 'buy', ?, ?, ?, ?, ?)""",
                (holding_id, code, day, shares, price, fees, domain.utc_now().isoformat()),
            )
        conn.execute(
            "INSERT INTO holding_tier_state (holding_id, updated_at) VALUES (?, ?)",
            (holding_id, domain.utc_now().isoformat()),
        )
        conn.commit()
    return int(holding_id)


def seed_event(
    holding_id: int,
    event_type: str,
    day: str,
    *,
    shares: int | None = None,
    price: float | None = None,
    fees: float = 0.0,
    tax: float = 0.0,
    cash: float | None = None,
    close: bool = False,
) -> None:
    """Append one ledger event; a sell updates the position's shares, ``close`` ends it."""
    with db.db_session() as conn:
        code, held = conn.execute(
            "SELECT code, shares FROM holdings WHERE id=?", (holding_id,)
        ).fetchone()
        conn.execute(
            """INSERT INTO holding_events
               (holding_id, code, event_type, event_date, shares, price, fees, tax,
                cash_amount, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (holding_id, code, event_type, day, shares, price, fees, tax, cash,
             domain.utc_now().isoformat()),
        )
        if event_type == "sell":
            left = 0 if close else held - shares
            conn.execute("UPDATE holdings SET shares=? WHERE id=?", (left, holding_id))
        if close:
            conn.execute(
                "UPDATE holdings SET exit_price=?, exit_date=? WHERE id=?",
                (price, day, holding_id),
            )
        conn.commit()
