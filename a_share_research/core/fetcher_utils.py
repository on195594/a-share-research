from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from a_share_research.core.market_data import shanghai_today
from a_share_research.core.providers.tushare_common import parse_date

# AKShare stock_fhps_detail_em: ratios are per 10 shares.
_AK_RATIO = "送转股份-送转总比例"
_AK_EX_DATE = "除权除息日"
_AK_PROGRESS = "方案进度"
_AK_IMPLEMENTED = "实施分配"
# TuShare dividend (TushareDividendProvider): ratios are per share.
_TS_EX_DATE = "ex_date"
_TS_PROGRESS = "div_proc"
_TS_IMPLEMENTED = "实施"


def detect_split_ratio(
    fhps_df: Any,
    latest_report_year: int | None,
    *,
    as_of: date | str | None = None,
) -> tuple[float, str | None]:
    """Detect post-report-period stock splits and return cumulative dilution factor.

    Returns ``(ratio, ex_date_str)``; ``ratio=0.0`` means no qualifying split.
    Only implemented splits with ``report_year-12-31 < ex_date <= as_of`` count.
    ``as_of`` defaults to today's Asia/Shanghai date; pass it explicitly for
    reproducible results. Accepts AKShare ``stock_fhps_detail_em`` columns (per
    10 shares) or TuShare ``dividend`` columns (``stk_bo_rate`` + ``stk_co_rate``,
    falling back to ``stk_div``; per share). Non-DataFrame input returns
    ``(0.0, None)``.
    """
    if fhps_df is None or getattr(fhps_df, "empty", True) or not latest_report_year:
        return 0.0, None
    columns = set(fhps_df.columns)
    if {_AK_RATIO, _AK_EX_DATE} <= columns:
        ratio = pd.to_numeric(fhps_df[_AK_RATIO], errors="coerce").fillna(0) / 10
        ex_date_col, progress_col, implemented = _AK_EX_DATE, _AK_PROGRESS, _AK_IMPLEMENTED
    elif _TS_EX_DATE in columns and columns & {"stk_div", "stk_bo_rate", "stk_co_rate"}:
        ratio = _tushare_per_share_ratio(fhps_df)
        ex_date_col, progress_col, implemented = _TS_EX_DATE, _TS_PROGRESS, _TS_IMPLEMENTED
    else:
        return 0.0, None

    reference = parse_date(as_of) if as_of is not None else shanghai_today()
    ex_date = pd.to_datetime(fhps_df[ex_date_col], errors="coerce")
    cond = (
        (ex_date > pd.Timestamp(f"{latest_report_year}-12-31"))
        & (ex_date <= pd.Timestamp(reference))
        & (ratio > 0)
    )
    if progress_col in columns:
        cond &= fhps_df[progress_col] == implemented
    if not cond.any():
        return 0.0, None
    factor = float((1.0 + ratio[cond]).prod())
    return round(factor - 1.0, 6), ex_date[cond].max().strftime("%Y-%m-%d")


def _tushare_per_share_ratio(frame: pd.DataFrame) -> pd.Series:
    def numeric(column: str) -> pd.Series:
        if column not in frame.columns:
            return pd.Series(0.0, index=frame.index)
        return pd.to_numeric(frame[column], errors="coerce").fillna(0)

    split = numeric("stk_bo_rate") + numeric("stk_co_rate")
    return split.where(split != 0, numeric("stk_div"))
