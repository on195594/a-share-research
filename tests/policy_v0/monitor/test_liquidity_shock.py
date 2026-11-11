import json
from datetime import datetime, timedelta, timezone

import pytest

from a_share_research.policy.v0 import commands_holdings, db, domain, risk_gates
from policy_v0.helpers import mock_price_quotes

CST = timezone(timedelta(hours=8))


def snapshot(now, count=501, **extra):
    return {
        "limit_down_count": count,
        "universe": 5000,
        "eligible_count": 4800,
        "coverage": "full",
        "status": "final",
        "source": "official exchange",
        "as_of": now.isoformat(),
        "is_final": True,
        **extra,
    }


def quote(price=8.0, **extra):
    return {
        "price": price,
        "source": "sina",
        "suspended": False,
        "limit_down_locked": False,
        "trading_status": "trading",
        **extra,
    }


def test_snapshot_boundary_and_freshness():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    assert risk_gates.market_snapshot_status(snapshot(now, 500), now)["complete"]
    assert not risk_gates.market_snapshot_status(
        snapshot(now, 501, source="search result"), now
    )["lower_bound_confirmed"]
    assert not risk_gates.market_snapshot_status(
        snapshot(now - timedelta(seconds=121), 500, is_final=False), now
    )["complete"]
    assert (
        risk_gates.market_snapshot_status(snapshot(now, 501), now)["limit_down_count"]
        == 501
    )


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        lambda now: snapshot(now, source="news search"),
        lambda now: snapshot(now - timedelta(seconds=121), is_final=False),
    ],
)
def test_untrusted_or_stale_501_cannot_start_defer(bad_snapshot):
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    result = risk_gates.liquidity_shock_gate(
        {
            "stop_loss_triggered": True,
            "stop_loss_20": 8,
            "quote": quote(),
            "market_snapshot": bad_snapshot(now),
            "now": now,
        }
    )
    assert result["status"] == "incomplete"
    assert result["reason_code"] == "market_snapshot_incomplete"


def test_defer_is_one_shot_and_due_rechecks_price():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    first = risk_gates.liquidity_shock_gate(
        {
            "stop_loss_triggered": True,
            "stop_loss_20": 8,
            "quote": quote(),
            "market_snapshot": snapshot(now),
            "now": now,
        }
    )
    assert first["status"] == "deferred"
    due = now + timedelta(hours=24)
    done = risk_gates.liquidity_shock_gate(
        {
            "stop_loss_triggered": True,
            "stop_loss_20": 8,
            "quote": quote(7.9),
            "market_snapshot": snapshot(due),
            "now": due,
            "existing_defer": first,
        }
    )
    assert done["status"] == "clear"
    assert done["reason_code"] == "stop_loss_20_reconfirmed"
    assert done["defer_started_at"] == first["defer_started_at"]


def test_due_recovered_price_clears_candidate_without_action():
    started = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    due = started + timedelta(hours=24)
    result = risk_gates.liquidity_shock_gate(
        {
            "stop_loss_triggered": True,
            "stop_loss_20": 8,
            "quote": quote(8.01),
            "market_snapshot": snapshot(due),
            "now": due,
            "existing_defer": {"defer_started_at": started.isoformat()},
        }
    )
    assert result["status"] == "clear"
    assert result["action_eligible"] is False
    assert result["reason_code"] == "stop_loss_20_recovered"


def test_untradeable_never_becomes_a_sell_quantity():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    result = risk_gates.liquidity_shock_gate(
        {
            "stop_loss_triggered": True,
            "stop_loss_20": 8,
            "quote": quote(suspended=True),
            "market_snapshot": snapshot(now, 500),
            "now": now,
        }
    )
    assert result["status"] == "untradeable"
    assert result["action_eligible"] is False
    assert "shares" not in result


def test_check_holdings_reuses_active_persisted_defer(capsys, monkeypatch):
    started = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    commands_holdings.cmd_add_holding(["600036", "40", "100", "--notes", "P3测试"])
    with db.db_session() as conn:
        holding_id = conn.execute(
            "SELECT id FROM holdings WHERE code='600036' AND exit_date IS NULL"
        ).fetchone()[0]
        evidence = json.dumps(
            {
                "defer_started_at": started.isoformat(),
                "review_due": (started + timedelta(hours=24)).isoformat(),
                "original_price": 31,
                "original_stop_loss_20": 32,
            }
        )
        conn.execute(
            """INSERT INTO holding_alerts
               (holding_id, code, level, category, reason_code, reason, evidence,
                opened_at, review_due, updated_at)
               VALUES (?, '600036', 'red', 'holding_deterioration',
                       'price_stop2_liquidity_defer', 'P3延迟', ?, ?, ?, ?)""",
            (
                holding_id,
                evidence,
                started.isoformat(),
                (started + timedelta(hours=24)).isoformat(),
                started.isoformat(),
            ),
        )
        conn.commit()
    now = started + timedelta(hours=1)
    captured = []
    real_evaluate = risk_gates.liquidity_shock_gate

    def capture(payload):
        result = real_evaluate(payload)
        captured.append((payload, result))
        return result

    monkeypatch.setattr(domain, "cst_now", lambda: now)
    monkeypatch.setattr(risk_gates, "liquidity_shock_gate", capture)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            _code: (
                commands_holdings.PriceQuote(
                    31,
                    now.date().isoformat(),
                    "11:00:00",
                    "sina",
                    False,
                    False,
                    "trading",
                )
            )
            for _code in codes
        },
    )
    monkeypatch.setattr(
        commands_holdings.market_quotes,
        "fetch_market_limit_down_snapshot",
        lambda: None,
    )

    capsys.readouterr()
    commands_holdings.cmd_check_holdings()
    output = capsys.readouterr().out

    assert captured[0][0]["existing_defer"]["defer_started_at"] == started.isoformat()
    assert captured[0][1]["defer_started_at"] == started.isoformat()
    assert "P3:deferred(price_stop2_liquidity_defer)" in output
    assert "建议立即止损" not in output
