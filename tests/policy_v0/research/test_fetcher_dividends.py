"""股息财年/TTM、缺失数据和 Provider 单位转换的固定日期回归测试。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from a_share_research.policy.v0 import fetcher
from a_share_research.core.fetcher_utils import detect_split_ratio


def _make_div_df(
    period: str = "2025-12-31",
    amount_per_10: float = 10.00,
    status: str = "实施分配",
    use_amount_col: bool = True,
):
    data: dict = {
        "报告期": [period],
        "方案进度": [status],
    }
    if use_amount_col:
        data["现金分红-现金分红比例"] = [amount_per_10]
    return pd.DataFrame(data)


def _make_multi_div_df(records):
    return pd.DataFrame(
        {
            "报告期": [period for period, _, _ in records],
            "现金分红-现金分红比例": [amount for _, amount, _ in records],
            "方案进度": [status for _, _, status in records],
        }
    )


# 固定时钟与报告期，避免分红陈旧守卫随真实日期漂移。
_FROZEN_NOW = datetime(2026, 8, 7)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW


@pytest.fixture(autouse=True)
def freeze_fetcher_clock(monkeypatch):
    monkeypatch.setattr(fetcher, "datetime", _FrozenDatetime)


class TestDividendBasis:
    def test_normal_calculation(self):
        div_df = _make_div_df(amount_per_10=10.00)  # 1.0 yuan/share
        dy, dps, reason, year, periods = fetcher._compute_dividend_basis(
            div_df, current_price=20.0
        )
        assert year == 2025
        assert periods == ["2025-12-31"]
        assert dps == 1.0
        assert dy == 5.0  # 1.0 / 20.0 * 100
        assert reason is None

    @pytest.mark.parametrize("price", [None, 0, -1.0])
    def test_invalid_price_returns_none(self, price):
        div_df = _make_div_df(amount_per_10=10.00)
        dy, dps, reason, year, periods = fetcher._compute_dividend_basis(
            div_df, current_price=price
        )
        assert (dy, dps, year, periods) == (None, None, None, [])
        assert reason is not None

    def test_none_div_df_returns_none(self):
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(None, current_price=20.0)
        assert dy is None

    def test_empty_div_df_returns_none(self):
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            pd.DataFrame(), current_price=20.0
        )
        assert dy is None

    def test_missing_report_period_column(self):
        div_df = pd.DataFrame({"现金分红-现金分红比例": [10.0]})
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=20.0
        )
        assert dy is None
        assert "报告期" in reason

    def test_pending_status_with_amount_still_counted(self):
        """'董事会决议通过'/'预案'阶段只要已有具体金额就应计入（与人工核实口径一致的前瞻估计）"""
        div_df = _make_div_df(amount_per_10=10.00, status="董事会决议通过")
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=20.0
        )
        assert dps == 1.0
        assert reason is None

    def test_dividend_older_than_12_months(self):
        # 守卫严格小于 400 天前；用固定时钟减 401 天，避开午夜边界。
        old_period = (_FROZEN_NOW - timedelta(days=401)).strftime("%Y-%m-%d")
        div_df = _make_div_df(period=old_period)
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=20.0
        )
        assert dy is None
        assert "超过12个月" in reason

    def test_zero_dividend_amount(self):
        div_df = _make_div_df(amount_per_10=0)
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=20.0
        )
        assert dy is None
        assert "无有效分红金额" in reason

    def test_missing_amount_column_treated_as_no_data(self):
        div_df = _make_div_df(use_amount_col=False)
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=20.0
        )
        assert dy is None

    def test_interim_plus_annual_same_year_summed(self):
        """同一财年中期+年度分红应合并计入（如宝钢2025中期0.12+年度0.18=0.30）"""
        div_df = pd.DataFrame(
            {
                "报告期": ["2025-06-30", "2025-12-31"],
                "现金分红-现金分红比例": [1.20, 1.80],
                "方案进度": ["实施分配", "实施分配"],
            }
        )
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(div_df, current_price=6.0)
        assert dps == 0.3
        assert dy == 5.0

    def test_uses_latest_complete_fiscal_year(self):
        """中期披露后应取最近完整财年，而不是截断到当前财年中期。"""
        div_df = pd.DataFrame(
            {
                "报告期": ["2025-12-31", "2026-06-30"],
                "现金分红-现金分红比例": [20.00, 10.13],
                "方案进度": ["实施分配", "实施分配"],
            }
        )
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=38.0
        )
        assert dps == 2.0
        assert reason is None

    def test_pending_annual_included_with_implemented_interim(self):
        """本财年年度分红仍是预案阶段，也应与已实施的中期分红合并
        （回归测试：招行案例，正确值=中期1.013+年度1.003=2.016）
        """
        div_df = pd.DataFrame(
            {
                "报告期": ["2024-12-31", "2025-06-30", "2025-12-31"],
                "现金分红-现金分红比例": [20.00, 10.13, 10.03],
                "方案进度": ["实施分配", "实施分配", "董事会决议通过"],
            }
        )
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=38.0
        )
        assert dps == 2.016
        assert reason is None

    def test_interim_dividend_does_not_truncate_fiscal_year(self):
        records = [
            ("2023-06-30", 0.5, "实施分配"),
            ("2023-12-31", 2.0, "实施分配"),
            ("2024-06-30", 1.0, "实施分配"),
            ("2024-12-31", 2.8, "实施分配"),
            ("2025-06-30", 2.2, "实施分配"),
            ("2025-12-31", 3.8, "实施分配"),
            ("2026-06-30", 4.2, "董事会决议通过"),
        ]
        div_df = _make_multi_div_df(records)
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=40.0
        )
        assert dps == 0.6
        assert dy == 1.5
        assert reason is None

    def test_compute_dps_ttm_uses_latest_365_day_window(self):
        records = [
            ("2025-06-30", 2.2, "实施分配"),
            ("2025-12-31", 3.8, "实施分配"),
            ("2026-06-30", 4.2, "董事会决议通过"),
        ]
        assert fetcher.compute_dps_ttm(_make_multi_div_df(records)) == 0.8

    def test_skipped_annual_dividend_fails_closed_instead_of_returning_stale(self):
        div_df = _make_multi_div_df(
            [
                ("2024-12-31", 20.0, "实施分配"),
                ("2025-12-31", 0.0, "实施分配"),
                ("2026-06-30", 42.0, "董事会决议通过"),
            ]
        )
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=40.0
        )
        assert dy is None
        assert dps is None
        assert reason
        assert "2024" in reason

    def test_latest_december_period_matches_full_fiscal_year(self):
        div_df = _make_multi_div_df(
            [
                ("2025-06-30", 2.2, "实施分配"),
                ("2025-12-31", 3.8, "实施分配"),
            ]
        )
        dy, dps, reason, *_ = fetcher._compute_dividend_basis(
            div_df, current_price=40.0
        )
        assert dps == 0.6
        assert fetcher.compute_dps_ttm(div_df) == 0.6
        assert reason is None

    def test_only_interim_dividend_falls_back_to_latest_period_year(self):
        div_df = _make_multi_div_df(
            [
                ("2025-06-30", 2.2, "实施分配"),
                ("2026-06-30", 4.2, "董事会决议通过"),
            ]
        )
        _, dps, reason, *_ = fetcher._compute_dividend_basis(div_df, current_price=40.0)
        assert dps == 0.42
        assert reason is None

    def test_skipped_annual_dividend_ttm_remains_available(self):
        div_df = _make_multi_div_df(
            [
                ("2024-12-31", 20.0, "实施分配"),
                ("2025-12-31", 0.0, "实施分配"),
                ("2026-06-30", 4.2, "董事会决议通过"),
            ]
        )
        assert fetcher.compute_dps_ttm(div_df) == 0.42

    def test_fetch_dividend_keeps_ttm_when_main_dividend_is_rejected(self, monkeypatch):
        div_df = _make_multi_div_df(
            [
                ("2024-12-31", 20.0, "实施分配"),
                ("2025-12-31", 0.0, "实施分配"),
                ("2026-06-30", 4.2, "董事会决议通过"),
            ]
        )
        monkeypatch.setattr(fetcher, "timed_call", lambda *args, **kwargs: div_df)
        results = {}
        null_reasons = {}

        fetcher._fetch_dividend("000001", 40.0, results, null_reasons, 2025)

        assert results["dps_ttm"] == 0.42
        assert "dividend_yield" not in results
        assert "dps" not in results
        assert null_reasons["dividend_yield"]
        assert null_reasons["dps"]

    def test_pending_december_dividend_is_counted(self):
        div_df = _make_multi_div_df(
            [
                ("2025-06-30", 2.2, "实施分配"),
                ("2025-12-31", 3.8, "董事会决议通过"),
                ("2026-06-30", 4.2, "实施分配"),
            ]
        )
        _, dps, reason, *_ = fetcher._compute_dividend_basis(div_df, current_price=40.0)
        assert dps == 0.6
        assert reason is None


def test_tushare_dividend_adapter_rescales_per_share_units_to_per_10_shares(
    monkeypatch,
):
    """TuShare reports per-share (cash_div/stk_co_rate); downstream consumers
    expect AKShare's per-10-share convention. Values mirror 603606's 2025 plan:
    每10股转增2股派5.6元 -> cash_div 0.56, stk_co_rate 0.2."""
    import a_share_research.core.providers as providers

    class FakeProvider:
        def fetch_dividend_history(self, code):
            return SimpleNamespace(
                status="ok",
                value=pd.DataFrame(
                    {
                        "end_date": ["2025-12-31"],
                        "ex_date": ["2026-05-26"],
                        "div_proc": ["实施"],
                        "stk_div": [0.00],
                        "stk_bo_rate": [0.00],
                        "stk_co_rate": [0.20],
                        "cash_div": [0.56],
                    }
                ),
            )

    monkeypatch.setattr(
        providers, "TushareDividendProvider", lambda **kwargs: FakeProvider()
    )
    frame = fetcher._fetch_dividends_tushare("603606")

    assert frame.loc[0, "送转股份-送转总比例"] == pytest.approx(2.00)
    assert frame.loc[0, "现金分红-现金分红比例"] == pytest.approx(5.60)

    # The whole point of the rescale: these two consumers must recover the
    # per-share figures the announcement states.
    assert fetcher._compute_dividend_basis(frame, 38.67)[1] == pytest.approx(0.56)
    assert detect_split_ratio(frame, 2025, as_of=_FROZEN_NOW.date()) == (
        pytest.approx(0.2),
        "2026-05-26",
    )


def test_tushare_dividend_adapter_rescales_stk_div_without_rate_columns(monkeypatch):
    import a_share_research.core.providers as providers

    class FakeProvider:
        def fetch_dividend_history(self, code):
            return SimpleNamespace(
                status="ok",
                value=pd.DataFrame(
                    {
                        "end_date": ["2025-12-31"],
                        "stk_div": [0.10],
                        "cash_div": [0.20],
                    }
                ),
            )

    monkeypatch.setattr(
        providers, "TushareDividendProvider", lambda **kwargs: FakeProvider()
    )
    frame = fetcher._fetch_dividends_tushare("600519")

    assert frame.loc[0, "送转股份-送转总比例"] == pytest.approx(1.0)
    assert frame.loc[0, "现金分红-现金分红比例"] == pytest.approx(2.0)
