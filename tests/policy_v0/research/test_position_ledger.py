import pytest

from a_share_research.policy.v0.position_ledger import calculate_lifecycle_return


def test_calculates_cash_flow_return_across_partial_sales_and_dividend():
    events = [
        ("buy", "2026-01-01", 500, 10.0, 0.0, 0.0, None, 0),
        ("sell", "2026-02-01", 200, 15.0, 0.0, 0.0, None, 0),
        ("dividend", "2026-03-01", None, None, 0.0, 0.0, 500.0, 0),
        ("sell", "2026-04-01", 300, 8.0, 0.0, 0.0, None, 0),
    ]

    result = calculate_lifecycle_return(events, 0, end_date="2026-04-01")

    assert result.pnl == pytest.approx(900.0)
    assert result.total_return_pct == pytest.approx(18.0)
    assert result.holding_days == 90


def test_rejects_invalid_or_inverted_lifecycle():
    events = [("buy", "2026-02-01", 100, 10.0, 0.0, 0.0, None, 0)]

    with pytest.raises(ValueError, match="早于首笔"):
        calculate_lifecycle_return(events, 0, end_date="2026-01-01")

    with pytest.raises(ValueError, match="非有限"):
        calculate_lifecycle_return(
            [("buy", "2026-01-01", 100, float("nan"), 0.0, 0.0, None, 0)],
            0,
            end_date="2026-01-01",
        )
