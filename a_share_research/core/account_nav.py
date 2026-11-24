"""Pure daily unit NAV from close valuations; callers supply ledger-derived assets and flows.

Each external cash flow is booked at the close of its trading day: the pre-flow value is that
close's assets minus the flow. Trading costs, dividends and taxes stay inside the assets.
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
    benchmark: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


def _context():
    return localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN))


def series(days: list[Day]) -> list[dict]:
    """Return the unit NAV per close; the first close defines NAV 1 and the initial units."""
    if not days:
        raise ValueError('至少需要一个收盘估值')
    if any(a.date >= b.date for a, b in zip(days, days[1:])):
        raise ValueError('收盘估值须按日期严格递增')
    if days[0].total_assets <= 0:
        raise ValueError('起点总资产须为正')
    with _context():
        units = days[0].total_assets
        rows = [dict(date=days[0].date, total_assets=days[0].total_assets, flow=days[0].flow,
                     nav=Decimal(1), benchmark=days[0].benchmark)]
        for day in days[1:]:
            before = day.total_assets - day.flow
            if before <= 0:
                raise ValueError(f'{day.date} 出入金前总资产不为正，不能延续单位净值')
            nav = before / units
            units += day.flow / nav
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
