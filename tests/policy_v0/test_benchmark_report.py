"""Ledger cash flows vs the same flows into an index (public market equivalent)."""

import pytest

from a_share_research.policy.v0 import cache, commands_holdings
from a_share_research.policy.v0.position_ledger import (
    ledger_cash_flows,
    public_market_equivalent,
)

EVENTS = [
    ("buy", "2026-05-27", 100, 100.0, 5.0, 0.0, None, 0),
    ("dividend", "2026-07-09", None, None, 0.0, 0.0, 300.0, 0),
    ("sell", "2026-09-30", 50, 120.0, 5.0, 3.0, None, 0),
    ("adjustment", "2026-09-30", 0, 99.0, 0.0, 0.0, None, 0),
]


def test_cash_flows_include_costs_and_dividends():
    assert ledger_cash_flows(EVENTS) == [
        ("2026-05-27", -10005.0),
        ("2026-07-09", 300.0),
        ("2026-09-30", 5992.0),
    ]


def test_mirror_trades_on_next_close_and_values_at_last_close():
    flows = ledger_cash_flows(EVENTS)
    # Index: 100 on the buy (5-27 is a holiday, next close 5-28), 120 on 7-09,
    # 150 on 9-30 and 160 at valuation.
    closes = {"2026-05-28": 100.0, "2026-07-09": 120.0, "2026-09-30": 150.0,
              "2026-10-21": 160.0}
    units = 10005 / 100 - 300 / 120 - 5992 / 150
    expected = -10005 + 300 + 5992 + units * 160
    assert public_market_equivalent(flows, closes) == pytest.approx(expected)


def test_flow_after_last_index_close_fails_closed():
    with pytest.raises(ValueError):
        public_market_equivalent([("2026-10-22", -100.0)], {"2026-10-21": 1.0})


def test_report_values_open_shares_at_common_valuation_day(
    isolated_cache_database, monkeypatch, capsys
):
    import sqlite3

    from policy_v0.monitor.test_monitor_snapshot import _seed_holding

    holding_id = _seed_holding(isolated_cache_database, "600900", shares=50)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.executemany(
            """INSERT INTO holding_events (holding_id, code, event_type, event_date,
               shares, price, fees, tax, cash_amount, inferred, created_at)
               VALUES (?, '600900', ?, ?, ?, ?, ?, ?, ?, ?, '2026-10-22')""",
            [(holding_id, *row) for row in EVENTS],
        )
    closes = {"2026-05-28": 100.0, "2026-07-09": 120.0, "2026-09-30": 150.0,
              "2026-10-21": 160.0}
    monkeypatch.setattr(commands_holdings, "_index_closes", lambda *_: closes)
    seen = []
    monkeypatch.setattr(
        commands_holdings, "_close_on", lambda code, day: seen.append((code, day)) or 130.0
    )
    assert cache.main(["benchmark-report", "--end", "2026-10-22"]) == 0
    out = capsys.readouterr().out
    assert seen == [("600900", "2026-10-21")]
    # 50 shares x 130 left; ledger P&L = -10005 + 300 + 5992 + 6500 = 2787.
    assert "账本净盈亏：+2,787 元" in out
    assert "沪深300全收益" in out and "中证红利全收益" in out
