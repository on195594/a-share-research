from datetime import datetime, timedelta, timezone

from a_share_research.policy.v0 import risk_gates

CST = timezone(timedelta(hours=8))


def _snapshot(now, count):
    return {
        "limit_down_count": count,
        "universe": 5000,
        "eligible_count": 4800,
        "coverage": "full",
        "status": "final",
        "source": "official exchange",
        "as_of": now.isoformat(),
        "is_final": True,
    }


def _quote(price=8.0, **extra):
    return {
        "price": price,
        "source": "sina",
        "suspended": False,
        "limit_down_locked": False,
        "trading_status": "trading",
        **extra,
    }


def test_liquidity_shock_500_vs_501_and_only_second_stop():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    base = {
        "stop_loss_triggered": True,
        "stop_loss_20": 8,
        "quote": _quote(),
        "now": now,
    }
    assert (
        risk_gates.liquidity_shock_gate(
            {**base, "market_snapshot": _snapshot(now, 500)}
        )["status"]
        == "clear"
    )
    assert (
        risk_gates.liquidity_shock_gate(
            {**base, "market_snapshot": _snapshot(now, 501)}
        )["status"]
        == "deferred"
    )
    assert (
        risk_gates.liquidity_shock_gate(
            {
                **base,
                "stop_loss_triggered": False,
                "market_snapshot": _snapshot(now, 501),
            }
        )["status"]
        == "not_applicable"
    )


def test_liquidity_shock_due_does_not_extend_window_and_protects_untradeable():
    started = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    due = started + timedelta(hours=24)
    payload = {
        "stop_loss_triggered": True,
        "stop_loss_20": 8,
        "quote": _quote(),
        "market_snapshot": _snapshot(due, 501),
        "now": due,
        "existing_defer": {"defer_started_at": started.isoformat()},
    }
    result = risk_gates.liquidity_shock_gate(payload)
    assert result["status"] == "clear"
    assert result["defer_started_at"] == started.isoformat()
    result = risk_gates.liquidity_shock_gate(
        {**payload, "quote": _quote(suspended=True)}
    )
    assert result["status"] == "untradeable"
    assert result["defer_started_at"] == started.isoformat()


def test_liquidity_shock_incomplete_500_is_not_a_clear_signal():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    result = risk_gates.liquidity_shock_gate(
        {
            "stop_loss_triggered": True,
            "stop_loss_20": 8,
            "quote": _quote(),
            "now": now,
            "market_snapshot": {"limit_down_count": 500, "as_of": now.isoformat()},
        }
    )
    assert result["status"] == "incomplete"
