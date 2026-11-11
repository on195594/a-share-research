"""tests/test_fetcher_utils.py — a_share_research.core.fetcher_utils 单元测试。

所有测试使用构造的 DataFrame，不发起任何网络请求。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from a_share_research.core.fetcher_utils import detect_split_ratio


def _make_fhps_df(
    ex_dates: list[str],
    ratios: list[float],
    prog: list[str] | None = None,
) -> pd.DataFrame:
    data: dict[str, list] = {
        "送转股份-送转总比例": ratios,
        "除权除息日": ex_dates,
    }
    if prog is not None:
        data["方案进度"] = prog
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# 1. fhps_df=None 返回 (0.0, None)
# ---------------------------------------------------------------------------

def test_detect_split_ratio_no_data_none():
    assert detect_split_ratio(None, 2024) == (0.0, None)


def test_detect_split_ratio_no_data_empty_df():
    df = pd.DataFrame(columns=["送转股份-送转总比例", "除权除息日"])
    assert detect_split_ratio(df, 2024) == (0.0, None)


def test_detect_split_ratio_no_data_no_report_year():
    df = _make_fhps_df(["2025-03-01"], [2.0], ["实施分配"])
    assert detect_split_ratio(df, None) == (0.0, None)


def test_detect_split_ratio_non_dataframe_input_defensive():
    # 非 DataFrame 类型（如字符串），getattr(x, 'empty', True) → True，返回 (0.0, None)
    assert detect_split_ratio("not-a-dataframe", 2024) == (0.0, None)


# ---------------------------------------------------------------------------
# 2. 无最新年报后送转，返回 (0.0, None)
# ---------------------------------------------------------------------------

def test_detect_split_ratio_no_qualifying_split_before_cutoff():
    # 除权日在 2024-12-31 之前，不计入（latest_report_year=2024）
    df = _make_fhps_df(["2024-06-01"], [2.0], ["实施分配"])
    assert detect_split_ratio(df, 2024) == (0.0, None)


# ---------------------------------------------------------------------------
# 3. 单次送转：10转2（ratio=2.0），返回 (0.2, 除权日)
# ---------------------------------------------------------------------------

def test_detect_split_ratio_single_split():
    ex_date = "2025-03-15"
    df = _make_fhps_df([ex_date], [2.0], ["实施分配"])
    ratio, ex = detect_split_ratio(df, 2024)
    assert abs(ratio - 0.2) < 1e-6, f"期望 0.2，实际 {ratio}"
    assert ex == ex_date


# ---------------------------------------------------------------------------
# 4. 除权日 > today，不计入
# ---------------------------------------------------------------------------

def test_detect_split_ratio_future_ignored():
    future_date = (date.today() + timedelta(days=30)).isoformat()
    df = _make_fhps_df([future_date], [2.0], ["实施分配"])
    assert detect_split_ratio(df, 2024) == (0.0, None)


# ---------------------------------------------------------------------------
# 5. 除权日在最新年报之前，不计入
# ---------------------------------------------------------------------------

def test_detect_split_ratio_before_report_ignored():
    # latest_report_year=2023 → cutoff=2023-12-31；除权日2023-10-01在cutoff前
    df = _make_fhps_df(["2023-10-01"], [2.0], ["实施分配"])
    assert detect_split_ratio(df, 2023) == (0.0, None)


# ---------------------------------------------------------------------------
# 6. 方案进度≠实施分配，不计入
# ---------------------------------------------------------------------------

def test_detect_split_ratio_not_implemented_ignored():
    df = _make_fhps_df(["2025-03-15"], [2.0], ["董事会预案"])
    assert detect_split_ratio(df, 2024) == (0.0, None)


# ---------------------------------------------------------------------------
# 7. 多次送转累乘（累计效应）
# ---------------------------------------------------------------------------

def test_detect_split_ratio_multiple_splits_cumulative():
    # 两次10转2：每次 factor *= 1.2 → 1.2 * 1.2 = 1.44 → ratio = 0.44
    df = _make_fhps_df(
        ["2025-01-10", "2025-06-20"],
        [2.0, 2.0],
        ["实施分配", "实施分配"],
    )
    ratio, ex = detect_split_ratio(df, 2024)
    assert abs(ratio - 0.44) < 1e-5, f"累计送转应为 0.44，实际 {ratio}"
    assert ex == "2025-06-20"  # 取最新除权日


# ---------------------------------------------------------------------------
# 显式 as_of 与 TuShare 原生字段
# ---------------------------------------------------------------------------


def test_detect_split_ratio_explicit_as_of_is_reproducible():
    df = _make_fhps_df(["2025-06-01", "2025-09-01"], [10.0, 5.0], ["实施分配", "实施分配"])
    assert detect_split_ratio(df, 2024, as_of="2025-07-01") == (1.0, "2025-06-01")
    assert detect_split_ratio(df, 2024, as_of=date(2025, 9, 1)) == (2.0, "2025-09-01")
    assert detect_split_ratio(df, 2024, as_of="20250531") == (0.0, None)


def test_detect_split_ratio_accepts_tushare_per_share_columns():
    df = pd.DataFrame(
        {
            "ex_date": ["2025-06-01", "2025-09-01", "2025-10-01", "2026-01-01"],
            "div_proc": ["实施", "实施", "预案", "实施"],
            "stk_div": [0.0, 0.5, 1.0, 1.0],
            "stk_bo_rate": [0.3, None, None, None],
            "stk_co_rate": [0.2, None, None, None],
        }
    )
    # bonus+conversion 0.5 (per share) then stk_div fallback 0.5; 预案 and future rows excluded
    assert detect_split_ratio(df, 2024, as_of="2025-12-31") == (1.25, "2025-09-01")


def test_detect_split_ratio_tushare_and_akshare_agree():
    tushare = pd.DataFrame({"ex_date": ["2025-06-01"], "div_proc": ["实施"], "stk_div": [0.2]})
    akshare = _make_fhps_df(["2025-06-01"], [2.0], ["实施分配"])
    assert detect_split_ratio(tushare, 2024, as_of="2025-12-31") == detect_split_ratio(
        akshare, 2024, as_of="2025-12-31"
    )
