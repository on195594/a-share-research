"""Pure daily unit NAV from close valuations; callers supply ledger-derived assets and flows.

Each day's return is Modified Dietz: (assets - previous assets - flow) over previous assets plus
the flows weighted by the share of the trading session they were present for. Trading costs,
dividends and taxes stay inside the assets.
"""
from datetime import date
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Day(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    date: date
    total_assets: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    flow: Annotated[Decimal, Field(allow_inf_nan=False)] = Decimal(0)
    # Sum of each flow times the fraction of the session after it (1 before the open, 0 at the close).
    weighted_flow: Annotated[Decimal, Field(allow_inf_nan=False)] = Decimal(0)
    benchmark: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class NonPositiveExposure(ValueError):
    """There was P&L but no positive Modified Dietz denominator for a close."""

    def __init__(self, day: date, gain: Decimal):
        super().__init__(f'{day} 期初资金为零或加权资金基数非正，却有 {gain} 的盈亏')
        self.day = day


def _context():
    return localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN))


def series(days: list[Day]) -> list[dict]:
    """Return the unit NAV per close, chaining daily returns from NAV 1 at the first close."""
    if not days:
        raise ValueError('至少需要一个收盘估值')
    if any(a.date >= b.date for a, b in zip(days, days[1:])):
        raise ValueError('收盘估值须按日期严格递增')
    if days[0].total_assets <= 0:
        raise ValueError('起点总资产须为正')
    with _context():
        nav = Decimal(1)
        rows = [dict(date=days[0].date, total_assets=days[0].total_assets, flow=days[0].flow,
                     nav=nav, benchmark=days[0].benchmark)]
        for previous, day in zip(days, days[1:]):
            gain = day.total_assets - previous.total_assets - day.flow
            base = previous.total_assets + day.weighted_flow
            if base <= 0:
                # An emptied account keeps its NAV until money comes back in.
                if gain != 0:
                    raise NonPositiveExposure(day.date, gain)
            else:
                nav *= 1 + gain / base
            if nav <= 0:
                raise ValueError(f'{day.date} 单日亏损超过全部资金，请核对当日出入金记录')
            rows.append(dict(date=day.date, total_assets=day.total_assets, flow=day.flow, nav=nav,
                             benchmark=day.benchmark))
    return rows


def period(rows: list[dict], start: date, end: date) -> dict:
    """Compare the closes on or before start and end; both must fall inside the series."""
    def close_on_or_before(day):
        found = [row for row in rows if row['date'] <= day]
        if not found:
            raise ValueError(f'{day} 早于首个收盘估值 {rows[0]["date"]}')
        return found[-1]

    if start > end:
        raise ValueError('起点须不晚于终点')
    first, last = close_on_or_before(start), close_on_or_before(end)
    if first['date'] > last['date']:
        raise ValueError('起点须不晚于终点')
    try:
        with _context():
            account = last['nav'] / first['nav'] - 1
            benchmark = last['benchmark'] / first['benchmark'] - 1
            return dict(start=first['date'], end=last['date'], account_return=account,
                        benchmark_return=benchmark, excess_percentage_points=(account - benchmark) * 100)
    except DecimalException as exc:
        raise ValueError('净值计算无法表示') from exc
