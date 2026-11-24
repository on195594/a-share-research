"""Daily NAV from a structural ledger and real 2026-11 TuShare closes.

Closes and H00906.CSI points are captured responses; positions, cash and flows are explicit
structural fixtures and never establish real account performance.
"""
import asyncio
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from a_share_research import api_keys, db
from a_share_research.account import evidence, ledger, nav
from a_share_research.account.reconciliation import Event, Opening, Snapshot, identity, project
from a_share_research.core import account_nav
from a_share_research.core.market_data import MarketDataResult
from a_share_research.core.providers import TushareMarketDataProvider
from a_share_research.tools.registry import Context, ToolError, invoke
from a_share_research.tools_client import ToolCallError

CAPTURE = json.loads((Path(__file__).parent / 'data/account-nav-closes-202611.json').read_text())
INDEX = {date.fromisoformat(k): Decimal(str(v)) for k, v in CAPTURE['index'].items()}
CLOSES = {code: {date.fromisoformat(k): Decimal(str(v)) for k, v in rows.items()}
          for code, rows in CAPTURE['closes'].items()}
TODAY = date(2026, 11, 24)


@pytest.fixture
def book(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'NAV structural', role='assistant'))
    directory = tmp_path / 'account-evidence'
    sha = evidence.store(directory, b'Structural NAV fixture, NOT broker evidence or actual performance')
    ledger.initialize(conn, principal, 'a', Opening(positions={'601600': 1000}, cash='10000'),
                      datetime.fromisoformat('2026-11-12T15:00:00+08:00'), directory, sha, tolerance=Decimal('0'))
    yield dict(conn=conn, principal=principal, directory=directory, sha=sha, path=tmp_path / 'app.db')
    conn.close()


def post(book, events, voids=()):
    state = ledger.state(book['conn'], book['principal'], 'a')
    after = project(Opening.model_validate(state['projection']), events)
    for event_id in voids:  # Voiding a deposit removes its cash from the projection.
        voided = Event.model_validate_json(book['conn'].execute(
            'SELECT event FROM account_journal WHERE id=?', (event_id,)).fetchone()[0])
        after = after.model_copy(update={'cash': after.cash - voided.cash})
    snapshot = Snapshot(positions=after.positions, cash=after.cash, total_assets=after.cash + sum(
        Decimal(1) * n for n in after.positions.values()), market_values={c: Decimal(1) * n for c, n in after.positions.items()})
    receipt = ledger.submit(book['conn'], book['principal'], 'a', ledger.Batch(
        expected_version=state['version'], events=events, void_event_ids=list(voids),
        evidence_hashes=[book['sha']], snapshot=snapshot), book['directory'])
    assert receipt['status'] == 'posted', receipt
    return book['conn'].execute('SELECT max(id) FROM account_journal').fetchone()[0]


def daily(book, closes=CLOSES, suspended=None, **kwargs):
    return nav.daily(book['conn'], book['principal'], 'a', today=TODAY,
                     fetch_index=lambda s, e: {d: v for d, v in INDEX.items() if s <= d <= e},
                     fetch_closes=lambda code, s, e: {d: v for d, v in closes[code].items() if s <= d <= e},
                     fetch_suspended=lambda code, s, e: {d for d in (suspended or {}).get(code, ()) if s <= d <= e},
                     **kwargs)


def test_unit_nav_ignores_external_cash_and_keeps_price_moves():
    days = [account_nav.Day(date=date(2026, 11, 12), total_assets=Decimal(1000), benchmark=Decimal(1)),
            account_nav.Day(date=date(2026, 11, 13), total_assets=Decimal(1100), benchmark=Decimal(1)),
            # Holdings doubled to 2200 before the close; the 1210 deposit adds units, not NAV.
            account_nav.Day(date=date(2026, 11, 16), total_assets=Decimal(3410), flow=Decimal(1210),
                            benchmark=Decimal(2))]
    rows = account_nav.series(days)
    assert [row['nav'] for row in rows] == [Decimal(1), Decimal('1.1'), Decimal('2.2')]
    result = account_nav.period(rows, date(2026, 11, 14), date(2026, 11, 16))
    assert result['start'] == date(2026, 11, 13) and result['account_return'] == 1
    assert result['excess_percentage_points'] == 0
    with pytest.raises(ValueError, match='起点须不晚于终点'):  # Both map to the 11-13 close.
        account_nav.period(rows, date(2026, 11, 15), date(2026, 11, 14))


def test_flows_earn_for_the_part_of_the_session_they_were_in_the_account():
    # Arithmetic case from review: 100000 arrives before the open, is invested, and the day ends +4970.
    def day(n, assets, flow=0, weighted=0):
        return account_nav.Day(date=date(2026, 11, n), total_assets=Decimal(assets), flow=Decimal(flow),
                               weighted_flow=Decimal(weighted), benchmark=Decimal(1))
    rows = account_nav.series([day(12, 10000), day(13, 114970, 100000, 100000)])
    assert abs(rows[-1]['nav'] - 1 - Decimal(4970) / Decimal(110000)) < Decimal('1e-20')
    # A loss on the new money no longer makes the pre-flow value negative.
    assert account_nav.series([day(12, 10000), day(13, 99970, 100000, 100000)])[-1]['nav'] < 1
    # Emptying the account keeps the NAV; money coming back resumes from it.
    rows = account_nav.series([day(12, 10000), day(13, 11000), day(16, 0, -11000, -11000),
                               day(17, 0), day(18, 5000, 5000, 5000)])
    assert [row['nav'] for row in rows[2:]] == [Decimal('1.1')] * 3
    with pytest.raises(ValueError, match='期初资金为零'):
        account_nav.series([day(12, 10000), day(13, 0, -10000, -10000), day(16, 50)])


def test_session_share_counts_only_trading_minutes():
    def left(text, day=date(2026, 11, 13)):
        return nav._session_left(datetime.fromisoformat(text), day)
    assert left('2026-11-12T16:00:00+08:00') == left('2026-11-13T09:00:00+08:00') == 1
    assert [left(f'2026-11-13T{t}+08:00') for t in ('10:30:00', '12:00:00', '14:00:00', '15:00:00')] == [
        Decimal('0.75'), Decimal('0.5'), Decimal('0.25'), 0]


def test_ledger_events_and_real_closes_drive_the_daily_series(book):
    buy = Event(kind='buy', code='601899', shares=300, price='26.50', fees='5.07',
                occurred_at='2026-11-13T13:49:17+08:00', broker_id='buy')
    # Exactly at the close: still that day's flow, matching the projection's inclusive cut-off.
    deposit = Event(kind='deposit', cash='5000', occurred_at='2026-11-18T15:00:00+08:00', broker_id='in')
    # After the close: the withdrawal belongs to the next trading close (11-23, skipping the weekend).
    withdrawal = Event(kind='withdrawal', cash='2000', occurred_at='2026-11-20T16:00:00+08:00', broker_id='out')
    wrong = Event(kind='deposit', cash='99999', occurred_at='2026-11-19T10:00:00+08:00', broker_id='wrong')
    post(book, [buy])
    post(book, [deposit, wrong])
    wrong_id = book['conn'].execute("SELECT id FROM account_journal WHERE event LIKE '%\"wrong\"%'").fetchone()[0]
    post(book, [withdrawal], voids=[wrong_id])

    result = daily(book)
    days = {row['date']: row for row in result['days']}
    assert result['as_of'] == '2026-11-23' and result['gaps'] == [] and list(days)[0] == '2026-11-12'
    assert days['2026-11-12']['total_assets'] == str(Decimal('10000') + 1000 * CLOSES['601600'][date(2026, 11, 12)])
    cash = Decimal('10000') - Decimal('7950') - Decimal('5.07')
    for day in ('2026-11-13', '2026-11-17'):
        stamp = date.fromisoformat(day)
        assert days[day]['total_assets'] == str(cash + 1000 * CLOSES['601600'][stamp] + 300 * CLOSES['601899'][stamp])
    assert [days[d]['flow'] for d in ('2026-11-18', '2026-11-19', '2026-11-20', '2026-11-23')] == ['5000.00', '0.00', '0.00', '-2000.00']
    # The after-close withdrawal left before the 11-23 open, so it never earned that day.
    holdings_23 = 1000 * CLOSES['601600'][date(2026, 11, 23)] + 300 * CLOSES['601899'][date(2026, 11, 23)]
    total_20 = Decimal(days['2026-11-20']['total_assets'])
    expected_23 = (Decimal(days['2026-11-23']['total_assets']) + 2000 - total_20) / (total_20 - 2000)
    assert abs(Decimal(days['2026-11-23']['nav']) / Decimal(days['2026-11-20']['nav']) - 1 - expected_23) < Decimal('2e-6')
    assert Decimal(days['2026-11-23']['total_assets']) == cash + 5000 - 2000 + holdings_23

    # The deposit day's NAV equals the price-only move of the pre-deposit holdings.
    def holdings(day):
        return 1000 * CLOSES['601600'][day] + 300 * CLOSES['601899'][day]
    expected = (cash + holdings(date(2026, 11, 18))) / (cash + holdings(date(2026, 11, 17)))
    ratio = Decimal(days['2026-11-18']['nav']) / Decimal(days['2026-11-17']['nav'])
    assert abs(ratio - expected) < Decimal('2e-6')
    since_open, = result['periods']  # One month and the year both clip to the opening close.
    assert since_open['label'] == '开户以来' and since_open['end'] == '2026-11-23'
    assert Decimal(since_open['benchmark_return']) == (INDEX[date(2026, 11, 23)] / INDEX[date(2026, 11, 12)] - 1).quantize(Decimal('1e-6'))

    custom = daily(book, start=date(2026, 11, 15), end=date(2026, 11, 20))['periods'][-1]
    assert (custom['label'], custom['start'], custom['end']) == ('指定区间', '2026-11-13', '2026-11-20')
    with pytest.raises(ledger.LedgerError, match='可查区间为 2026-11-12 至 2026-11-23'):
        daily(book, end=date(2026, 11, 1))


def test_series_stops_before_an_unpublished_close_and_carries_suspensions(book):
    post(book, [Event(kind='buy', code='601899', shares=300, price='26.50', fees='5.07',
                      occurred_at='2026-11-13T13:49:17+08:00', broker_id='buy')])
    # Variant: drop both holdings' final close, as before TuShare publishes it; each is reported.
    trimmed = {code: {d: v for d, v in rows.items() if d < date(2026, 11, 23)} for code, rows in CLOSES.items()}
    result = daily(book, closes=trimmed)
    assert result['as_of'] == '2026-11-20'
    assert result['gaps'] == [f'{code} 的 2026-11-23 收盘价尚未发布，净值截至此前一个交易日' for code in ('601600', '601899')]
    # Variant: 601600 suspended from 11-18 to the latest close keeps its 11-17 close to the end.
    halted = CLOSES | {'601600': {d: v for d, v in CLOSES['601600'].items() if d < date(2026, 11, 18)}}
    stopped = [d for d in INDEX if d >= date(2026, 11, 18)]
    result = daily(book, closes=halted, suspended={'601600': stopped})
    days = {row['date']: row for row in result['days']}
    assert result['as_of'] == '2026-11-23' and result['gaps'] == []
    assert Decimal(days['2026-11-23']['total_assets']) == Decimal('10000') - Decimal('7955.07') + 1000 * CLOSES['601600'][
        date(2026, 11, 17)] + 300 * CLOSES['601899'][date(2026, 11, 23)]


def test_same_second_events_replay_in_journal_order(book):
    # Deposit then buy in one second; pick broker ids whose business ids sort the buy first.
    for n in range(100):
        deposit = Event(kind='deposit', cash='20000', occurred_at='2026-11-13T10:00:00+08:00', broker_id=f'in{n}')
        buy = Event(kind='buy', code='601899', shares=1000, price='26.50', occurred_at='2026-11-13T10:00:00+08:00',
                    broker_id=f'buy{n}')
        if identity(buy, 'a') < identity(deposit, 'a'):
            break
    post(book, [deposit, buy])
    assert daily(book)['as_of'] == '2026-11-23'


def test_dividend_booked_after_the_close_counts_on_its_ex_date(book):
    post(book, [Event(kind='dividend', code='601600', cash='120', occurred_at='2026-11-18T16:30:00+08:00',
                      broker_id='div')])
    days = {row['date']: row for row in daily(book)['days']}
    assert Decimal(days['2026-11-18']['total_assets']) == Decimal('10120') + 1000 * CLOSES['601600'][date(2026, 11, 18)]
    assert days['2026-11-18']['flow'] == '0.00'


def _frame(rows):
    return pd.DataFrame([{'date': d.isoformat(), 'close': float(v)} for d, v in sorted(rows.items())])


@pytest.fixture
def tool(book, bind_account, monkeypatch):
    bind_account(book['conn'], 'a')

    def bars(rows, source):
        def fetch(provider, code, start, end):
            selected = {d: v for d, v in rows(code).items() if start <= d.isoformat() <= end}
            return MarketDataResult(_frame(selected), 'ok', source, datetime.now(db.SHANGHAI).isoformat())
        return fetch

    monkeypatch.setattr(TushareMarketDataProvider, 'fetch_index_bars', bars(lambda code: INDEX, 'tushare.index_daily'))
    monkeypatch.setattr(TushareMarketDataProvider, 'fetch_daily_bars', bars(lambda code: CLOSES[code], 'tushare.daily'))
    monkeypatch.setattr(TushareMarketDataProvider, 'fetch_suspended_days', lambda provider, code, start, end: MarketDataResult(
        pd.DataFrame(columns=['trade_date']), 'ok', 'tushare.suspend_d', datetime.now(db.SHANGHAI).isoformat()))
    ctx = Context(book['conn'], book['path'], book['principal'], account_shadow_enabled=True)
    return lambda arguments: invoke(ctx, 'account_nav', arguments)['data']


def test_tool_returns_series_and_rejects_ranges_before_the_opening(tool):
    result = tool({'start': '2026-11-13'})
    assert result['as_of'] == '2026-11-23' and result['periods'][-1]['start'] == '2026-11-13'
    with pytest.raises(ToolError) as error:
        tool({'end': '2026-11-01'})
    assert error.value.code == 'invalid_arguments' and '可查区间' in str(error.value)


def test_web_page_renders_the_tool_result_and_reports_tool_errors(web, tool, owner_request, monkeypatch):
    class BoundTools:
        async def call(self, name, arguments, *, request_id=None, read_only=False):
            assert name == 'account_nav' and read_only
            try:
                return tool(arguments)
            except ToolError as exc:
                raise ToolCallError('business', str(exc), exc.code) from exc

    monkeypatch.setattr(web.ToolsClient, 'from_settings', lambda settings: BoundTools())

    async def visit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=web.settings.base_url,
                                     cookies=owner_request('/account/nav').cookies) as browser:
            page = (await browser.get('/account/nav')).text
            assert '开户以来' in page and '2026-11-12 至' in page and '逐日净值' in page
            assert '<a href="/portfolio/overview" aria-current="page">我的组合</a>' in page
            custom = (await browser.get('/account/nav', params={'start': '2026-11-17', 'end': '2026-11-20'})).text
            assert '指定区间' in custom and '2026-11-17 至 2026-11-20' in custom
            failed = (await browser.get('/account/nav', params={'end': '2026-11-01'})).text
            assert '可查区间为 2026-11-12' in failed and 'name="end" type="date" value="2026-11-01"' in failed
            malformed = (await browser.get('/account/nav', params={'start': ' bad '})).text
            assert 'YYYY-MM-DD' in malformed and 'href="/account/nav">查看默认区间' in malformed
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=web.settings.base_url) as anonymous:
            assert (await anonymous.get('/account/nav')).headers['location'] == '/login'

    asyncio.run(visit())


@pytest.mark.parametrize('path,current', [('/account/nav', '/portfolio/overview'),
                                          ('/policy/evaluations/3', '/portfolio/overview'),
                                          ('/policy', '/policy'), ('/batches/7', '/batches'),
                                          ('/policy-history', None), ('/', None)])
def test_menu_marks_the_section_a_page_belongs_to(web, path, current):
    from types import SimpleNamespace
    html = web.templates.env.get_template('base.html').render(
        request=SimpleNamespace(url=SimpleNamespace(path=path)), csrf='x', asset_version='1')
    marked = [href for href in ('/portfolio/overview', '/batches', '/research-library', '/policy', '/connections')
              if f'<a href="{href}" aria-current="page">' in html]
    assert marked == ([current] if current else [])
