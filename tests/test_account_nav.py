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
from a_share_research.account.reconciliation import Event, Opening, Snapshot, project
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


def daily(book, closes=CLOSES, **kwargs):
    return nav.daily(book['conn'], book['principal'], 'a', today=TODAY,
                     fetch_index=lambda s, e: {d: v for d, v in INDEX.items() if s <= d <= e},
                     fetch_closes=lambda code, s, e: {d: v for d, v in closes[code].items() if s <= d <= e},
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
    with pytest.raises(ValueError, match='出入金前总资产不为正'):
        account_nav.series(days[:1] + [account_nav.Day(date=date(2026, 11, 13), total_assets=Decimal(5),
                                                       flow=Decimal(5), benchmark=Decimal(1))])


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


def test_series_stops_at_the_last_close_every_holding_has(book):
    # Variant: drop the final close of one holding, as before TuShare publishes it.
    trimmed = CLOSES | {'601600': {d: v for d, v in CLOSES['601600'].items() if d < date(2026, 11, 23)}}
    result = daily(book, closes=trimmed)
    assert result['as_of'] == '2026-11-20'
    assert result['gaps'] == ['601600 收盘价只到 2026-11-20，净值截至该日']
    # A suspension inside the series keeps the previous close instead.
    suspended = CLOSES | {'601600': {d: v for d, v in CLOSES['601600'].items() if d != date(2026, 11, 17)}}
    days = {row['date']: row for row in daily(book, closes=suspended)['days']}
    assert days['2026-11-17']['total_assets'] == str(Decimal('10000') + 1000 * CLOSES['601600'][date(2026, 11, 16)])


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
        async def call(self, name, arguments, *, request_id=None):
            assert name == 'account_nav'
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
            custom = (await browser.get('/account/nav', params={'start': '2026-11-17', 'end': '2026-11-20'})).text
            assert '指定区间' in custom and '2026-11-17 至 2026-11-20' in custom
            failed = (await browser.get('/account/nav', params={'end': '2026-11-01'})).text
            assert '可查区间为 2026-11-12' in failed
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=web.settings.base_url) as anonymous:
            assert (await anonymous.get('/account/nav')).headers['location'] == '/login'

    asyncio.run(visit())
