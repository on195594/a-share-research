"""Daily account NAV derived from the bound ledger and unadjusted close prices.

Assets at each close are ledger positions times that day's close plus ledger cash; the ledger
already reconciles positions and cash against broker snapshots when a batch is posted.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from a_share_research import db
from a_share_research.account import ledger
from a_share_research.account.reconciliation import Event, Opening, project
from a_share_research.core import account_nav

BENCHMARK = 'H00906.CSI'
METHOD = 'daily_unit_nav_v1'
BASIS = '账本持仓×不复权收盘价+账本现金；出入金按所在交易日收盘计；基准为中证800全收益'
# Look back far enough that a stock suspended on the opening day still has a prior close.
PRICE_LOOKBACK = timedelta(days=30)

Closes = dict[date, Decimal]


def _book(conn, account_id):
    book = conn.execute('SELECT opened_at, opening FROM account_books WHERE account_id=?', (account_id,)).fetchone()
    if book is None:
        raise ledger.LedgerError('尚未建立影子账本期初', 404)
    rows = conn.execute('SELECT j.event FROM account_journal j LEFT JOIN account_voids v ON v.event_id=j.id '
                        'WHERE j.account_id=? AND v.event_id IS NULL', (account_id,))
    events = sorted((Event.model_validate_json(row['event']) for row in rows), key=lambda e: e.occurred_at)
    return datetime.fromisoformat(book['opened_at']), Opening.model_validate_json(book['opening']), events


def _flow(event):
    return {'deposit': event.cash, 'withdrawal': -event.cash}.get(event.kind, Decimal(0))


def _close_on_or_before(closes: Closes, day: date):
    found = [d for d in closes if d <= day]
    return closes[max(found)] if found else None


def _month_ago(day: date) -> date:
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    for dom in range(day.day, 0, -1):
        try:
            return date(year, month, dom)
        except ValueError:
            continue
    raise AssertionError('unreachable')


def _text(value: Decimal, places: str) -> str:
    return str(value.quantize(Decimal(places)))


def _period(rows, label, start, end):
    result = account_nav.period(rows, max(start, rows[0]['date']), end)
    return dict(label=label, start=result['start'].isoformat(), end=result['end'].isoformat(),
                account_return=_text(result['account_return'], '1e-6'),
                benchmark_return=_text(result['benchmark_return'], '1e-6'),
                excess_percentage_points=_text(result['excess_percentage_points'], '1e-4'))


def daily(conn, principal, account_id: str, *, today: date,
          fetch_index: Callable[[date, date], Closes], fetch_closes: Callable[[str, date, date], Closes],
          start: date | None = None, end: date | None = None) -> dict:
    """Return the NAV series from the opening close and the standard comparison periods."""
    with db.transaction(conn, immediate=False):
        ledger.authorize(conn, principal)
        opened_at, opening, events = _book(conn, account_id)
    first_day = opened_at.astimezone(db.SHANGHAI).date()
    index = fetch_index(first_day, today)
    stamps = [(day, datetime.combine(day, time(15), db.SHANGHAI)) for day in sorted(index)]
    stamps = [(day, stamp) for day, stamp in stamps if stamp >= opened_at]
    if not stamps:
        raise ledger.LedgerError(f'{BENCHMARK} 尚无开户日 {first_day} 及之后的收盘值，收盘数据发布后再查', 422)

    holdings, previous = [], opened_at
    for day, stamp in stamps:
        position = project(opening, [e for e in events if e.occurred_at <= stamp])
        flow = sum((_flow(e) for e in events if previous < e.occurred_at <= stamp), Decimal(0))
        holdings.append((day, position, flow))
        previous = stamp
    codes = sorted({code for _, position, _ in holdings for code in position.positions})
    closes = {code: fetch_closes(code, first_day - PRICE_LOOKBACK, today) for code in codes}

    # Stop at the last close every held stock has reached, so an unpublished close is never
    # filled with the previous one; a suspension before that point keeps its last close.
    gaps, last_day = [], stamps[-1][0]
    for code in sorted(holdings[-1][1].positions):
        latest = max(closes[code], default=None)
        if latest is None or latest < last_day:
            gaps.append(f'{code} 收盘价只到 {latest or "无"}，净值截至该日')
            last_day = min(last_day, latest or first_day)

    days = []
    for day, position, flow in holdings:
        if day > last_day:
            break
        assets = position.cash
        for code, shares in position.positions.items():
            price = _close_on_or_before(closes[code], day)
            if price is None:
                raise ledger.LedgerError(f'{code} 在 {day} 及之前 30 天没有收盘价，无法估值', 503)
            assets += price * shares
        days.append(account_nav.Day(date=day, total_assets=assets, flow=flow, benchmark=index[day]))
    if not days:
        raise ledger.LedgerError('持仓股票尚无开户日及之后的收盘价，收盘数据发布后再查', 422)
    try:
        rows = account_nav.series(days)
    except ValueError as exc:
        raise ledger.LedgerError(str(exc), 422) from None

    as_of = rows[-1]['date']
    periods = []
    # A young account clips the longer windows to the opening; repeat a span only once.
    for label, since in (('开户以来', rows[0]['date']), ('近1月', _month_ago(as_of)),
                         ('今年以来', date(as_of.year - 1, 12, 31))):
        item = _period(rows, label, since, as_of)
        if all(item['start'] != p['start'] for p in periods):
            periods.append(item)
    if start is not None or end is not None:
        try:
            periods.append(_period(rows, '指定区间', start or rows[0]['date'], end or as_of))
        except ValueError as exc:
            raise ledger.LedgerError(f'{exc}；可查区间为 {rows[0]["date"]} 至 {as_of}', 422) from None
    return dict(account_id=account_id, method=METHOD, benchmark=BENCHMARK, basis=BASIS,
                opened_at=opened_at.isoformat(), as_of=as_of.isoformat(), periods=periods, gaps=gaps,
                days=[dict(date=row['date'].isoformat(), total_assets=_text(row['total_assets'], '0.01'),
                           flow=_text(row['flow'], '0.01'), nav=_text(row['nav'], '1e-6'),
                           benchmark=str(row['benchmark'])) for row in rows])
