#!/usr/bin/env python3
"""
A股基本面数据自动获取工具
用法：
  a-stock-fetch fetch <股票代码>   获取并缓存基本面数据
  a-stock-fetch quarter <股票代码> [--json]  只读季度与TTM核对
  fetcher.py check <股票代码>   验证缓存数据质量
  fetcher.py batch              批量更新 watchlist 所有股票
"""

import os
import re
import sys
import logging
import multiprocessing as mp
from decimal import Decimal, InvalidOperation
from queue import Empty
from typing import Any, Callable
from functools import lru_cache
from datetime import datetime, date, timedelta, timezone
import requests

from a_share_research.core.fetcher_utils import detect_split_ratio as _detect_split_ratio
from a_share_research.core.providers import (
    QuoteObservation,
)

from a_share_research.policy.v0 import db, paths, risk_gates, store

logger = logging.getLogger(__name__)
if not logger.handlers:
    _stdout_handler = logging.StreamHandler(sys.stdout)
    _stdout_handler.setFormatter(logging.Formatter("%(message)s"))
    _stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)
    logger.addHandler(_stdout_handler)

    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setFormatter(logging.Formatter("%(message)s"))
    _stderr_handler.setLevel(logging.ERROR)
    logger.addHandler(_stderr_handler)

    logger.setLevel(logging.INFO)
    logger.propagate = False

API_TIMEOUT = 10  # 每个行情/财务 API 调用的超时秒数
PE_TIMEOUT = 30  # PE 历史分位计算允许更长时间（需拉历史价格）
BOND_YIELD_REFRESH_INTERVAL = timedelta(hours=1)

# 结构化数据源开关：'tushare'（默认）/ 'akshare'（回退）。
# 出问题时无需改代码，设 FETCHER_DATA_SOURCE=akshare 即可整体退回旧链路。
# 例外：10年期国债收益率恒走 AKShare——TuShare 的 yc_cb 是单独授权接口
# （官方文档原文"属于单独的权限接口，请在群里联系群主或管理员"），不随积分开通。
# 实时行情不受此开关影响：统一使用新浪单源，并校验交易日历与时间戳。
DATA_SOURCE = (
    os.environ.get("FETCHER_DATA_SOURCE", "tushare").strip().lower() or "tushare"
)

# 字段注册表：key → (显示标签, 数据来源层)
#   structured = 结构化接口（默认 TuShare，FETCHER_DATA_SOURCE=akshare 时回退）
#   computed  = 从历史数据自行构造
#   web       = 需要 WebSearch 补充（fetcher 不负责）
FIELDS = {
    "pe_static": ("PE_静态（年报EPS，非TTM）", "computed"),
    "pe_ttm": ("PE_静态兼容别名（deprecated）", "compatibility"),
    "pe_ttm_true": ("PE_TTM（总市值/TTM归母净利润）", "computed"),
    "net_profit_ttm": ("TTM归母净利润(元)", "computed"),
    "net_profit_ttm_yoy": ("TTM归母净利润同比(%)", "computed"),
    "peg_ttm": ("PEG_TTM", "computed"),
    "total_market_cap": ("总市值(元)", "structured"),
    "pb": ("PB（当前价/同期BPS）", "computed"),
    "valuation_compatibility": ("PB/BPS最新报告口径兼容性", "computed"),
    "roe_3y_avg": ("ROE近3年均值(%)", "structured"),
    "net_profit_growth": ("净利润增速近3年均值(%)", "structured"),
    "debt_ratio": ("资产负债率(%)", "structured"),
    "interest_bearing_to_total_debt": ("有息负债占总负债比(%)", "structured"),
    "dividend_yield": ("股息率(%)", "structured"),
    "dps": ("每股分红(元)", "structured"),
    "dps_ttm": ("每股分红TTM(元)", "structured"),
    "pe_percentile_5y": ("PE历史5年分位(%)", "computed"),
    "pe_percentile_10y": ("PE历史10年分位(%)", "computed"),
    "pb_percentile_10y": ("PB历史10年分位(%)", "computed"),
    "price_change_5d": ("近5个交易日涨跌幅(%)", "computed"),
    "ps_ttm": ("PS_TTM", "structured"),
    "ps_percentile_5y": ("PS_TTM历史5年分位(%)", "computed"),
    "float_to_total_ratio": ("流通/总市值比(%)", "structured"),
    "gross_margin": ("毛利率(%)", "structured"),
    "revenue_growth_3y": ("收入增速近3年均值(%)", "structured"),
    "current_ratio": ("流动比率", "structured"),
    "operating_cf_per_share": ("每股经营现金流(元)", "structured"),
    "eps": ("基本每股收益(元)", "structured"),
    "latest_report_snapshot": ("最新中报/季报方向快照", "structured"),
    "regulatory_gate": ("P0监管合规门", "computed"),
    "roe_structural_gate": ("P1 ROE结构门", "computed"),
    "cash_flow_gate": ("P2自由现金流门", "computed"),
    "bps": ("每股净资产(元)", "structured"),
    "industry_status": ("行业来源状态", "structured"),
    "bond_yield_10y": ("10年期国债收益率(%)", "akshare"),
    "nim": ("净息差（银行）", "web"),
    "npl_ratio": ("不良贷款率（银行）", "web"),
    "provision_coverage": ("拨备覆盖率（银行）", "web"),
}


# ─── 工具函数 ────────────────────────────────────────────────────────────────


def _timed_call_worker(
    result_queue: mp.Queue,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """Run one API call in an isolated process so the parent can terminate it."""
    try:
        result_queue.put(("OK", fn(*args, **kwargs)))
    except Exception as exc:
        result_queue.put(("ERROR", str(exc)))


def timed_call(
    fn: Callable[..., Any], *args: Any, timeout: int = API_TIMEOUT, **kwargs: Any
) -> Any | str | tuple[str, str]:
    """Run a potentially blocking API call in a killable child process.

    A thread executor can return ``TIMEOUT`` while Python still waits for its
    non-daemon worker during interpreter shutdown.  Fetching in a child process
    makes the timeout a real upper bound for the CLI process as well.
    """
    try:
        context = mp.get_context("fork")
        result_queue = context.Queue(maxsize=1)
        process = context.Process(
            target=_timed_call_worker,
            args=(result_queue, fn, args, kwargs),
        )
        process.start()
    except Exception as exc:
        return ("ERROR", f"无法启动超时隔离进程: {exc}")

    try:
        status, payload = result_queue.get(timeout=timeout)
    except Empty:
        process.terminate()
        process.join()
        return "TIMEOUT"
    except Exception as exc:
        process.terminate()
        process.join()
        return ("ERROR", str(exc))
    finally:
        result_queue.close()
        result_queue.join_thread()

    process.join()
    if status == "OK":
        return payload
    return ("ERROR", payload)


def _is_timed_call_error(result: Any) -> bool:
    """判断 timed_call 返回值是否为错误哨兵（'TIMEOUT' 或 ('ERROR', msg)）。

    result 可能是 DataFrame（成功时），== 'TIMEOUT' 在 DataFrame 上会做逐元素比较
    返回非标量结果，导致后续 bool() 抛 ValueError，因此先用 isinstance 收窄类型。
    """
    if isinstance(result, str):
        return result == "TIMEOUT"
    return isinstance(result, tuple) and len(result) == 2 and result[0] == "ERROR"


def timed_call_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    timeout: int = API_TIMEOUT,
    max_retries: int = 3,
    **kwargs: Any,
) -> Any | str | tuple[str, str]:
    """带指数退避的重试版 timed_call，专用于已知不稳定的 API"""
    import time

    result: Any = ("ERROR", "no attempts made")  # max_retries=0 时有明确返回值
    for attempt in range(max_retries):
        result = timed_call(fn, *args, timeout=timeout, **kwargs)
        if _is_timed_call_error(result):
            if attempt < max_retries - 1:
                wait = 2**attempt  # 1s, 2s, 4s
                logger.info("  [retry] 第 %d 次失败，%ds 后重试...", attempt + 1, wait)
                time.sleep(wait)
            continue
        return result  # 成功则返回
    return result  # 所有重试耗尽，返回最后一次结果


def parse_float(val: Any, default: float | None = None) -> float | None:
    """将财务数据中的各种格式转为 float，无效返回 default"""
    if val is None:
        return default
    s = str(val).replace("%", "").strip()
    if s in ("--", "-", "None", "nan", "False", "True", ""):
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def avg_of(series: Any, n: int) -> float | None:
    """取 series 最后 n 行，计算非 None 值的均值，保留2位小数。series 为 None 时返回 None。"""
    if series is None:
        return None
    vals = [parse_float(v) for v in series.tail(n) if parse_float(v) is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _market_prefix(code: str) -> str:
    """返回新浪行情接口所需的市场前缀（sh/sz/bj）。
    沪市：6 开头；北交所：4/8/920 开头；其余为深市。
    """
    if code.startswith("6"):
        return "sh"
    if code.startswith(("4", "8", "920")):
        return "bj"
    return "sz"


def _fetch_sina_quote(code: str) -> QuoteObservation | None:
    response = requests.get(
        f"https://hq.sinajs.cn/list={_market_prefix(code)}{code}",
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=8,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    start, end = response.text.find('"') + 1, response.text.rfind('"')
    if start <= 0 or end <= start:
        return None
    fields = response.text[start:end].split(",")
    if len(fields) < 32:
        return None
    price = parse_float(fields[3])
    if price is None:
        return None
    return QuoteObservation(
        price,
        fields[30],
        fields[31],
        "sina",
        instrument_name=fields[0].strip() or None,
    )


@lru_cache(maxsize=1)
def _fetch_trading_dates_akshare() -> tuple[date, ...]:
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return ()
    parsed = __import__("pandas").to_datetime(frame["trade_date"], errors="coerce")
    return tuple(item.date() for item in parsed.dropna())


def _fetch_trading_dates_tushare() -> tuple[date, ...]:
    try:
        from a_share_research.core.providers.tushare_quotes import TushareMarketDataProvider

        result = TushareMarketDataProvider(
            token=paths.tushare_token()
        ).fetch_trade_calendar(
            (date.today() - timedelta(days=366)).isoformat(),
            date.today().isoformat(),
        )
        frame = _tushare_frame(result, "tushare.trade_cal")
    except Exception as exc:
        logger.warning("TuShare交易日历失败，尝试 AKShare 回退: %s", exc)
        return ()
    if (
        isinstance(frame, tuple)
        or frame is None
        or frame.empty
        or "date" not in frame.columns
    ):
        return ()
    parsed = __import__("pandas").to_datetime(frame["date"], errors="coerce")
    return tuple(item.date() for item in parsed.dropna())


@lru_cache(maxsize=1)
def _fetch_trading_dates() -> tuple[date, ...]:
    """Load the TuShare calendar, falling back to AKShare on provider failure."""
    if DATA_SOURCE == "akshare":
        return _fetch_trading_dates_akshare()
    return _fetch_trading_dates_tushare() or _fetch_trading_dates_akshare()


def _validate_sina_quote(
    quote: QuoteObservation,
    trading_dates: tuple[date, ...],
    now: datetime | None = None,
) -> str | None:
    from a_share_research.core.providers.validated_realtime_quotes import (
        validate_quote_observation,
    )

    return validate_quote_observation(
        quote,
        now or datetime.now(timezone.utc),
        trading_dates,
        max_intraday_age_seconds=120,
    )


def _fetch_realtime_quote(code: str) -> QuoteObservation:
    """Fetch and persist a Sina quote after a trading-calendar check."""
    calendar_result = timed_call(_fetch_trading_dates, timeout=API_TIMEOUT)
    if _is_timed_call_error(calendar_result) or not calendar_result:
        raise RuntimeError("交易日历不可用，无法校验行情新鲜度")
    quote = _fetch_sina_quote(code)
    if quote is None:
        raise RuntimeError("新浪行情返回空数据")
    validation_error = _validate_sina_quote(quote, calendar_result)
    if validation_error:
        raise RuntimeError(f"新浪行情校验失败: {validation_error}")
    store.record_quote_snapshot(
        code,
        quote.price,
        quote.quote_date,
        quote.quote_time,
        quote.source,
        {
            "sina": {
                "price": quote.price,
                "quote_date": quote.quote_date,
                "quote_time": quote.quote_time,
            }
        },
        False,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    return quote


# ─── 结构化数据获取函数（供 timed_call 包装）────────────────────────────────


def _tushare_frame(result: Any, source: str) -> Any:
    """Unwrap a shared-provider result while preserving fetcher error semantics."""
    if (
        getattr(result, "status", None) == "ok"
        and getattr(result, "value", None) is not None
    ):
        return result.value
    message = getattr(result, "error_message", None) or getattr(
        result, "error_code", None
    )
    return ("ERROR", f"{source}: {message or '无数据'}")


def _fetch_info_akshare(code: str) -> dict | None:
    import akshare as ak

    df = ak.stock_individual_info_em(symbol=code, timeout=API_TIMEOUT)
    if df is None or df.empty:
        return None
    return dict(zip(df["item"], df["value"]))


def _fetch_info_tushare(code: str) -> dict | None:
    from a_share_research.core.providers import TushareValuationProvider

    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=30)).isoformat()
    result = TushareValuationProvider(
        token=paths.tushare_token()
    ).fetch_valuation_history(code, start_date, end_date)
    frame = _tushare_frame(result, "tushare.daily_basic")
    if isinstance(frame, tuple) or frame is None or frame.empty:
        return frame
    row = frame.iloc[-1]
    return {
        "总市值": row.get("total_mv"),
        "流通市值": row.get("circ_mv"),
    }


def _fetch_info(code: str) -> dict | None:
    return (
        _fetch_info_akshare(code)
        if DATA_SOURCE == "akshare"
        else _fetch_info_tushare(code)
    )


@lru_cache(maxsize=1)
def _fetch_tushare_industry_map() -> dict | None:
    """读取共享基础库行业映射；进程内缓存，失败时静默返回 None。"""

    def load_map() -> Any:
        from a_share_research.core.providers.tushare_fundamentals import (
            TushareFundamentalsProvider,
        )

        result = TushareFundamentalsProvider(
            token=paths.tushare_token()
        ).fetch_industry_map()

        if getattr(result, "status", None) == "ok" and getattr(result, "value", None):
            return result.value
        return None

    industry_map = timed_call(load_map, timeout=15)
    if _is_timed_call_error(industry_map):
        return None
    return industry_map if industry_map else None


def _fetch_industry_from_lib(code: str) -> str | None:
    """从共享基础库批量行业映射中读取行业；失败时静默返回 None。"""
    industry_map = _fetch_tushare_industry_map()
    if not industry_map:
        return None
    industry = industry_map.get(code)
    if not industry:
        return None
    industry = str(industry).strip()
    return industry or None


def _fetch_financials_akshare(code: str) -> Any:
    import akshare as ak

    return ak.stock_financial_abstract_ths(symbol=code, indicator="按年度")


def _report_period_label(raw: Any) -> str | None:
    import pandas as pd

    try:
        period = pd.to_datetime(raw)
    except Exception:
        return None
    suffix = {3: "Q1", 6: "半年报", 9: "Q3", 12: "年报"}.get(period.month)
    return f"{period.year}{suffix}" if suffix else None


def _deduplicated_report_rows(frame: Any, *, prefer_corrected: bool = False) -> Any:
    import pandas as pd

    if isinstance(frame, tuple) or frame is None or getattr(frame, "empty", True):
        return None
    rows = frame.copy()
    if "end_date" not in rows.columns:
        return None
    rows["_period"] = pd.to_datetime(rows["end_date"], errors="coerce")
    announced = rows.get("f_ann_date", pd.Series(index=rows.index, dtype="object"))
    announced = announced.combine_first(
        rows.get("ann_date", pd.Series(index=rows.index, dtype="object"))
    )
    rows["_announcement"] = pd.to_datetime(announced, errors="coerce")
    update_flag = rows.get("update_flag", pd.Series(index=rows.index, dtype="float64"))
    rows["_update_order"] = pd.to_numeric(update_flag, errors="coerce").fillna(-1)
    order = ["_period", "_announcement", "_update_order"]
    if prefer_corrected:
        rows["_update_order"] = rows["_update_order"].eq(1)
        order = ["_period", "_update_order", "_announcement"]
    rows = rows.dropna(subset=["_period"]).sort_values(order, na_position="first")
    return rows.drop_duplicates("_period", keep="last").reset_index(drop=True)


def _row_at_period(frame: Any, period: Any) -> Any:
    if frame is None or getattr(frame, "empty", True):
        return None
    rows = frame[frame["_period"] == period]
    return rows.iloc[-1] if not rows.empty else None


def _row_number(row: Any, *columns: str) -> float | None:
    if row is None:
        return None
    for column in columns:
        if column in row.index:
            value = parse_float(row[column])
            if value is not None:
                return value
    return None


def _ttm_pair(
    frame: Any, latest_period: Any, *columns: str
) -> tuple[float | None, float | None]:
    """Return current/prior TTM values without annualising an interim period."""
    import pandas as pd

    if frame is None or getattr(frame, "empty", True):
        return None, None
    annual_periods = frame.loc[
        frame["_period"].dt.month.eq(12) & frame["_period"].le(latest_period),
        "_period",
    ]
    if annual_periods.empty:
        return None, None
    annual_period = annual_periods.max()
    current_ytd = _row_number(_row_at_period(frame, latest_period), *columns)
    prior_ytd_period = latest_period - pd.DateOffset(years=1)
    prior_ytd = _row_number(_row_at_period(frame, prior_ytd_period), *columns)
    if latest_period.month == 12:
        return current_ytd, prior_ytd
    annual = _row_number(_row_at_period(frame, annual_period), *columns)
    prior_annual = _row_number(
        _row_at_period(frame, annual_period - pd.DateOffset(years=1)), *columns
    )
    prior_prior_ytd = _row_number(
        _row_at_period(frame, latest_period - pd.DateOffset(years=2)), *columns
    )
    current_ttm = (
        annual + current_ytd - prior_ytd
        if None not in (annual, current_ytd, prior_ytd)
        else None
    )
    prior_ttm = (
        prior_annual + prior_ytd - prior_prior_ytd
        if None not in (prior_annual, prior_ytd, prior_prior_ytd)
        else None
    )
    return current_ttm, prior_ttm


def _announcement_date(*rows: Any) -> str | None:
    import pandas as pd

    values = [
        row["_announcement"]
        for row in rows
        if row is not None and pd.notna(row.get("_announcement"))
    ]
    return max(values).date().isoformat() if values else None


def _build_latest_report_snapshot(
    indicator: Any,
    income: Any,
    balance: Any,
    *,
    annual_period: Any,
) -> dict[str, Any] | None:
    import math
    import pandas as pd

    frames = {
        "indicator": _deduplicated_report_rows(indicator),
        "income": _deduplicated_report_rows(income),
        "balance": _deduplicated_report_rows(balance),
    }
    periods = [
        frame["_period"].max()
        for frame in frames.values()
        if frame is not None and not frame.empty
    ]
    if not periods:
        return None
    latest_period = max(periods)
    previous_period = latest_period - pd.DateOffset(years=1)

    def row_at(name: str, period: Any) -> Any:
        frame = frames[name]
        if frame is None:
            return None
        rows = frame[frame["_period"] == period]
        return rows.iloc[-1] if not rows.empty else None

    latest = {name: row_at(name, latest_period) for name in frames}
    previous = {name: row_at(name, previous_period) for name in frames}
    net_profit_ttm, prior_net_profit_ttm = _ttm_pair(
        frames["income"], latest_period, "n_income_attr_p"
    )
    revenue_ttm, prior_revenue_ttm = _ttm_pair(
        frames["income"], latest_period, "total_revenue", "revenue"
    )

    def value(row: Any, *columns: str) -> float | None:
        if row is None:
            return None
        for column in columns:
            if column in row.index:
                parsed = parse_float(row[column])
                if parsed is not None and math.isfinite(parsed):
                    return parsed
        return None

    def direction(
        current: float | None, prior: float | None = None, *, yoy: bool = False
    ) -> str:
        compared = (
            current
            if yoy
            else (None if current is None or prior is None else current - prior)
        )
        if compared is None:
            return "missing"
        if math.isclose(compared, 0.0, abs_tol=1e-12):
            return "flat"
        return "up" if compared > 0 else "down"

    def field(
        current: float | None, prior: float | None = None, *, yoy: bool = False
    ) -> dict[str, Any]:
        return {
            "value": current,
            "status": "ok" if current is not None else "missing",
            "direction": direction(current, prior, yoy=yoy),
        }

    indicator_row = latest["indicator"]
    income_row = latest["income"]
    balance_row = latest["balance"]
    prior_income = previous["income"]
    prior_balance = previous["balance"]
    announcements = [
        row["_announcement"]
        for row in latest.values()
        if row is not None and pd.notna(row["_announcement"])
    ]
    annual_dt = pd.to_datetime(annual_period, errors="coerce")
    report_label = _report_period_label(latest_period)
    return {
        "report_period": report_label,
        "announcement_date": max(announcements).date().isoformat()
        if announcements
        else None,
        "source": "tushare.fina_indicator+income+balancesheet",
        "is_newer_than_annual": bool(pd.notna(annual_dt) and latest_period > annual_dt),
        "fields": {
            "revenue": field(
                value(income_row, "total_revenue", "revenue"),
                value(prior_income, "total_revenue", "revenue"),
            ),
            "revenue_yoy": field(value(indicator_row, "or_yoy"), yoy=True),
            "net_profit_parent": field(
                value(income_row, "n_income_attr_p"),
                value(prior_income, "n_income_attr_p"),
            ),
            "net_profit_yoy": field(value(indicator_row, "netprofit_yoy"), yoy=True),
            "net_profit_ttm": field(net_profit_ttm, prior_net_profit_ttm),
            "net_profit_ttm_yoy": field(
                None
                if net_profit_ttm is None
                or prior_net_profit_ttm is None
                or prior_net_profit_ttm <= 0
                else (net_profit_ttm / prior_net_profit_ttm - 1) * 100,
                yoy=True,
            ),
            "revenue_ttm": field(revenue_ttm, prior_revenue_ttm),
            "deducted_net_profit_yoy": field(
                value(indicator_row, "dt_netprofit_yoy"), yoy=True
            ),
            "roe": field(value(indicator_row, "roe_waa")),
            "bps": field(value(indicator_row, "bps")),
            "equity_parent": field(
                value(balance_row, "total_hldr_eqy_exc_min_int"),
                value(prior_balance, "total_hldr_eqy_exc_min_int"),
            ),
        },
    }


def _build_risk_gate_inputs(
    indicator: Any, income: Any, balance: Any, cashflow: Any
) -> dict[str, dict[str, Any]]:
    """Build P1/P2 inputs from the already-fetched company statement history."""
    import pandas as pd

    indicator_rows = _deduplicated_report_rows(indicator)
    income_rows = _deduplicated_report_rows(income)
    balance_rows = _deduplicated_report_rows(balance)
    cashflow_rows = _deduplicated_report_rows(cashflow)
    result: dict[str, dict[str, Any]] = {}

    if income_rows is not None and balance_rows is not None:
        common = set(income_rows["_period"]) & set(balance_rows["_period"])
        if common:
            latest = max(common)
            prior = latest - pd.DateOffset(years=1)
            latest_income = _row_at_period(income_rows, latest)
            latest_balance = _row_at_period(balance_rows, latest)
            prior_balance = _row_at_period(balance_rows, prior)
            net_profit, _ = _ttm_pair(income_rows, latest, "n_income_attr_p")
            revenue, _ = _ttm_pair(income_rows, latest, "total_revenue", "revenue")
            equity_values = (
                _row_number(latest_balance, "total_hldr_eqy_exc_min_int"),
                _row_number(prior_balance, "total_hldr_eqy_exc_min_int"),
            )
            asset_values = (
                _row_number(latest_balance, "total_assets"),
                _row_number(prior_balance, "total_assets"),
            )
            average_equity = (
                sum(equity_values) / 2 if None not in equity_values else None
            )
            average_assets = sum(asset_values) / 2 if None not in asset_values else None
            annual_roe: list[float] = []
            annual_periods: list[Any] = []
            if indicator_rows is not None:
                annual = (
                    indicator_rows[
                        indicator_rows["_period"].dt.month.eq(12)
                        & indicator_rows["_period"].le(latest)
                    ]
                    .sort_values("_period")
                    .tail(5)
                )
                annual_periods = list(annual["_period"])
                annual_roe = [
                    value
                    for value in (
                        _row_number(row, "roe_waa") for _, row in annual.iterrows()
                    )
                    if value is not None
                ]
            current_dupont = {
                "net_margin": net_profit / revenue
                if net_profit is not None and revenue not in (None, 0)
                else None,
                "asset_turnover": revenue / average_assets
                if revenue is not None and average_assets not in (None, 0)
                else None,
                "equity_multiplier": average_assets / average_equity
                if average_assets is not None and average_equity not in (None, 0)
                else None,
            }
            dupont_history = {
                "net_margin": [],
                "asset_turnover": [],
                "equity_multiplier": [],
            }
            for period in annual_periods:
                income_row = _row_at_period(income_rows, period)
                balance_row = _row_at_period(balance_rows, period)
                previous_balance = _row_at_period(
                    balance_rows, period - pd.DateOffset(years=1)
                )
                annual_profit = _row_number(income_row, "n_income_attr_p")
                annual_revenue = _row_number(income_row, "total_revenue", "revenue")
                annual_equity = (
                    _row_number(balance_row, "total_hldr_eqy_exc_min_int"),
                    _row_number(previous_balance, "total_hldr_eqy_exc_min_int"),
                )
                annual_assets = (
                    _row_number(balance_row, "total_assets"),
                    _row_number(previous_balance, "total_assets"),
                )
                if (
                    annual_profit is None
                    or annual_revenue in (None, 0)
                    or None in annual_equity
                    or None in annual_assets
                ):
                    dupont_history = {}
                    break
                mean_equity = sum(annual_equity) / 2
                mean_assets = sum(annual_assets) / 2
                dupont_history["net_margin"].append(annual_profit / annual_revenue)
                dupont_history["asset_turnover"].append(annual_revenue / mean_assets)
                dupont_history["equity_multiplier"].append(mean_assets / mean_equity)
            result["roe_structural_gate"] = {
                "latest_roe_ttm": net_profit / average_equity * 100
                if net_profit is not None and average_equity not in (None, 0)
                else None,
                "roe_5y_values": annual_roe,
                "dupont_current": current_dupont,
                "dupont_5y": dupont_history,
                "sources": [
                    "tushare.fina_indicator (company-filed statement mirror)",
                    "tushare.income (company-filed statement mirror)",
                    "tushare.balancesheet (company-filed statement mirror)",
                ],
                "as_of": _announcement_date(latest_income, latest_balance),
                "report_period": _report_period_label(latest),
            }

    if cashflow_rows is not None and not cashflow_rows.empty:
        latest = cashflow_rows["_period"].max()
        latest_row = _row_at_period(cashflow_rows, latest)
        cfo, _ = _ttm_pair(cashflow_rows, latest, "n_cashflow_act")
        capex, _ = _ttm_pair(cashflow_rows, latest, "c_pay_acq_const_fiolta")
        result["cash_flow_gate"] = {
            "ttm_cfo": cfo,
            "ttm_capex": capex,
            "sources": ["tushare.cashflow (company-filed statement mirror)"],
            "as_of": _announcement_date(latest_row),
            "report_period": _report_period_label(latest),
        }
    return result


def _fetch_financials_tushare(code: str) -> Any:
    import pandas as pd
    from a_share_research.core.providers import TushareFinancialProvider

    provider = TushareFinancialProvider(token=paths.tushare_token())

    def fetch_frame(method_name: str, source: str) -> Any:
        method = getattr(provider, method_name, None)
        if method is None:
            return ("ERROR", f"{source}: provider不支持")
        try:
            return _tushare_frame(method(code), source)
        except Exception as exc:
            return ("ERROR", f"{source}: {exc}")

    def annual_frame(frame: Any) -> Any:
        if isinstance(frame, tuple) or frame is None or frame.empty:
            return frame
        value = frame.copy()
        if "end_date" in value.columns:
            period = pd.to_datetime(value["end_date"], errors="coerce")
            value = value[period.dt.month.eq(12)].copy()
        return value.rename(columns={"end_date": "报告期"}).drop_duplicates(
            "报告期", keep="last"
        )

    indicator = fetch_frame("fetch_indicator_history", "tushare.fina_indicator")
    if isinstance(indicator, tuple) or indicator is None:
        return indicator
    income = fetch_frame("fetch_income_history", "tushare.income")
    balance = fetch_frame("fetch_balance_history", "tushare.balancesheet")
    cashflow = fetch_frame("fetch_cashflow_history", "tushare.cashflow")

    frame = indicator.copy()
    period = pd.to_datetime(frame.get("end_date"), errors="coerce")
    annual = frame[period.dt.month.eq(12)].copy() if "end_date" in frame else frame
    if annual.empty:
        annual = frame
    annual = annual.rename(
        columns={
            "end_date": "报告期",
            "roe_waa": "净资产收益率",
            "netprofit_yoy": "净利润同比增长率",
            "debt_to_assets": "资产负债率",
            "grossprofit_margin": "销售毛利率",
            "bps": "每股净资产",
        }
    )

    annual_income = annual_frame(income)
    if (
        not isinstance(annual_income, tuple)
        and annual_income is not None
        and not annual_income.empty
    ):
        columns = ["报告期"]
        for candidate, target in (
            ("basic_eps", "基本每股收益"),
            ("eps", "基本每股收益"),
            ("total_revenue", "营业总收入"),
            ("revenue", "营业总收入"),
        ):
            if (
                candidate in annual_income.columns
                and target not in annual_income.columns
            ):
                annual_income = annual_income.rename(columns={candidate: target})
            if target in annual_income.columns and target not in columns:
                columns.append(target)
        if len(columns) > 1:
            annual_income = annual_income[columns].drop_duplicates(
                "报告期", keep="last"
            )
            annual = annual.merge(annual_income, on="报告期", how="left")
            if "营业总收入" in annual.columns:
                revenue = pd.to_numeric(annual["营业总收入"], errors="coerce")
                annual["营业总收入同比增长率"] = revenue.pct_change() * 100

    annual_balance = annual_frame(balance)
    if (
        not isinstance(annual_balance, tuple)
        and annual_balance is not None
        and not annual_balance.empty
    ):
        debt_fields = [
            f
            for f in (
                "st_borr",
                "lt_borr",
                "bond_payable",
                "non_cur_liab_due_1y",
                "total_liab",
            )
            if f in annual_balance.columns
        ]
        fields = [
            field
            for field in (
                "报告期",
                "total_cur_assets",
                "total_cur_liab",
                "total_share",
                *debt_fields,
            )
            if field in annual_balance.columns
        ]
        if len(fields) > 1:
            annual = annual.merge(annual_balance[fields], on="报告期", how="left")
            if {"total_cur_assets", "total_cur_liab"} <= set(annual.columns):
                assets = pd.to_numeric(annual["total_cur_assets"], errors="coerce")
                liabilities = pd.to_numeric(annual["total_cur_liab"], errors="coerce")
                annual["流动比率"] = assets.div(liabilities.where(liabilities != 0))
            if "total_liab" in annual.columns:
                debt = annual.reindex(
                    columns=[
                        "st_borr",
                        "lt_borr",
                        "bond_payable",
                        "non_cur_liab_due_1y",
                    ]
                ).apply(pd.to_numeric, errors="coerce")
                # 缺列/缺值不能证明债务为零；四个分项须全部有效。
                debt = debt.where((debt >= 0) & (debt < float("inf")))
                tot_liab = pd.to_numeric(annual["total_liab"], errors="coerce")
                interest_debt = debt.sum(axis=1, min_count=4)
                annual["有息负债率"] = (
                    interest_debt.div(
                        tot_liab.where((tot_liab > 0) & (tot_liab < float("inf")))
                    )
                    * 100
                )

    annual_cashflow = annual_frame(cashflow)
    if (
        not isinstance(annual_cashflow, tuple)
        and annual_cashflow is not None
        and not annual_cashflow.empty
        and "n_cashflow_act" in annual_cashflow.columns
        and "total_share" in annual.columns
    ):
        annual = annual.merge(
            annual_cashflow[["报告期", "n_cashflow_act"]], on="报告期", how="left"
        )
        cashflow_total = pd.to_numeric(annual["n_cashflow_act"], errors="coerce")
        total_share = pd.to_numeric(annual["total_share"], errors="coerce")
        annual["每股经营现金流"] = cashflow_total.div(
            total_share.where(total_share != 0)
        )

    annual = annual.sort_values("报告期").reset_index(drop=True)
    annual_period = annual["报告期"].iloc[-1] if not annual.empty else None
    annual.attrs["latest_report_snapshot"] = _build_latest_report_snapshot(
        indicator, income, balance, annual_period=annual_period
    )
    annual.attrs["risk_gate_inputs"] = _build_risk_gate_inputs(
        indicator, income, balance, cashflow
    )
    return annual


def _fetch_financials(code: str) -> Any:
    return (
        _fetch_financials_akshare(code)
        if DATA_SOURCE == "akshare"
        else _fetch_financials_tushare(code)
    )


def _fetch_dividends_akshare(code: str) -> Any:
    import akshare as ak

    return ak.stock_fhps_detail_em(symbol=code)


def _fetch_dividends_tushare(code: str) -> Any:
    from a_share_research.core.providers import TushareDividendProvider

    result = TushareDividendProvider(
        token=paths.tushare_token()
    ).fetch_dividend_history(code)
    frame = _tushare_frame(result, "tushare.dividend")
    if isinstance(frame, tuple) or frame is None:
        return frame
    frame = frame.rename(
        columns={
            "end_date": "报告期",
            "ex_date": "除权除息日",
            "div_proc": "方案进度",
            "stk_div": "送转股份-送转总比例",
        }
    ).copy()
    # TuShare reports per-share figures (cash_div 0.56 = 0.56 yuan/share,
    # stk_co_rate 0.2 = 0.2 bonus share/share) while every downstream consumer
    # -- _compute_dividend_basis, compute_dps_ttm, detect_split_ratio -- expects
    # the AKShare per-10-share convention and divides by 10. Normalise here, at
    # the adapter, so those three stay correct for both sources.
    if {"stk_bo_rate", "stk_co_rate"} <= set(frame.columns):
        split = frame[["stk_bo_rate", "stk_co_rate"]].fillna(0).sum(axis=1)
        if "送转股份-送转总比例" in frame.columns:
            split = split.where(split != 0, frame["送转股份-送转总比例"])
        frame["送转股份-送转总比例"] = split * 10
    elif "送转股份-送转总比例" in frame.columns:
        frame["送转股份-送转总比例"] = frame["送转股份-送转总比例"] * 10
    if "现金分红-现金分红比例" not in frame.columns and "cash_div" in frame.columns:
        frame["现金分红-现金分红比例"] = frame["cash_div"] * 10
    if "方案进度" in frame.columns:
        frame["方案进度"] = frame["方案进度"].replace({"实施": "实施分配"})
    return frame


def _fetch_dividends(code: str) -> Any:
    return (
        _fetch_dividends_akshare(code)
        if DATA_SOURCE == "akshare"
        else _fetch_dividends_tushare(code)
    )


def _fetch_price_history_akshare(code: str) -> Any:
    """通达信接口（stock_zh_a_daily），返回列名归一化为 '日期'/'收盘'。"""
    import akshare as ak

    df = ak.stock_zh_a_daily(symbol=f"{_market_prefix(code)}{code}", adjust="")
    return df.rename(columns={"date": "日期", "close": "收盘"})


def _fetch_price_history_tushare(code: str) -> Any:
    from a_share_research.core.providers import TushareValuationProvider

    result = TushareValuationProvider(
        token=paths.tushare_token()
    ).fetch_valuation_history(
        code,
        (date.today() - timedelta(days=3665)).isoformat(),
        date.today().isoformat(),
    )
    frame = _tushare_frame(result, "tushare.daily_basic")
    if isinstance(frame, tuple) or frame is None:
        return frame
    frame = frame.rename(columns={"trade_date": "日期", "close": "收盘"})
    columns = ["日期", "收盘"]
    if "ps_ttm" in frame.columns:
        columns.append("ps_ttm")
    return frame[columns]


def _fetch_price_history(code: str) -> Any:
    return (
        _fetch_price_history_akshare(code)
        if DATA_SOURCE == "akshare"
        else _fetch_price_history_tushare(code)
    )


def _fetch_bond_yield_api(start_date: str, end_date: str) -> Any:
    import akshare as ak

    return ak.bond_china_yield(start_date=start_date, end_date=end_date)


# ─── 计算辅助函数 ────────────────────────────────────────────────────────────


def _compute_dividend_basis(
    div_df: Any, current_price: float | None
) -> tuple[float | None, float | None, str | None, int | None, list[str]]:
    """从分红历史（按报告期）估算股息率与每股分红（DPS）。

    div_df 来自 ak.stock_fhps_detail_em，含'报告期'（如2025-12-31）/
    '现金分红-现金分红比例'（元/10股，需除以10换算每股）/'方案进度'。

    按'报告期'所属自然年分组求和，不用滚动天数窗口——A股中期+年度分红
    分两次实施，年报分红从预案到实施常跨年，若按"近366天已实施记录"滚动
    求和，会在年度分红还是"预案"时漏掉它，转而误抓上一财年已实施的年度
    分红与本财年中期分红叠加，凑出跨财年的错误总和（同向偏高约50%-65%，
    实测案例：招行3.013≠正确2.016、神华3.24≠正确2.01、宝钢一度被误判为
    0.18，实际中期0.12+年度0.18=0.30才对）。按报告期年份分组天然避免跨年
    叠加，且允许把还在"董事会决议通过"/"预案"阶段但已有具体金额的年度
    分红计入（forward-looking，与历史人工核实口径一致）。

    取有现金分红金额的最新完整财年（存在正值12-31记录的最大年份），
    汇总该年全部中期与年度分红记录。这样可避免中期分红披露后、年度分红
    尚未披露时，当前未完成财年的分红季节性地被截断。若没有任何正值12-31
    记录，则回退到最新报告期所在自然年。
    若最新报告期距离选中的完整财年已相隔至少两年，可能意味着公司跳过了
    年度分红；此时 fail-closed 返回空值，拒绝用陈旧年度分红伪装当前股息率。
    若最新报告期已超过约13个月（公司可能停止分红），返回 (None, None, 原因)。
    返回股息率、DPS、原因及实际选用的年份与报告期。
    """
    import pandas as pd

    if div_df is None or div_df.empty or not current_price or current_price <= 0:
        return None, None, "无分红数据或价格不可用", None, []
    if "报告期" not in div_df.columns:
        return None, None, "分红数据缺少报告期字段", None, []

    df = div_df.copy()
    amount_col = (
        "现金分红-现金分红比例" if "现金分红-现金分红比例" in df.columns else "派息"
    )
    if amount_col not in df.columns:
        return None, None, "分红数据缺少现金分红金额字段", None, []
    df["_amount"] = pd.to_numeric(df[amount_col], errors="coerce")
    df["_period"] = pd.to_datetime(df["报告期"], errors="coerce")
    df = df.dropna(subset=["_period"])
    valid = df[df["_amount"].fillna(0) > 0]
    if valid.empty:
        return None, None, "无有效分红金额记录", None, []

    cutoff = datetime.now() - timedelta(days=400)
    latest_period = valid["_period"].max()
    if latest_period < cutoff:
        return (
            None,
            None,
            f"超过12个月无分红（最近报告期：{latest_period.strftime('%Y-%m-%d')}）",
            None,
            [],
        )

    complete_years = valid.loc[
        (valid["_period"].dt.month == 12) & (valid["_period"].dt.day == 31),
        "_period",
    ].dt.year
    target_year = (
        int(complete_years.max()) if not complete_years.empty else latest_period.year
    )
    if latest_period.year - target_year >= 2:
        return (
            None,
            None,
            (
                f"最近完整财年({target_year})距最新报告期({latest_period.strftime('%Y-%m-%d')})超过1年，"
                f"可能已停发年度分红，拒绝返回可能陈旧的股息率"
            ),
            None,
            [],
        )
    selected = valid[valid["_period"].dt.year == target_year]
    total_per_10 = selected["_amount"].sum()
    if total_per_10 <= 0:
        return None, None, "派息为0", None, []
    per_share = round(float(total_per_10) / 10, 4)
    yield_pct = round(per_share / float(current_price) * 100, 2)
    periods = sorted(set(selected["_period"].dt.date.astype(str)))
    return yield_pct, per_share, None, target_year, periods


def _compute_dividends(
    div_df: Any, current_price: float | None, latest_report_year: int | None
) -> dict:
    """Shared fetch/quarter dividend basis, including fetch's whole-year adjustment."""
    split_ratio, split_ex_date = 0.0, None
    if isinstance(div_df, str):
        reason = "分红API超时"
    elif div_df is None or isinstance(div_df, tuple):
        reason = "无分红数据"
    else:
        reason = None
        split_ratio, split_ex_date = _detect_split_ratio(div_df, latest_report_year)
    dy = dps = dps_ttm = year = None
    periods = []
    if reason is None:
        dy, dps, reason, year, periods = _compute_dividend_basis(div_df, current_price)
        dps_ttm = compute_dps_ttm(div_df)
    ttm_reason = (
        reason
        if div_df is None or isinstance(div_df, (str, tuple))
        else ("无有效TTM分红金额记录" if dps_ttm is None else None)
    )
    if split_ratio > 0:
        dps = round(dps / (1 + split_ratio), 4) if dps is not None else None
        dps_ttm = round(dps_ttm / (1 + split_ratio), 4) if dps_ttm is not None else None
        dy = round(dy / (1 + split_ratio), 2) if dy is not None else None
    return {
        "dividend_yield": dy,
        "dps": dps,
        "dps_ttm": dps_ttm,
        "reason": reason,
        "ttm_reason": ttm_reason,
        "year": year,
        "periods": periods,
        "split_ratio": split_ratio,
        "split_ex_date": split_ex_date,
    }


def compute_dps_ttm(div_df: Any) -> float | None:
    """计算报告期口径的近12个月每股分红，供人工判断参考，不用于自动评分。

    该值具有季节性，故与主字段 dps 的完整财年口径区分。
    """
    import pandas as pd

    if div_df is None or div_df.empty:
        return None
    if "报告期" not in div_df.columns:
        return None

    amount_col = (
        "现金分红-现金分红比例" if "现金分红-现金分红比例" in div_df.columns else "派息"
    )
    if amount_col not in div_df.columns:
        return None
    df = div_df.copy()
    df["_amount"] = pd.to_numeric(df[amount_col], errors="coerce")
    df["_period"] = pd.to_datetime(df["报告期"], errors="coerce")
    valid = df.dropna(subset=["_period"])
    valid = valid[valid["_amount"].fillna(0) > 0]
    if valid.empty:
        return None

    latest_period = valid["_period"].max()
    window_start = latest_period - timedelta(days=365)
    total_per_10 = valid[
        (valid["_period"] > window_start) & (valid["_period"] <= latest_period)
    ]["_amount"].sum()
    if total_per_10 <= 0:
        return None
    return round(float(total_per_10) / 10, 4)


def _load_price_df(code: str, years: int = 10) -> Any:
    """获取并预处理估值历史，供 PE/PB/PS 分位计算复用。

    返回含 'date'(datetime64[us]) 和 '收盘' 列的 DataFrame，
    已按 years 起始日过滤；失败时返回 'API_ERROR' 字符串。
    """
    import pandas as pd

    start_dt = (date.today() - timedelta(days=years * 366)).strftime("%Y%m%d")
    raw = timed_call_with_retry(_fetch_price_history, code, timeout=PE_TIMEOUT)
    if isinstance(raw, (str, tuple)) or raw is None:
        return "API_ERROR"
    if hasattr(raw, "empty") and raw.empty:
        return "API_ERROR"
    columns = ["日期", "收盘"] + (["ps_ttm"] if "ps_ttm" in raw.columns else [])
    df = raw[columns].copy()
    df["date"] = pd.to_datetime(df["日期"], errors="coerce").astype("datetime64[us]")
    df = df.dropna(subset=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    cutoff = pd.Timestamp(start_dt).to_datetime64().astype("datetime64[us]")
    return df[df["date"] >= cutoff].reset_index(drop=True)


def compute_price_change_5d(
    price_df: Any,
) -> tuple[float | None, str | None, str | None]:
    """Return the latest close change versus five valid trading observations ago."""
    import math
    import pandas as pd

    if (
        price_df is None
        or isinstance(price_df, (str, tuple))
        or getattr(price_df, "empty", True)
        or not {"date", "收盘"} <= set(price_df.columns)
    ):
        return None, None, "历史价格不可用"

    frame = price_df[["date", "收盘"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["收盘"] = pd.to_numeric(frame["收盘"], errors="coerce")
    frame = frame.dropna(subset=["date", "收盘"])
    frame = frame[
        frame["收盘"].map(
            lambda value: math.isfinite(float(value)) and float(value) > 0
        )
    ]
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    if len(frame) < 6:
        return None, None, "有效历史价格少于6个交易观察"

    start = float(frame.iloc[-6]["收盘"])
    latest = float(frame.iloc[-1]["收盘"])
    as_of = frame.iloc[-1]["date"].date().isoformat()
    return round((latest / start - 1) * 100, 2), as_of, None


def compute_ps_ttm_percentile_5y(
    valuation_df: Any,
) -> tuple[float | None, float | None, dict[str, Any]]:
    """Return latest PS_TTM and its strict-less rank over 60 month-end values."""
    import math
    import pandas as pd

    metadata: dict[str, Any] = {
        "sample_start": None,
        "sample_end": None,
        "valid_months": 0,
        "basis": "ps_ttm",
        "source": "tushare.daily_basic",
    }
    if (
        valuation_df is None
        or isinstance(valuation_df, (str, tuple))
        or getattr(valuation_df, "empty", True)
        or not {"date", "ps_ttm"} <= set(valuation_df.columns)
    ):
        return None, None, metadata

    frame = valuation_df[["date", "ps_ttm"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["ps_ttm"] = pd.to_numeric(frame["ps_ttm"], errors="coerce")
    frame = frame.dropna(subset=["date", "ps_ttm"])
    frame = frame[
        frame["ps_ttm"].map(
            lambda value: math.isfinite(float(value)) and float(value) > 0
        )
    ].sort_values("date")
    if frame.empty:
        return None, None, metadata

    frame["month"] = frame["date"].dt.to_period("M")
    latest_month = frame["month"].max()
    first_month = latest_month - 59
    monthly = (
        frame[frame["month"] >= first_month].groupby("month", as_index=False).tail(1)
    )
    monthly = monthly.sort_values("date").reset_index(drop=True)
    metadata.update(
        {
            "sample_start": monthly["date"].iloc[0].date().isoformat()
            if not monthly.empty
            else None,
            "sample_end": monthly["date"].iloc[-1].date().isoformat()
            if not monthly.empty
            else None,
            "valid_months": len(monthly),
        }
    )
    if len(monthly) < 60:
        return None, None, metadata

    current = float(monthly["ps_ttm"].iloc[-1])
    percentile = round(float((monthly["ps_ttm"] < current).mean() * 100), 1)
    return current, percentile, metadata


def _period_year(raw: Any) -> int | None:
    """Report-period column carries a bare year on AKShare ("2024") but an ISO
    date on TuShare ("2024-12-31"); accept both."""
    text = str(raw).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) == 4:
        return int(text)
    try:
        import pandas as pd

        return int(pd.to_datetime(raw).year)
    except Exception:
        return None


def compute_pe_percentile(
    code: str,
    current_pe: float | None,
    fin_df: Any,
    years: int = 10,
    price_df: Any = None,
    split_ratio: float = 0.0,
    split_ex_date: str | None = None,
) -> float | str | None:
    """构造历史 PE 时序，计算当前 PE 处于过去 N 年的百分位。

    look-ahead bias 修复：年报 EPS 在 yr+1-05-01 才公开，merge_asof backward-fill
    确保每个交易日只使用已披露数据。

    price_df 可由调用方预先通过 _load_price_df 获取并传入（PE/PB 共用时避免重复拉取）。
    """
    import pandas as pd

    if not current_pe or current_pe <= 0:
        return None
    if price_df is None:
        price_df = _load_price_df(code, years)
    if isinstance(price_df, str) or price_df is None:
        return "API_ERROR"
    price_df = _trim_price_window(price_df, years)

    eps_records = []
    for _, row in fin_df.iterrows():
        yr = _period_year(row["报告期"])
        eps = parse_float(row["基本每股收益"])
        if yr and eps and eps > 0:
            eps_records.append({"date": pd.Timestamp(f"{yr + 1}-05-01"), "eps": eps})
    if not eps_records:
        return None

    eps_df = pd.DataFrame(eps_records).sort_values("date").reset_index(drop=True)
    eps_df["date"] = eps_df["date"].astype("datetime64[us]")
    merged = pd.merge_asof(price_df, eps_df, on="date", direction="backward")
    if split_ratio > 0 and split_ex_date:
        split_dt = pd.Timestamp(split_ex_date).to_datetime64().astype("datetime64[us]")
        mask = merged["date"] >= split_dt
        merged.loc[mask, "eps"] = merged.loc[mask, "eps"] / (1.0 + split_ratio)
    valid_pe = merged["收盘"].div(merged["eps"]).dropna()
    valid_pe = valid_pe[(valid_pe > 0) & (valid_pe < 300)]

    if len(valid_pe) < 100:
        return None
    return round(float((valid_pe < current_pe).mean() * 100), 1)


def compute_pb_percentile(
    code: str,
    current_pb: float | None,
    fin_df: Any,
    years: int = 10,
    price_df: Any = None,
    split_ratio: float = 0.0,
    split_ex_date: str | None = None,
) -> float | str | None:
    """构造历史 PB 时序，计算当前 PB 在过去 N 年的百分位。

    与 compute_pe_percentile 同思路，look-ahead bias 修复相同。
    price_df 可由调用方预传入，避免重复拉取价格历史。
    """
    import pandas as pd

    if not current_pb or current_pb <= 0:
        return None
    if price_df is None:
        price_df = _load_price_df(code, years)
    if isinstance(price_df, str) or price_df is None:
        return "API_ERROR"
    price_df = _trim_price_window(price_df, years)

    bps_records = []
    for _, row in fin_df.iterrows():
        yr = _period_year(row["报告期"])
        bps = parse_float(row["每股净资产"])
        if yr and bps and bps > 0:
            bps_records.append({"date": pd.Timestamp(f"{yr + 1}-05-01"), "bps": bps})
    if not bps_records:
        return None

    bps_df = pd.DataFrame(bps_records).sort_values("date").reset_index(drop=True)
    bps_df["date"] = bps_df["date"].astype("datetime64[us]")
    merged = pd.merge_asof(price_df, bps_df, on="date", direction="backward")
    if split_ratio > 0 and split_ex_date:
        split_dt = pd.Timestamp(split_ex_date).to_datetime64().astype("datetime64[us]")
        mask = merged["date"] >= split_dt
        merged.loc[mask, "bps"] = merged.loc[mask, "bps"] / (1.0 + split_ratio)
    valid_pb = merged["收盘"].div(merged["bps"]).dropna()
    valid_pb = valid_pb[(valid_pb > 0) & (valid_pb < 30)]

    if len(valid_pb) < 100:
        return None
    return round(float((valid_pb < current_pb).mean() * 100), 1)


def _trim_price_window(price_df: Any, years: int) -> Any:
    """Trim caller-supplied history to the requested trailing window."""
    import pandas as pd

    if getattr(price_df, "empty", True) or "date" not in price_df.columns:
        return price_df
    cutoff = price_df["date"].max() - pd.DateOffset(years=years)
    return price_df[price_df["date"] >= cutoff].reset_index(drop=True)


# ─── 字段提取纯函数（供 cmd_fetch 调用，也是单元测试入口）──────────────────


def _extract_fin_fields(fin_df: Any) -> dict:
    """从 stock_financial_abstract_ths 年度 DataFrame 提取结构化字段。

    返回 dict，值为 float 或 None。
    'bps' 仅供调用方打印日志，不应写入 results。
    其余所有键均对应 FIELDS 注册表中的 structured 字段。
    """

    def col(name: str) -> Any:
        return fin_df[name] if name in fin_df.columns else None

    def col_last(name: str) -> float | None:
        if fin_df.empty or name not in fin_df.columns:
            return None
        return parse_float(fin_df[name].iloc[-1])

    return {
        "roe_3y_avg": avg_of(col("净资产收益率"), 3),
        "net_profit_growth": avg_of(col("净利润同比增长率"), 3),
        "debt_ratio": col_last("资产负债率"),
        "eps": col_last("基本每股收益"),
        "bps": col_last("每股净资产"),
        "gross_margin": col_last("销售毛利率"),
        "revenue_growth_3y": avg_of(col("营业总收入同比增长率"), 3),
        "current_ratio": col_last("流动比率"),
        "operating_cf_per_share": col_last("每股经营现金流"),
        "interest_bearing_to_total_debt": col_last("有息负债率"),
    }


_FIN_FIELDS_KEYS = (
    "roe_3y_avg",
    "net_profit_growth",
    "debt_ratio",
    "eps",
    "bps",
    "gross_margin",
    "revenue_growth_3y",
    "current_ratio",
    "operating_cf_per_share",
    "interest_bearing_to_total_debt",
)


def _extract_data_period(fin_df: Any) -> str | None:
    """Derive the latest report period from the financial statement column."""
    if (
        fin_df is None
        or isinstance(fin_df, (str, tuple))
        or getattr(fin_df, "empty", True)
    ):
        return None
    if "报告期" not in fin_df.columns:
        return None
    raw = fin_df["报告期"].iloc[-1]
    text = str(raw).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) == 4:
        return f"{text}年报"
    if text.endswith(("年报", "半年报")) or __import__("re").fullmatch(
        r"\d{4}Q[1-3]", text
    ):
        return text
    try:
        parsed = __import__("pandas").to_datetime(raw)
    except Exception:
        return None
    suffix = {3: "Q1", 6: "半年报", 9: "Q3", 12: "年报"}.get(parsed.month)
    return f"{parsed.year}{suffix}" if suffix else None


def _lookup_cached_industry(code: str) -> str | None:
    """查stock_fundamentals表里该代码历史缓存的industry值，忽略TTL过期——
    新浪行情本身不返回行业字段，复用哪怕过期的历史值好过显示"未知"。"""
    try:
        import sqlite3

        with db.read_only_db_session() as conn:
            row = conn.execute(
                "SELECT industry FROM stock_fundamentals "
                "WHERE code=? AND industry IS NOT NULL AND industry NOT LIKE '未知%'",
                (code,),
            ).fetchone()
        return row[0] if row else None
    except (OSError, sqlite3.Error):
        return None


def _lookup_cached_name(code: str) -> str | None:
    """Return the last cached stock name without applying fundamentals TTL."""
    try:
        import sqlite3

        with db.read_only_db_session() as conn:
            row = conn.execute(
                "SELECT name FROM stock_fundamentals WHERE code=?", (code,)
            ).fetchone()
        return row[0] if row and row[0] else None
    except (OSError, sqlite3.Error):
        return None


def _fetch_spot_data(
    code: str, results: dict, null_reasons: dict
) -> tuple[str, str, float | None]:
    """Step 1: fail-closed quote plus independent descriptive stock info."""
    logger.info("  [1/7] 基本信息（名称/行业/价格/流通比）...")
    try:
        quote = _fetch_realtime_quote(code)
    except Exception as exc:
        logger.error("  ❌ 新浪行情获取失败: %s", exc)
        raise SystemExit(1) from exc
    info = timed_call(_fetch_info, code, timeout=15)
    lib_industry = _fetch_industry_from_lib(code) if DATA_SOURCE != "akshare" else None
    if info == "TIMEOUT" or not info or isinstance(info, tuple):
        err = (
            info[1]
            if isinstance(info, tuple)
            else ("超时" if info == "TIMEOUT" else "未知错误")
        )
        logger.warning(
            "  ⚠️ %s 基本信息失败（%s），名称/行业使用共享库、新浪或历史缓存",
            DATA_SOURCE,
            err,
        )
        info = {}
    quote_name = getattr(quote, "instrument_name", None)
    name = info.get("股票简称") or quote_name or _lookup_cached_name(code) or code
    info_industry = info.get("行业") if isinstance(info, dict) else None
    cached_industry = (
        None if lib_industry or info_industry else _lookup_cached_industry(code)
    )
    industry = lib_industry or info_industry or cached_industry or "未知"
    if lib_industry:
        results["industry_status"] = "verified"
        results["_industry_source"] = "a_stock_lib"
    elif info_industry:
        results["industry_status"] = "verified"
        results["_industry_source"] = DATA_SOURCE
    elif cached_industry:
        results["industry_status"] = "stale_cache"
        results["_industry_source"] = "historical_cache"
    else:
        results["industry_status"] = "missing"
        results["_industry_source"] = "none"
    current_price = quote.price
    if results["industry_status"] == "verified":
        store.update_qualitative_only_security(code, name, industry)
    results["_quote_as_of"] = quote.as_of
    results["_quote_source"] = quote.source
    total_mv = parse_float(info.get("总市值"))
    float_mv = parse_float(info.get("流通市值"))
    if total_mv and total_mv > 0:
        # TuShare daily_basic reports market value in CNY 10,000; AKShare uses CNY.
        results["total_market_cap"] = (
            total_mv * 10_000 if DATA_SOURCE == "tushare" else total_mv
        )
        results["_total_market_cap_source"] = (
            "tushare.daily_basic" if DATA_SOURCE == "tushare" else "akshare"
        )
    else:
        null_reasons["total_market_cap"] = "总市值字段缺失"
    if total_mv and total_mv > 0 and float_mv is not None:
        results["float_to_total_ratio"] = round(float_mv / total_mv * 100, 1)
    else:
        null_reasons["float_to_total_ratio"] = "市值字段缺失"
    logger.info(
        f"  ✅ {name}({code}) | 行业: {industry} | 当前价: {current_price}({quote.source}) | "
        f"流通比: {results.get('float_to_total_ratio', '─')}%"
    )
    return name, industry, current_price


def _fetch_fin_data(
    code: str, results: dict, null_reasons: dict
) -> tuple[Any, float | None, float | None]:
    """Step 2: 年度财务指标（ROE/增速/负债率/EPS 等）。返回 (fin_df, eps, bps)。"""
    source_label = "AKShare同花顺" if DATA_SOURCE == "akshare" else "TuShare"
    logger.info("  [2/7] 财务指标（%s年度）...", source_label)
    fin_df = timed_call_with_retry(_fetch_financials, code, timeout=API_TIMEOUT)
    if isinstance(fin_df, str):
        reason = "财务API超时"
    elif fin_df is None or isinstance(fin_df, tuple):
        reason = fin_df[1] if isinstance(fin_df, tuple) else "财务API失败"
    else:
        reason = None
    if reason:
        for k in _FIN_FIELDS_KEYS:
            null_reasons[k] = reason
        logger.warning("  ⚠️ 财务指标 获取失败（%s限速，已重试 3 次）", source_label)
        return fin_df, None, None
    fin_fields = _extract_fin_fields(fin_df)
    eps = fin_fields["eps"]
    bps = fin_fields["bps"]
    for k, v in fin_fields.items():
        results[k] = v
        if v is None:
            null_reasons[k] = "数据含缺失值"
    attrs = getattr(fin_df, "attrs", {})
    snapshot = attrs.get("latest_report_snapshot")
    if snapshot is not None:
        results["latest_report_snapshot"] = snapshot
    else:
        null_reasons["latest_report_snapshot"] = "数据源未返回可核验的最新报告快照"
    gate_inputs = attrs.get("risk_gate_inputs")
    if isinstance(gate_inputs, dict):
        results["_risk_gate_inputs"] = gate_inputs
    logger.info(
        f"  ✅ ROE3y={results.get('roe_3y_avg')}% | "
        f"净利增速3年均值={results.get('net_profit_growth')}% | "
        f"负债率={results.get('debt_ratio')}% | EPS={eps} | BPS={bps}"
    )
    return fin_df, eps, bps


def _compute_ttm_valuation(results: dict, null_reasons: dict) -> None:
    snapshot = results.get("latest_report_snapshot")
    fields = snapshot.get("fields", {}) if isinstance(snapshot, dict) else {}
    net_profit_field = fields.get("net_profit_ttm", {})
    growth_field = fields.get("net_profit_ttm_yoy", {})
    net_profit = (
        net_profit_field.get("value") if isinstance(net_profit_field, dict) else None
    )
    growth = growth_field.get("value") if isinstance(growth_field, dict) else None
    market_cap = results.get("total_market_cap")
    if isinstance(net_profit, (int, float)):
        results["net_profit_ttm"] = net_profit
    else:
        null_reasons["net_profit_ttm"] = "缺少构造TTM利润所需的同口径期间"
    if isinstance(growth, (int, float)):
        results["net_profit_ttm_yoy"] = growth
    else:
        null_reasons["net_profit_ttm_yoy"] = "缺少上年同口径TTM利润或基期非正"
    if (
        isinstance(market_cap, (int, float))
        and isinstance(net_profit, (int, float))
        and market_cap > 0
        and net_profit > 0
    ):
        results["pe_ttm_true"] = round(market_cap / net_profit, 2)
    else:
        null_reasons["pe_ttm_true"] = "总市值或TTM归母净利润缺失/非正"
    pe_ttm_true = results.get("pe_ttm_true")
    if (
        isinstance(pe_ttm_true, (int, float))
        and isinstance(growth, (int, float))
        and growth > 0
    ):
        results["peg_ttm"] = round(pe_ttm_true / growth, 2)
    elif isinstance(growth, (int, float)) and growth <= 0:
        null_reasons["peg_ttm"] = "净利润TTM增速非正，PEG不适用"
    else:
        null_reasons["peg_ttm"] = "真实TTM PE或正的TTM利润增速缺失"


def _fetch_pb_pe_data(
    code: str, current_price: float | None, results: dict, null_reasons: dict
) -> None:
    """Step 3: use the same annual EPS/BPS basis as historical percentiles."""
    logger.info("  [3/7] PB / PE_静态（当前价÷同期每股指标）...")
    bps = results.get("bps")
    if current_price and bps and bps > 0:
        results["pb"] = round(current_price / bps, 2)
        logger.info(f"  ✅ PB={results['pb']}（{current_price}/{bps}）")
    else:
        null_reasons["pb"] = f"BPS不可用（bps={bps}）或价格不可用"
        logger.warning("  ⚠️ PB 无法计算: %s", null_reasons["pb"])
    # 静态PE（非TTM）：当前价 ÷ 最近完整年报EPS。季报后实际PE偏高，分析时注意口径
    eps = results.get("eps")
    if current_price and eps and eps > 0:
        results["pe_static"] = round(current_price / eps, 2)
        results["pe_ttm"] = results["pe_static"]
        logger.info(f"  ✅ PE_静态={results['pe_static']}（{current_price}/{eps}）")
    else:
        null_reasons["pe_static"] = f"EPS不可用（eps={eps}）或价格不可用"
        null_reasons["pe_ttm"] = f"EPS不可用（eps={eps}）或价格不可用"
        logger.warning("  ⚠️ PE_静态无法计算: %s", null_reasons["pe_static"])


def compute_valuation_compatibility(
    *,
    current_price: float | None,
    annual_pb: float | None,
    annual_bps: float | None,
    annual_period: str | None,
    latest_snapshot: dict[str, Any] | None,
    quote_source: str | None,
    quote_as_of: str | None,
) -> dict[str, Any]:
    """Compare annual-basis PB with the latest-report BPS on the same quote."""

    snapshot = latest_snapshot if isinstance(latest_snapshot, dict) else {}
    raw_fields = snapshot.get("fields")
    fields = raw_fields if isinstance(raw_fields, dict) else {}
    raw_bps_field = fields.get("bps")
    bps_field = raw_bps_field if isinstance(raw_bps_field, dict) else {}
    latest_bps = bps_field.get("value")
    latest_period = snapshot.get("report_period")
    latest_source = snapshot.get("source")
    latest_announcement = snapshot.get("announcement_date")

    base = {
        "pb_cache": annual_pb,
        "pb_cache_bps": annual_bps,
        "pb_cache_bps_period": annual_period,
        "pb_latest_report": None,
        "canonical_pb": None,
        "pb_percentile_eligible": False,
        "latest_report_bps": latest_bps,
        "latest_report_period": latest_period,
        "relative_difference_pct": None,
        "status": "incomplete",
        "timing_eligible": False,
        "reason_code": "missing_or_invalid_input",
        "reason": "required valuation compatibility input is missing or invalid",
        "quote_source": quote_source,
        "quote_as_of": quote_as_of,
        "latest_report_bps_source": latest_source,
        "latest_report_announcement_date": latest_announcement,
    }

    numeric_values = (current_price, annual_pb, annual_bps, latest_bps)
    try:
        decimals = tuple(Decimal(str(value)) for value in numeric_values)
        price_float = float(str(current_price))
        latest_bps_float = float(str(latest_bps))
        quote_text = str(quote_as_of).strip()
        quote_timestamp_valid = datetime.fromisoformat(quote_text)
        announcement_text = str(latest_announcement).strip()
        announcement_valid = date.fromisoformat(announcement_text)
    except (InvalidOperation, TypeError, ValueError):
        return base
    period_pattern = re.compile(r"^\d{4}(?:Q1|半年报|Q3|年报)$")
    if (
        any(not value.is_finite() or value <= 0 for value in decimals)
        or not isinstance(annual_period, str)
        or period_pattern.fullmatch(annual_period.strip()) is None
        or not isinstance(latest_period, str)
        or period_pattern.fullmatch(latest_period.strip()) is None
        or not isinstance(quote_source, str)
        or not quote_source.strip()
        or not isinstance(latest_source, str)
        or not latest_source.strip()
        or not quote_text
        or ("T" not in quote_text and " " not in quote_text)
        or not quote_timestamp_valid
        or not announcement_valid
        or bps_field.get("status") != "ok"
    ):
        return base

    _price_decimal, pb_decimal, _annual_bps_decimal, _latest_bps_decimal = decimals
    pb_latest_report = round(price_float / latest_bps_float, 2)
    pb_latest_decimal = Decimal(str(pb_latest_report))
    difference_for_gate = (
        abs(pb_decimal - pb_latest_decimal) / pb_decimal * Decimal("100")
    )
    base["pb_latest_report"] = pb_latest_report
    base["canonical_pb"] = pb_latest_report
    base["relative_difference_pct"] = round(float(difference_for_gate), 2)

    if annual_period != latest_period:
        base.update(
            {
                "status": "latest_report_update",
                "timing_eligible": True,
                "pb_percentile_eligible": False,
                "reason_code": "latest_bps_period_update",
                "reason": "latest-report BPS is newer; only PB-percentile frameworks must wait for a compatible series",
            }
        )
    elif difference_for_gate <= Decimal("2.0"):
        base.update(
            {
                "status": "compatible",
                "timing_eligible": True,
                "pb_percentile_eligible": True,
                "reason_code": "within_2pct",
                "reason": "current-price PB differs by no more than 2%",
            }
        )
    else:
        base.update(
            {
                "reason_code": "same_period_value_mismatch",
                "reason": "same-period BPS values produce PB difference above 2%",
            }
        )
    return base


def _fetch_dividend(
    code: str,
    current_price: float | None,
    results: dict,
    null_reasons: dict,
    latest_report_year: int | None = None,
) -> tuple[float, str | None]:
    """Step 4: 股息率 + DPS（每股分红，用于股息率交叉验证）。"""
    logger.info("  [4/7] 计算股息率与每股分红...")
    div_df = timed_call(_fetch_dividends, code, timeout=API_TIMEOUT)
    dividend = _compute_dividends(div_df, current_price, latest_report_year)
    for field in ("dividend_yield", "dps", "dps_ttm"):
        if dividend[field] is not None:
            results[field] = dividend[field]
        else:
            null_reasons[field] = dividend[
                "ttm_reason" if field == "dps_ttm" else "reason"
            ]
    if dividend["dividend_yield"] is not None:
        logger.info(
            f"  ✅ 股息率={dividend['dividend_yield']}% | "
            f"DPS={dividend['dps']}元 | DPS_TTM={dividend['dps_ttm']}元"
        )
    else:
        logger.warning("  ⚠️ 股息率无法计算: %s", dividend["reason"])
    return dividend["split_ratio"], dividend["split_ex_date"]


def _fetch_percentiles(
    code: str,
    fin_df: Any,
    results: dict,
    null_reasons: dict,
    split_ratio: float = 0.0,
    split_ex_date: str | None = None,
) -> None:
    """Step 5: five-session change and PE/PB/PS historical percentiles."""
    logger.info("  [5/7] 计算5日涨跌、PE/PB历史分位与 PS_TTM 5年分位...")
    financials_available = fin_df is not None and not isinstance(fin_df, (str, tuple))
    if not financials_available:
        null_reasons["pe_percentile_5y"] = "财务数据不可用"
        null_reasons["pe_percentile_10y"] = "财务数据不可用"
        null_reasons["pb_percentile_10y"] = "财务数据不可用"
        logger.warning("  ⚠️ PE/PB跳过（财务数据不可用）；PS仍按独立估值历史核验")

    # 价格历史仅拉取一次，PE 和 PB 分位共享同一份数据
    price_df = _load_price_df(code, years=10)
    if isinstance(price_df, str):
        null_reasons["pe_percentile_5y"] = "历史价格API失败（网络错误，重试后仍不可用）"
        null_reasons["pe_percentile_10y"] = (
            "历史价格API失败（网络错误，重试后仍不可用）"
        )
        null_reasons["pb_percentile_10y"] = (
            "历史价格API失败（网络错误，重试后仍不可用）"
        )
        null_reasons["price_change_5d"] = "历史价格API失败（网络错误，重试后仍不可用）"
        null_reasons["ps_ttm"] = "历史估值API失败（网络错误，重试后仍不可用）"
        null_reasons["ps_percentile_5y"] = "历史估值API失败（网络错误，重试后仍不可用）"
        logger.warning("  ⚠️ 历史价格获取失败，跳过PE/PB分位计算")
        return

    price_change_5d, price_change_as_of, price_change_reason = compute_price_change_5d(
        price_df
    )
    if price_change_5d is None:
        null_reasons["price_change_5d"] = price_change_reason or "5日涨跌幅不可用"
    else:
        results["price_change_5d"] = price_change_5d
        results["_price_change_5d_as_of"] = price_change_as_of
        logger.info(f"  ✅ 近5个交易日涨跌={price_change_5d}%（{price_change_as_of}）")
    results["_pe_percentile_windows"] = {}
    for years in (5, 10):
        window = _trim_price_window(price_df, years)
        if not window.empty:
            results["_pe_percentile_windows"][str(years)] = {
                "sample_start": window["date"].min().date().isoformat(),
                "sample_end": window["date"].max().date().isoformat(),
            }

    if financials_available and results.get("pe_static"):
        for years in (5, 10):
            field = f"pe_percentile_{years}y"
            pct = compute_pe_percentile(
                code,
                results["pe_static"],
                fin_df,
                years=years,
                price_df=price_df,
                split_ratio=split_ratio,
                split_ex_date=split_ex_date,
            )
            if isinstance(pct, float):
                results[field] = pct
                logger.info(f"  ✅ PE{years}y分位={pct}%")
            else:
                null_reasons[field] = "历史PE数据点不足（<100）"
    else:
        null_reasons["pe_percentile_5y"] = "PE_静态不可用"
        null_reasons["pe_percentile_10y"] = "PE_静态不可用"

    if financials_available and results.get("pb"):
        pct = compute_pb_percentile(
            code,
            results["pb"],
            fin_df,
            years=10,
            price_df=price_df,
            split_ratio=split_ratio,
            split_ex_date=split_ex_date,
        )
        if isinstance(pct, float):
            results["pb_percentile_10y"] = pct
            logger.info(f"  ✅ PB10y分位={pct}%")
        else:
            null_reasons["pb_percentile_10y"] = "历史PB数据点不足（<100）"
    else:
        null_reasons["pb_percentile_10y"] = "PB 不可用"

    if DATA_SOURCE == "akshare":
        reason = "AKShare手动降级路径不提供同口径PS_TTM"
        null_reasons["ps_ttm"] = reason
        null_reasons["ps_percentile_5y"] = reason
    else:
        ps_ttm, percentile, metadata = compute_ps_ttm_percentile_5y(price_df)
        results["_ps_percentile_window"] = metadata
        if ps_ttm is None or percentile is None:
            reason = (
                "PS_TTM有效月不足60"
                if metadata["valid_months"] < 60
                else "PS_TTM缺失、非正或非有限"
            )
            null_reasons["ps_ttm"] = reason
            null_reasons["ps_percentile_5y"] = reason
        else:
            results["ps_ttm"] = ps_ttm
            results["ps_percentile_5y"] = percentile
            logger.info(f"  ✅ PS_TTM={ps_ttm} | PS5y分位={percentile}%")


def _fetch_bond_yield(null_reasons: dict) -> dict | None:
    """Step 6: 10年期国债收益率（D框架股息率溢价计算用，全局字段）。"""
    logger.info("  [6/7] 10年期国债收益率...")
    recent = store.get_market_indicator_snapshot(
        "bond_yield_10y", max_age=BOND_YIELD_REFRESH_INTERVAL
    )
    if recent is not None:
        logger.info(
            "  ✅ 复用1小时内国债收益率快照: %s%%（%s）",
            recent["value"],
            recent["as_of"],
        )
        return recent
    end_dt = date.today().strftime("%Y%m%d")
    start_dt = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    df = timed_call(_fetch_bond_yield_api, start_dt, end_dt, timeout=API_TIMEOUT)

    val = None
    if not (isinstance(df, (str, tuple)) or df is None or df.empty):
        curve = (
            df[df["曲线名称"] == "中债国债收益率曲线"]
            if "曲线名称" in df.columns
            else df
        )
        if curve.empty:
            curve = df
        val = parse_float(curve.iloc[-1].get("10年"))

    if val is not None:
        as_of = None
        for column in ("日期", "曲线日期", "date", "交易日期"):
            if column in curve.columns:
                try:
                    as_of = (
                        __import__("pandas")
                        .to_datetime(curve.iloc[-1][column])
                        .date()
                        .isoformat()
                    )
                except Exception:
                    as_of = None
                break
        if as_of is not None:
            store.set_market_indicator_snapshot(
                "bond_yield_10y", round(val, 4), as_of, "akshare.bond_china_yield"
            )
            logger.info(f"  ✅ 10年期国债={val}%（{as_of}）")
            return store.get_market_indicator_snapshot("bond_yield_10y")
        logger.warning("  ⚠️ 国债API返回值缺少可验证日期，不写入快照")

    cached = store.get_market_indicator_snapshot(
        "bond_yield_10y", max_age=timedelta(hours=24)
    )
    if cached is not None:
        logger.warning(
            "  ⚠️ 国债收益率 API 失败，复用24小时内可信快照: %s%%（%s）",
            cached["value"],
            cached["as_of"],
        )
        return cached
    null_reasons["bond_yield_10y"] = "国债收益率API失败，且无24小时内可信快照"
    logger.warning("  ⚠️ 国债收益率不可用；D框架择时评分必须标记 incomplete")
    return None


def _cache_gate_defaults(
    code: str, results: dict, data_period: str, industry: str
) -> None:
    """Persist explicit gates; P0 stays incomplete until official evidence is supplied.

    store.set_fundamentals carries supplied P0 evidence for the same annual period.
    """
    as_of = results.get("_quote_as_of") or data_period
    unavailable = ["unavailable"]
    regulatory = results.get("regulatory_gate")
    if not isinstance(regulatory, dict):
        regulatory = risk_gates.regulatory_gate(
            {
                "code": code,
                "report_period": data_period,
                "as_of": as_of,
                "sources": unavailable,
            }
        )
    regulatory["as_of"] = regulatory.get("as_of") or as_of
    regulatory["sources"] = regulatory.get("sources") or unavailable
    regulatory["rule_version"] = regulatory.get("rule_version") or "unavailable"
    for check in regulatory.get("checks", {}).values():
        check["as_of"] = check.get("as_of") or as_of
        check["sources"] = check.get("sources") or unavailable
    results["regulatory_gate"] = regulatory

    inputs = results.get("_risk_gate_inputs", {})
    for key, gate in (
        ("roe_structural_gate", risk_gates.roe_structural_gate),
        ("cash_flow_gate", risk_gates.cash_flow_gate),
    ):
        value = results.get(key)
        if not isinstance(value, dict):
            payload = dict(inputs.get(key, {})) if isinstance(inputs, dict) else {}
            payload.setdefault("report_period", data_period)
            if key == "cash_flow_gate":
                payload.update(
                    {
                        "total_market_cap": results.get("total_market_cap"),
                        "quote_as_of": results.get("_quote_as_of"),
                        "industry": industry,
                    }
                )
            value = gate(payload or {"as_of": as_of, "sources": unavailable})
        if (
            key == "cash_flow_gate"
            and value.get("status") == "not_applicable"
            and not risk_gates.official_sources(
                value.get("sources", []), allow_statement_mirror=True
            )
        ):
            value.update(
                {
                    "status": "incomplete",
                    "action_eligible": False,
                    "reason_code": "financial_framework_source_missing",
                }
            )
        value["as_of"] = value.get("as_of") or as_of
        value["sources"] = value.get("sources") or unavailable
        results[key] = value


def _build_cache_payload(
    code: str,
    name: str,
    industry: str,
    results: dict,
    null_reasons: dict,
    data_period: str | None,
) -> None:
    """Step 7: 将采集结果写入 cache，打印完成汇总。"""
    logger.info("  [7/7] 写入缓存...")
    if data_period is None:
        logger.error("  ❌ 无法从实际财务报表确定 data_period，拒绝写入 fundamentals")
        raise SystemExit(1)
    _cache_gate_defaults(code, results, data_period, industry)
    business_fields = [key for key in FIELDS if key != "bond_yield_10y"]
    cache_data = {key: results.get(key) for key in business_fields}
    normalized_reasons: dict[str, str] = {}
    provenance: dict[str, dict[str, str]] = {}
    quote_as_of = results.get("_quote_as_of")
    quote_derived_fields = {
        "pe_static",
        "pe_ttm",
        "pe_ttm_true",
        "peg_ttm",
        "total_market_cap",
        "pb",
        "valuation_compatibility",
        "dividend_yield",
        "pe_percentile_5y",
        "pe_percentile_10y",
        "pb_percentile_10y",
        "float_to_total_ratio",
    }
    for key in business_fields:
        source_layer = FIELDS[key][1]
        provenance_source = (
            DATA_SOURCE if source_layer == "structured" else source_layer
        )
        if key == "industry_status":
            provenance_source = results.get("_industry_source", provenance_source)
        elif key == "total_market_cap":
            provenance_source = results.get(
                "_total_market_cap_source", provenance_source
            )
        gate_status = (
            cache_data[key].get("status")
            if key in risk_gates.GATE_NAMES and isinstance(cache_data[key], dict)
            else None
        )
        if cache_data[key] is None:
            reason = null_reasons.get(key) or (
                "需要WebSearch补充" if source_layer == "web" else "数据源未返回有效值"
            )
            normalized_reasons[key] = reason
            status = "missing"
        elif gate_status not in (None, "clear", "not_applicable"):
            reason = str(cache_data[key].get("reason_code") or gate_status)
            normalized_reasons[key] = reason
            status = "missing"
        else:
            status = "ok"
        provenance[key] = {
            "source": provenance_source,
            "as_of": (
                results.get("_price_change_5d_as_of") or data_period
                if key == "price_change_5d"
                else quote_as_of
                if key in quote_derived_fields and quote_as_of
                else data_period
            ),
            "status": status,
        }
        if key in {"pe_percentile_5y", "pe_percentile_10y"}:
            years = "5" if key.endswith("_5y") else "10"
            provenance[key].update(
                {
                    "window_years": years,
                    "basis": "annual_eps_disclosure_lag_adjusted",
                    "price_basis": "raw_close",
                    "split_policy": "detected_event_eps_adjustment",
                }
            )
            provenance[key].update(
                results.get("_pe_percentile_windows", {}).get(years, {})
            )
        if key in {"ps_ttm", "ps_percentile_5y"} and DATA_SOURCE != "akshare":
            ps_window = results.get("_ps_percentile_window", {})
            provenance[key].update(ps_window)
            provenance[key]["source"] = "tushare.daily_basic"
            provenance[key]["as_of"] = ps_window.get("sample_end") or data_period
        if key == "latest_report_snapshot" and isinstance(cache_data[key], dict):
            snapshot = cache_data[key]
            provenance[key]["source"] = (
                snapshot.get("source") or provenance[key]["source"]
            )
            provenance[key]["as_of"] = snapshot.get("announcement_date") or data_period
        elif key in {"net_profit_ttm", "net_profit_ttm_yoy"}:
            snapshot = results.get("latest_report_snapshot", {})
            provenance[key]["source"] = (
                snapshot.get("source") or provenance[key]["source"]
            )
            provenance[key]["as_of"] = snapshot.get("announcement_date") or data_period
    cache_data.update(
        {
            "data_period": data_period,
            "null_reasons": normalized_reasons,
            "field_provenance": provenance,
        }
    )
    try:
        msg = store.set_fundamentals(code, name, industry, cache_data)
    except ValueError as exc:
        logger.error("  ❌ fundamentals校验失败: %s", exc)
        raise SystemExit(1) from exc
    logger.info(f"  ✅ {msg}")
    gate_names = set(risk_gates.GATE_NAMES)
    ordinary_fields = [key for key in business_fields if key not in gate_names]
    fetched_count = sum(1 for key in ordinary_fields if results.get(key) is not None)
    complete_gates = sum(
        1
        for key in gate_names
        # set_fundamentals may carry P0 evidence forward into cache_data.
        if isinstance(cache_data.get(key), dict)
        and cache_data[key].get("status") in {"clear", "not_applicable"}
    )
    logger.info(
        f"\n=== {name}({code}) 完成 | "
        f"普通字段: {fetched_count}/{len(ordinary_fields)} | "
        f"决策门完整: {complete_gates}/{len(gate_names)} | "
        f"{datetime.now().strftime('%H:%M:%S')} ==="
    )
    if null_reasons:
        logger.info("null 字段原因：")
        for k, reason in null_reasons.items():
            label = FIELDS.get(k, (k,))[0]
            logger.info(f"  - {label}: {reason}")


# ─── 命令实现 ────────────────────────────────────────────────────────────────


def cmd_fetch(args: list[str]) -> None:
    if not args:
        print("错误：需要参数 <股票代码>", file=sys.stderr)
        sys.exit(1)
    code = args[0]

    print(f"[fetch] 开始获取 {code} 基本面数据...", flush=True)
    results = {}
    null_reasons = {}

    name, industry, current_price = _fetch_spot_data(code, results, null_reasons)
    fin_df, _eps, _bps = _fetch_fin_data(code, results, null_reasons)
    _compute_ttm_valuation(results, null_reasons)
    data_period = _extract_data_period(fin_df)
    latest_report_year = int(data_period[:4]) if data_period else None
    split_ratio, split_ex_date = _fetch_dividend(
        code, current_price, results, null_reasons, latest_report_year
    )
    if split_ratio > 0:
        for field in ("eps", "bps", "operating_cf_per_share"):
            val = results.get(field)
            if val is not None:
                original = val
                results[field] = round(val / (1 + split_ratio), 4)
                logger.info(
                    f"  ✅ 送转复权调整: {field} {original} → {results[field]} "
                    f"(ratio={split_ratio}, ex={split_ex_date})"
                )
    _fetch_pb_pe_data(code, current_price, results, null_reasons)
    results["valuation_compatibility"] = compute_valuation_compatibility(
        current_price=current_price,
        annual_pb=results.get("pb"),
        annual_bps=results.get("bps"),
        annual_period=data_period,
        latest_snapshot=results.get("latest_report_snapshot"),
        quote_source=results.get("_quote_source"),
        quote_as_of=results.get("_quote_as_of"),
    )
    _fetch_percentiles(code, fin_df, results, null_reasons, split_ratio, split_ex_date)
    _fetch_bond_yield(null_reasons)
    _build_cache_payload(code, name, industry, results, null_reasons, data_period)


def _print_field_quality_table(data):
    src_icon = {
        "structured": "📊 " + ("AKShare" if DATA_SOURCE == "akshare" else "TuShare"),
        "akshare": "📊 AKShare",
        "computed": "🔢 自动计算",
        "compatibility": "↩ 兼容别名",
        "web": "🔍 需WebSearch",
    }

    print(f"  {'字段':<24} {'值':>10}  {'来源':<16} 状态")
    print("  " + "─" * 62)

    core_total = core_fetched = 0
    for key, (label, src) in FIELDS.items():
        val = data.get(key)
        icon = src_icon.get(src, src)
        val_str = str(val) if val is not None else "─"

        if src in ("structured", "akshare", "computed"):
            core_total += 1
            if val is not None:
                core_fetched += 1
                status = "✅"
            else:
                status = "⚠️ null"
        else:
            status = "─ (web补充)" if val is None else "✅"

        print(f"  {label:<24} {val_str:>10}  {icon:<16} {status}")

    print("  " + "─" * 62)
    return core_fetched, core_total


def _print_data_quality_summary(data, core_fetched, core_total):
    score = round(core_fetched / core_total * 100) if core_total else 0
    web_filled = sum(
        1 for k, (_, s) in FIELDS.items() if s == "web" and data.get(k) is not None
    )
    web_total = sum(1 for _, (_, s) in FIELDS.items() if s == "web")
    print(
        f"\n  核心字段: {core_fetched}/{core_total} | "
        f"Web补充: {web_filled}/{web_total} | "
        f"数据完整性: {score}%"
    )

    missing_core = [
        FIELDS[k][0]
        for k, (_, s) in FIELDS.items()
        if s in ("structured", "akshare", "computed") and data.get(k) is None
    ]
    if missing_core:
        print(f"  待补充: {', '.join(missing_core)}")


def _quarter_metric(value: Any, basis: str, periods: list[str], reason=None) -> dict:
    import math

    number = parse_float(value)
    if number is None or not math.isfinite(number):
        return {
            "value": None,
            "basis": basis,
            "periods": periods,
            "reason": reason or "缺少同口径已披露数据",
        }
    return {"value": number, "basis": basis, "periods": periods, "reason": None}


def build_quarter_check(code: str, frames: dict[str, Any]) -> dict:
    """Calculate only disclosed periods; no estimates or interim annualisation."""
    import pandas as pd

    rows = {
        key: _deduplicated_report_rows(
            frames.get(key), prefer_corrected=key == "deducted"
        )
        for key in ("income", "cashflow", "balance", "deducted")
    }
    periods = [
        frame["_period"].max()
        for frame in rows.values()
        if frame is not None and not frame.empty
    ]
    latest = max(periods) if periods else None
    result = {"code": code, "report_period": None, "fields": {}}
    fields = result["fields"]
    if latest is not None:
        result["report_period"] = latest.date().isoformat()
        prior = latest - pd.DateOffset(years=1)
        year_end = pd.Timestamp(latest.year - 1, 12, 31)
        previous = latest - pd.offsets.QuarterEnd()
    else:
        prior = year_end = previous = None

    def labels(*values):
        return [value.date().isoformat() for value in values if value is not None]

    def number(frame, period, column):
        value = _row_number(_row_at_period(rows.get(frame), period), column)
        return _quarter_metric(value, "", [])["value"]

    def growth(current, base, basis, comparison_periods):
        reason = None
        if base is not None and base <= 0:
            reason = "比较基期非正，增长率不适用"
        value = (
            (current / base - 1) * 100
            if current is not None and base is not None and base > 0
            else None
        )
        return _quarter_metric(value, basis, comparison_periods, reason)

    for key, frame, column in (
        ("revenue", "income", "revenue"),
        ("net_profit_attributable", "income", "n_income_attr_p"),
        ("deducted_net_profit", "deducted", "profit_dedt"),
    ):
        current, base = number(frame, latest, column), number(frame, prior, column)
        fields[key + "_ytd"] = _quarter_metric(current, "ytd", labels(latest))
        fields[key + "_ytd_yoy"] = growth(
            current, base, "ytd_yoy", labels(latest, prior)
        )
        single_periods, comparison_periods = labels(latest), labels(latest, prior)
        if latest is not None and latest.month != 3:
            preceding = number(frame, previous, column)
            prior_previous = previous - pd.DateOffset(years=1)
            prior_preceding = number(frame, prior_previous, column)
            current = current - preceding if None not in (current, preceding) else None
            base = (
                base - prior_preceding if None not in (base, prior_preceding) else None
            )
            single_periods = labels(latest, previous)
            comparison_periods = labels(latest, previous, prior, prior_previous)
        fields[key + "_single_quarter"] = _quarter_metric(
            current, "single_quarter", single_periods
        )
        fields[key + "_single_quarter_yoy"] = growth(
            current, base, "single_quarter_yoy", comparison_periods
        )

    ttm_periods = (
        labels(latest)
        if latest is not None and latest.month == 12
        else labels(year_end, latest, prior)
    )
    for key, frame, column in (
        ("operating_cash_flow_ttm", "cashflow", "n_cashflow_act"),
        ("net_profit_attributable_ttm", "income", "n_income_attr_p"),
    ):
        value = None
        required = (
            [latest]
            if latest is not None and latest.month == 12
            else [latest, prior, year_end]
        )
        if latest is not None and all(
            number(frame, period, column) is not None for period in required
        ):
            value, _ = _ttm_pair(rows.get(frame), latest, column)
        fields[key] = _quarter_metric(
            value, "ttm", ttm_periods, "缺少TTM所需的最新期、上年同期或上年年末数据"
        )
    cash = fields["operating_cash_flow_ttm"]["value"]
    profit = fields["net_profit_attributable_ttm"]["value"]
    ratio = (
        cash / profit
        if cash is not None and profit is not None and profit > 0
        else None
    )
    fields["operating_cash_flow_to_net_profit_ttm"] = _quarter_metric(
        ratio,
        "ttm_ratio",
        ttm_periods,
        "TTM归母净利润非正，比例不适用" if profit is not None and profit <= 0 else None,
    )
    for key, column in (
        ("accounts_receivable", "accounts_receiv"),
        ("inventory", "inventories"),
        ("notes_payable", "notes_payable"),
        ("contract_liabilities", "contract_liab"),
    ):
        current = number("balance", latest, column)
        fields[key] = _quarter_metric(current, "period_end", labels(latest))
        fields[key + "_yoy"] = growth(
            current,
            number("balance", prior, column),
            "yoy_same_period_end",
            labels(latest, prior),
        )
        seasonal = growth(
            current,
            number("balance", year_end, column),
            "vs_year_start",
            labels(latest, year_end),
        )
        seasonal["label"] = "较年初（季节性比较，非同比）"
        fields[key + "_vs_year_start"] = seasonal
    return result


def cmd_quarter(args: list[str]) -> None:
    import json
    import tushare
    from a_share_research.core.providers import TushareFinancialProvider
    from a_share_research.core.providers.tushare_quotes import to_tushare_stock_code

    if (
        len(args) not in (1, 2)
        or not re.fullmatch(r"\d{6}", args[0])
        or (len(args) == 2 and args[1] != "--json")
    ):
        print("用法: a-stock-fetch quarter <股票代码> [--json]", file=sys.stderr)
        raise SystemExit(1)
    code = args[0]
    frames = {}
    errors = {}
    # Existing adapters log to stdout; JSON output must remain a single object.
    was_disabled = logger.disabled
    logger.disabled = "--json" in args
    try:
        from contextlib import redirect_stdout

        with redirect_stdout(sys.stderr if "--json" in args else sys.stdout):
            provider = TushareFinancialProvider(token=paths.tushare_token())
            for key, method, source in (
                ("income", "fetch_income_history", "tushare.income"),
                ("balance", "fetch_balance_history", "tushare.balancesheet"),
                ("cashflow", "fetch_cashflow_history", "tushare.cashflow"),
            ):
                method_call = getattr(provider, method, None)
                raw = (
                    timed_call(method_call, code, timeout=API_TIMEOUT)
                    if method_call is not None
                    else ("ERROR", f"{source}: provider不支持")
                )
                frame = (
                    _tushare_frame(raw, source)
                    if not _is_timed_call_error(raw)
                    else raw
                )
                if frame is None or isinstance(frame, (str, tuple)):
                    errors[key] = str(frame or "数据源无返回")
                    frame = None
                frames[key] = frame

            # The provider's indicator projection does not include deducted profit.
            def fetch_deducted():
                return tushare.pro_api(paths.tushare_token()).fina_indicator(
                    ts_code=to_tushare_stock_code(code),
                    fields="ts_code,end_date,ann_date,update_flag,profit_dedt",
                )

            deducted = timed_call(fetch_deducted, timeout=API_TIMEOUT)
            if deducted is None or isinstance(deducted, (str, tuple)):
                errors["deducted"] = str(deducted or "扣非净利润数据源无返回")
                deducted = None
            frames["deducted"] = deducted
            result = build_quarter_check(code, frames)
            result["source_errors"] = errors
            fields = result["fields"]
            if "deducted" in errors:
                for name, metric in fields.items():
                    if name.startswith("deducted_net_profit"):
                        metric["reason"] = (
                            f"扣非净利润直接源不可用: {errors['deducted']}"
                        )
            quote = None
            try:
                quote = _fetch_realtime_quote(code)
            except Exception as exc:
                errors["price"] = str(exc)
            price = _quarter_metric(getattr(quote, "price", None), "current_quote", [])
            if price["value"] is not None and price["value"] <= 0:
                price = _quarter_metric(None, "current_quote", [], "价格非正")
            price["periods"] = (
                [quote.as_of]
                if price["value"] is not None and getattr(quote, "as_of", None)
                else []
            )
            price.update(
                source=getattr(quote, "source", None),
                as_of=getattr(quote, "as_of", None),
            )
            fields["price"] = price
            # Use fetch's financial adapter for the same split cutoff year.
            fin_df = timed_call(_fetch_financials, code, timeout=API_TIMEOUT)
            data_period = _extract_data_period(fin_df)
            latest_report_year = int(data_period[:4]) if data_period else None
            div = timed_call(_fetch_dividends, code, timeout=API_TIMEOUT)
            dividend = _compute_dividends(div, price["value"], latest_report_year)
            dps, reason = dividend["dps"], dividend["reason"]
            if dps is not None and dps <= 0:
                dps, reason = None, "每股分红非正"
            fields["dps"] = _quarter_metric(
                dps, "fetcher_dividend_year", dividend["periods"], reason
            )
            fields["dps"].update(
                year=dividend["year"],
                split_ratio=dividend["split_ratio"],
                split_ex_date=dividend["split_ex_date"],
            )
            null_reasons = {}
            bond = _fetch_bond_yield(null_reasons)
            bond_value = _quarter_metric(
                (bond or {}).get("value"),
                "cgb_10y_yield_pct",
                [],
                null_reasons.get("bond_yield_10y"),
            )
            bond_value.update(
                source=(bond or {}).get("source"), as_of=(bond or {}).get("as_of")
            )
            bond_value["periods"] = (
                [(bond or {})["as_of"]] if (bond or {}).get("as_of") else []
            )
            fields["bond_yield_10y"] = bond_value
            for spread in (150, 100):
                value = None
                if (
                    dps is not None
                    and dps > 0
                    and price["value"] is not None
                    and bond_value["value"] is not None
                ):
                    value = price["value"] * (
                        bond_value["value"] / 100 + spread / 10000
                    )
                fields[f"dps_at_{spread}bp_spread"] = _quarter_metric(
                    value,
                    "dividend_spread_threshold",
                    price["periods"] + bond_value["periods"],
                    "缺少有效分红、现价或国债收益率",
                )
    finally:
        logger.disabled = was_disabled
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        print(f"{code} 季度核对 | 最新报告期: {result['report_period'] or '未知'}")
        for name, metric in fields.items():
            print(
                f"{name}: {metric['value']} | {metric['basis']} | {','.join(metric['periods'])} | {metric.get('label') or metric['reason'] or ''}"
            )


def cmd_check(args: list[str]) -> None:
    if not args:
        print("错误：需要参数 <股票代码>", file=sys.stderr)
        sys.exit(1)
    code = args[0]

    data = store.get_fundamentals(code)
    if data is None:
        print(f"未找到 {code} 的缓存数据，请先执行: a-stock-fetch fetch {code}")
        return

    meta = data.pop("_cache_meta", {})
    name = meta.get("name", code)
    industry = meta.get("industry", "未知")
    updated = meta.get("updated_at", "未知")
    ttl = meta.get("ttl_hours", "?")

    print(f"\n=== {name}({code}) 数据质量报告 ===")
    print(f"行业: {industry} | 更新: {updated} | TTL: {ttl}h\n")
    core_fetched, core_total = _print_field_quality_table(data)
    _print_data_quality_summary(data, core_fetched, core_total)


def cmd_batch(args: list[str]) -> None:
    codes = store.list_codes()
    # 去重保序（list_codes 已按更新时间排序，保序去重防万一）
    seen, unique = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    if not unique:
        print("watchlist 为空")
        return

    print(f"=== 批量更新 {len(unique)} 支股票 ===\n")
    summary = []
    for i, code in enumerate(unique, 1):
        print(f"[{i}/{len(unique)}] ── {code} ──")
        try:
            cmd_fetch([code])
            summary.append((code, "✅"))
        except (Exception, SystemExit) as e:
            summary.append((code, f"❌ {e}"))
        print()

    print("=== 批量汇总 ===")
    for code, status in summary:
        print(f"  {code}: {status}")
    ok = sum(1 for _, s in summary if s == "✅")
    print(f"完成: {ok}/{len(summary)}")


COMMANDS = {
    "fetch": cmd_fetch,
    "quarter": cmd_quarter,
    "check": cmd_check,
    "batch": cmd_batch,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"]:
        print(__doc__)
        return 0
    command = args[0]
    if command not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 1
    try:
        COMMANDS[command](args[1:])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
