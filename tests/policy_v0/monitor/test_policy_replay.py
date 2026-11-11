from datetime import date, timedelta

import pytest

from policy_replay import Bar, Policy, buy_and_hold, replay


def _bars(prices, valuations=None, dividends=None):
    valuations = valuations or [None] * len(prices)
    dividends = dividends or [0.0] * len(prices)
    start = date(2025, 1, 1)
    return [
        Bar(start + timedelta(days=i), price, valuation, dividend)
        for i, (price, valuation, dividend) in enumerate(
            zip(prices, valuations, dividends, strict=True)
        )
    ]


def test_tier_signal_executes_on_next_bar_without_lookahead():
    bars = _bars([100, 126, 120, 120], [0.2, 0.2, 0.2, 0.2])
    result = replay(bars, Policy(fee_bps=0))
    # +25% is first observed at 126, so 1/3 is sold at next close 120.
    assert result.trades == 1
    assert result.turnover == pytest.approx(0.4)
    assert result.final_equity == pytest.approx(120)


def test_valuation_exit_uses_next_bar_and_fees():
    bars = _bars([100, 110, 90], [0.2, 0.90, 0.90])
    result = replay(bars, Policy(fee_bps=100))
    assert result.trades == 1
    assert result.final_equity == pytest.approx(89.1)


def test_stop2_sells_half_on_next_bar():
    bars = _bars([100, 79, 70, 60])
    result = replay(bars, Policy(fee_bps=0))
    assert result.trades == 1
    assert result.turnover == pytest.approx(0.35)
    assert result.final_equity == pytest.approx(65)


def test_dividends_are_total_return_cash_flows():
    bars = _bars([100, 100, 100], dividends=[0, 2, 0])
    result = buy_and_hold(bars)
    assert result.final_equity == pytest.approx(102)
    assert result.total_return == pytest.approx(0.02)
