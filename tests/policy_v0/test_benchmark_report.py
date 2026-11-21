"""Ledger cash flows vs the same flows into an index (public market equivalent)."""

import pytest

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
