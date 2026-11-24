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
METHOD = 'daily_modified_dietz_v1'
BASIS = ('账本持仓×不复权收盘价+账本现金；分红与送转按账本记账日，不推算应收，晚于除权入账可能放大期间回撤；'
         '首个正资产收盘为净值 1，基期前费用已反映在资产中，不计入后续收益；'
         '出入金按当日交易时段剩余占比计入收益分母（修正 Dietz）；基准为中证800全收益')
# Look back far enough that a stock suspended on the opening day still has a prior close.
PRICE_LOOKBACK = timedelta(days=30)

Closes = dict[date, Decimal]


def _book(conn, account_id):
    book = conn.execute('SELECT opened_at, opening FROM account_books WHERE account_id=?', (account_id,)).fetchone()
    if book is None:
        raise ledger.LedgerError('尚未建立影子账本期初', 404)
    rows = conn.execute('SELECT j.event FROM account_journal j LEFT JOIN account_voids v ON v.event_id=j.id '
                        'WHERE j.account_id=? AND v.event_id IS NULL ORDER BY j.id', (account_id,))
    events = sorted((Event.model_validate_json(row['event']) for row in rows), key=lambda e: e.occurred_at)
    return datetime.fromisoformat(book['opened_at']), Opening.model_validate_json(book['opening']), events


def _flow(event):
    return {'deposit': event.cash, 'withdrawal': -event.cash}.get(event.kind, Decimal(0))


def _session_left(at: datetime, day: date) -> Decimal:
    """Share of the 09:30-11:30 and 13:00-15:00 session on day still ahead at the flow time."""
    local = at.astimezone(db.SHANGHAI)
    if local.date() < day:
        return Decimal(1)
    minute = Decimal(local.hour * 3600 + local.minute * 60 + local.second) / 60
    elapsed = sum((min(max(minute, start), end) - start for start, end in ((570, 690), (780, 900))),
                  Decimal(0))
    return Decimal(1) - elapsed / Decimal(240)


def latest_close_day(now: datetime) -> date:
    """Do not expect today's daily values until the Shanghai trading session has closed."""
    local = now.astimezone(db.SHANGHAI)
    return local.date() if local.time() >= time(15) else local.date() - timedelta(days=1)


def _counts_on(event, day: date, stamp: datetime) -> bool:
    # Ex-date closes already drop the payout, so a dividend or bonus booked later that day belongs
    # to that close rather than the next one.
    return event.occurred_at <= stamp or (event.kind in ('dividend', 'adjustment')
                                          and event.occurred_at.astimezone(db.SHANGHAI).date() == day)


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
          fetch_trading_days: Callable[[date, date], set[date]],
          fetch_suspended: Callable[[str, date, date], set[date]],
          start: date | None = None, end: date | None = None) -> dict:
    """Return the NAV series from the opening close and the standard comparison periods."""
    with db.transaction(conn, immediate=False):
        ledger.authorize(conn, principal)
        opened_at, opening, events = _book(conn, account_id)
    first_day = opened_at.astimezone(db.SHANGHAI).date()
    no_close = f'开户日 {first_day} 及之后尚无可用收盘估值，收盘数据发布后再查'
    if today < first_day:
        raise ledger.LedgerError(no_close, 422)
    trading_days = fetch_trading_days(first_day, today)
    stamps = [(day, datetime.combine(day, time(15), db.SHANGHAI)) for day in sorted(trading_days)]
    stamps = [(day, stamp) for day, stamp in stamps if stamp >= opened_at]
    if not stamps:
        raise ledger.LedgerError(no_close, 422)
    index = fetch_index(stamps[0][0], today)
    gaps = []
    missing_index = next((day for day, _ in stamps if day not in index), None)
    if missing_index is not None:
        message = f'{BENCHMARK} 的 {missing_index} 收盘值缺失'
        stamps = [(day, stamp) for day, stamp in stamps if day < missing_index]
        if not stamps:
            raise ledger.LedgerError(f'{message}，尚无法计算净值', 422)
        gaps.append(f'{message}，净值最晚截至此前一个交易日')

    holdings, previous = [], None
    for day, stamp in stamps:
        position = project(opening, [e for e in events if _counts_on(e, day, stamp)])
        flows = [e for e in events if (previous is None or previous < e.occurred_at) and e.occurred_at <= stamp
                 and _flow(e)]
        holdings.append((day, position, sum((_flow(e) for e in flows), Decimal(0)),
                         sum((_flow(e) * _session_left(e.occurred_at, day) for e in flows), Decimal(0))))
        previous = stamp
    codes = sorted({code for _, position, *_ in holdings for code in position.positions})
    closes = {code: fetch_closes(code, first_day - PRICE_LOOKBACK, today) for code in codes}

    # A held stock without a close is either suspended (keep its last close) or not yet published;
    # the series stops before the first unpublished close instead of filling it with an older one.
    cutoff = None
    for code in codes:
        missing = [day for day, position, *_ in holdings if code in position.positions and day not in closes[code]]
        if not missing:
            continue
        suspended = fetch_suspended(code, missing[0], missing[-1])
        if unpublished := [day for day in missing if day not in suspended]:
            gaps.append(f'{code} 的 {unpublished[0]} 收盘价尚未发布，净值截至此前一个交易日')
            cutoff = min(cutoff or unpublished[0], unpublished[0])

    days = []
    for day, position, flow, weighted_flow in holdings:
        if cutoff is not None and day >= cutoff:
            break
        assets = position.cash
        for code, shares in position.positions.items():
            price = _close_on_or_before(closes[code], day)
            if price is None:
                raise ledger.LedgerError(f'{code} 在 {day} 及之前 30 天没有收盘价，无法估值', 503)
            assets += price * shares
        days.append(account_nav.Day(date=day, total_assets=assets, flow=flow, weighted_flow=weighted_flow,
                                    benchmark=index[day]))
    if not days:
        raise ledger.LedgerError('持仓股票尚无开户日及之后的收盘价，收盘数据发布后再查', 422)
    first_funded = next((i for i, day in enumerate(days) if day.total_assets > 0), None)
    if first_funded is None:
        raise ledger.LedgerError('尚无总资产为正的收盘估值，无法建立净值基期', 422)
    label_since_start = '开户以来'
    if first_funded:
        days = days[first_funded:]
        label_since_start = '首个正资产收盘以来'
        gaps.append(f'开户后的零资产收盘不计收益；净值从 {days[0].date} 起算')
    try:
        rows = account_nav.series(days)
    except account_nav.NonPositiveExposure as exc:
        # Do not fabricate return for this day or chain subsequent closes over the gap.
        gaps.append(f'{exc}，净值截至此前一个交易日')
        rows = account_nav.series([day for day in days if day.date < exc.day])
    except ValueError as exc:
        raise ledger.LedgerError(str(exc), 422) from None

    as_of = rows[-1]['date']
    periods = []
    # A young account clips longer windows to the first comparable close; repeat a span only once.
    for label, since in ((label_since_start, rows[0]['date']), ('近1月', _month_ago(as_of)),
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
