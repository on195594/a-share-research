"""Research uses captured disclosures and reports. Ledger associations use the existing scoped book fixture."""
import json
import re
from html import unescape

import pytest

from a_share_research import overview, scans, service, template_filters
from a_share_research.account import cycle_analysis
from a_share_research.tools.registry import Context, invoke
from support.account_analysis import request, rewrite
from support.account_nav import evaluate as save_nav
from support.policy import run_monitor as save_monitor
from support.positions import batch, event, state, submit
from support.scans import CATALOG, priced, run_scan
from support.web import page_request


def html(response, status=200):
    assert response.status_code == status and response.media_type == "text/html"
    return unescape(response.body.decode())


@pytest.mark.wide_scan_scope
def test_owner_subscription_and_scan_routes_return_json_to_browser(web, owner_request, bound_book):
    subscriptions = scans.replace_subscriptions(web.conn, ['603606'], catalog=CATALOG)
    scan_id = scans.submit(web.conn)
    listing = web.subscription_detail(owner_request('/subscriptions'))
    weekly = web.weekly_scan_detail(owner_request('/scans/weekly'))
    scan = web.scan_detail(owner_request(f'/scans/{scan_id}'), scan_id)
    for response in (listing, weekly, scan):
        assert response.status_code == 200 and response.media_type == 'application/json'
    assert json.loads(listing.body) == subscriptions
    scope = json.loads(weekly.body)['scan_scope']
    assert {item['code'] for item in scope if 'holding' in item['origins']} == {'600001'}
    assert json.loads(scan.body) == scans.view(web.conn, scan_id)


def test_overview_display_formats():
    assert template_filters.shanghai_minute('2026-11-20 11:43:49.601366+08:00') == '2026-11-20 11:43'
    assert template_filters.shanghai_minute('2026-11-19T16:01:49Z') == '2026-11-20 00:01'
    assert template_filters.shanghai_minute('2026-11-01') == '2026-11-01'
    assert template_filters.decimal_format(1.266986874755609, '.2f') == '1.27'
    assert template_filters.decimal_format(0, '.2f') == '0.00'
    gap = {'field': 'industry_change_pct', 'minimum_action': 'supply a same-day industry comparison'}
    assert template_filters.monitor_gap(gap) == '行业涨跌幅 · 补充同一交易日的行业涨跌幅'
    assert template_filters.monitor_gap(dict(field='new_field', minimum_action='new hint')) == 'new_field · new hint'


def test_reopening_detaches_thesis_and_monitor_from_the_closed_cycle(ledger_monitor):
    configured = ledger_monitor
    (path, conn, principal, directory, sha), _, active, _ = configured
    book = (conn, principal, directory, sha)
    cycle_analysis.update(conn, principal, 'a', 'legacy:1', request(book, [rewrite(retire_l3_ids=[])]), directory)
    receipt = save_monitor(configured)
    ctx = Context(conn, path, principal, account_shadow_enabled=True)
    old = invoke(ctx, 'account_portfolio_overview', {})['data']['holdings'][0]
    assert old['research'] is None
    assert old['position']['theses'][0]['l1'] == rewrite()['l1']
    assert old['policy']['id'] == receipt['id']
    assert old['policy']['as_of'] == receipt['result']['as_of']
    assert (old['policy']['identity'], old['policy']['activation_id']) == (active['identity'], active['id'])

    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    profile = conn.execute("SELECT profile_identity FROM policy_v0_account_openings WHERE account_id='a'").fetchone()[0]
    buy = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    reopened = batch(book, [buy], version=1, shares=100, cash='10200',
                     entries={'0': dict(policy_identity=profile, framework='A通用', name='Structural reopened')})
    posted = submit(book, reopened)
    assert posted['status'] == 'posted'
    cycle_id = state(book)['projection']['cycles'][-1]['cycle_id']
    cycle_analysis.initialize(conn, principal, 'a', cycle_id, cycle_analysis.Initialize(
        expected_account_version=2, evidence_hashes=[sha]), directory)

    conn.execute('PRAGMA query_only=ON')
    portfolio = overview.portfolio_overview(conn)
    holding, = portfolio['holdings']
    current = invoke(ctx, 'research_company_overview', {'code': old['code']})['data']
    assert holding['position'] == current['position']
    assert holding['position']['cycle_id'] == cycle_id
    assert holding['position']['theses'] == [] and holding['policy'] is None
    assert current['held'] and current['position']['shares'] == 100
    assert portfolio['policy']['id'] == receipt['id'] and portfolio['policy']['account_version'] == 0
    assert portfolio['account']['version'] == 2
    assert any('NAV' in gap for gap in portfolio['gaps'])
    assert overview.monitor_detail(conn, receipt['id']) == receipt
    assert overview.receipt_detail(conn, portfolio['account']['last_receipt']['id']) == posted
    conn.execute('PRAGMA query_only=OFF')
    conn.execute('DELETE FROM account_source')
    for read, record_id in ((overview.monitor_detail, receipt['id']), (overview.receipt_detail, posted['receipt_id'])):
        with pytest.raises(service.RequestError, match='绑定账户'):
            read(conn, record_id)


def test_corrupt_monitor_is_a_gap_in_overviews_and_a_conflict_on_its_deep_link(ledger_monitor, web, owner_request):
    configured = ledger_monitor
    (_, conn, _, _, _), _, _, _ = configured
    receipt = save_monitor(configured)
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    conn.execute("UPDATE policy_evaluations SET capture_context_hash=? WHERE id=?", ('0' * 64, receipt['id']))

    portfolio = overview.portfolio_overview(conn)
    holding, = portfolio['holdings']
    company = overview.company_overview(conn, holding['code'])
    assert company['held'] and company['position'] == holding['position']
    assert portfolio['policy'] is None and company['policy'] is None
    assert company['account_gaps'] == portfolio['gaps']
    assert '政策采集来源哈希不一致' in company['account_gaps']
    assert '政策采集来源哈希不一致' not in company['gaps']

    path = f"/policy/evaluations/{receipt['id']}"
    assert '政策采集来源哈希不一致' in html(web.monitor_detail(owner_request(path), receipt['id']), 409)
    response = web.monitor_detail(owner_request(path, accept='application/json'), receipt['id'])
    assert response.status_code == 409 and json.loads(response.body)['detail'] == '政策采集来源哈希不一致'


def test_public_research_and_open_disclosures_survive_an_unavailable_account(web, owner_request):
    conn = web.conn
    priced(conn)
    code = '603606'
    scans.replace_subscriptions(conn, [code], catalog=CATALOG)
    scan = run_scan(conn)
    conn.execute('PRAGMA query_only=ON')
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        company = overview.company_overview(conn, code)
    finally:
        conn.set_trace_callback(None)
    assert sum('FROM scan_events e LEFT JOIN runs' in stmt for stmt in statements) == 1
    assert company['held'] is None and company['account_gaps'] == overview.portfolio_overview(conn)['gaps']
    assert not company['gaps'] and company['account_gaps']
    assert company['research']['path'] == f"/reports/{company['research']['report_id']}"
    current = service.company_card(conn, code)['current']
    assert current['last_close_date'] and current['current_zone']
    assert company['research']['current'] == {k: current[k] for k in ('last_close_date', 'current_zone')}
    pending = {e['id']: e['review']['state'] for e in scans.open_events(conn, code)}
    assert pending
    assert pending.items() <= {e['id']: e['review']['state'] for e in company['announcements'] if e['review']}.items()
    assert all(e['id'] in {row['id'] for row in scan['events'] if row['code'] == code}
               for e in company['announcements'])
    assert overview.portfolio_overview(conn)['holdings'] is None

    path = f'/stock/{code}/overview'
    page = html(web.company_overview(owner_request(path), code))
    research = company['research']
    announcement = company['announcements'][0]
    for value in (research['verdict'], research['summary'], research['current']['current_zone'],
                  research['current']['last_close_date'], research['zone_note'], research['period'], research['path'],
                  announcement['title'], announcement['source_url'], *company['account_gaps']):
        assert value in page
    assert f'href="{path}"' in html(web.stock_page(owner_request(f'/stock/{code}'), code))
    assert 'href="/portfolio/overview"' in html(web.home(owner_request('/')))


def test_account_gaps_do_not_flag_an_unheld_company_or_its_missing_report(bound_book, web, owner_request):
    database, conn, principal, _, _ = bound_book
    ctx = Context(conn, database, principal, account_shadow_enabled=True)
    for code, status in (('603606', 200), ('000963', 404)):
        data = invoke(ctx, 'research_company_overview', {'code': code})['data']
        assert data['held'] is False and data['gaps'] == []
        assert '暂无可查询的 NAV 区间凭证' in data['account_gaps']
        page = html(web.stock_page(owner_request(f'/stock/{code}'), code), status)
        assert f'href="/stock/{code}/overview"' not in page

    # A captured disclosure without a research card still needs review.
    code = '000963'
    scans.replace_subscriptions(conn, [code], catalog=CATALOG)
    run_scan(conn)
    stock = html(web.stock_page(owner_request(f'/stock/{code}'), code), 404)
    assert f'href="/stock/{code}/overview"' in stock and '总览有待核对项' in stock


def test_held_company_without_research_links_to_saved_records_and_evidence(ledger_monitor, web, owner_request):
    configured = ledger_monitor
    (database, conn, principal, directory, sha), _, _, _ = configured
    book = conn, principal, directory, sha
    posted = submit(book, batch(book, [], shares=100, cash='10000'))
    assert posted['status'] == 'posted'
    rejected = submit(book, batch(book, [], shares=100, cash='10000', version=1))
    assert rejected['status'] == 'conflict'
    evaluation = save_monitor(configured)
    web.conn.execute('PRAGMA query_only=ON')
    ctx = Context(conn, database, principal, account_shadow_enabled=True)
    view = invoke(ctx, 'account_portfolio_overview', {})['data']
    holding, = view['holdings']
    code = holding['code']
    portfolio_path = '/portfolio/overview'
    company_path = f'/stock/{code}/overview'
    evaluation_path = holding['policy']['path']
    receipt_path = view['account']['last_receipt']['path']
    latest = view['account']['latest_receipt']
    assert latest['id'] == rejected['receipt_id'] and latest['id'] != posted['receipt_id']

    for endpoint, path, args, data in (
        (web.portfolio_overview, portfolio_path, (), view),
        (web.company_overview, company_path, (code,), invoke(ctx, 'research_company_overview', {'code': code})['data']),
        (web.monitor_detail, evaluation_path, (evaluation['id'],), evaluation),
        (web.receipt_detail, receipt_path, (posted['receipt_id'],), posted),
    ):
        anonymous = endpoint(page_request(path), *args)
        assert anonymous.status_code == 303 and anonymous.headers['location'] == '/login'
        response = endpoint(owner_request(path, accept='application/json'), *args)
        assert response.media_type == 'application/json' and json.loads(response.body) == data
    assert web.portfolio_overview(owner_request(portfolio_path, accept='application/json, */*')).media_type == 'application/json'

    portfolio = html(web.portfolio_overview(owner_request(portfolio_path)))
    company = html(web.company_overview(owner_request(company_path), code))
    assert f'href="{company_path}#evidence-{code}"' in portfolio
    assert f'id="evidence-{code}"' in company
    for page in (portfolio, company):
        for value in (code, evaluation_path, receipt_path, latest['path'], *holding['gaps']):
            assert value in page
    assert evaluation['identity'] in portfolio and evaluation['result']['as_of'][:10] in portfolio
    assert '前收盘价 · 补充有效的前收盘价' in company
    stock = html(web.stock_page(owner_request(f'/stock/{code}'), code), 404)
    assert f'href="{company_path}"' in stock
    monitor = html(web.monitor_detail(owner_request(evaluation_path), evaluation['id']))
    assert evaluation['request_id'] in monitor and evaluation['identity'] in monitor
    receipt = html(web.receipt_detail(owner_request(latest['path']), latest['id']))
    assert rejected['request_hash'] in receipt and sha in receipt
    for page, original in ((monitor, evaluation), (receipt, rejected)):
        assert json.loads(re.search(r'<pre[^>]*>(.*?)</pre>', page, re.S)[1]) == original


def test_nav_deep_link_opens_the_saved_after_cost_comparison(nav_book, bind_account, web, owner_request):
    bind_account(nav_book['conn'], 'a')
    portfolio_path = '/portfolio/overview'
    missing = '暂无可查询的 NAV 区间凭证'
    assert missing in html(web.portfolio_overview(owner_request(portfolio_path)))
    saved = save_nav(nav_book)
    web.conn.execute('PRAGMA query_only=ON')
    sha = saved['artifact_sha256']
    ctx = Context(nav_book['conn'], nav_book['path'], nav_book['principal'], account_shadow_enabled=True)
    reference = invoke(ctx, 'account_portfolio_overview', {})['data']['nav_interval']
    assert reference['artifact_sha256'] == sha
    nav_path = reference['path']
    page = html(web.portfolio_overview(owner_request(portfolio_path)))
    assert missing not in page and f'href="{nav_path}"' in page
    anonymous = web.nav_detail(page_request(nav_path), sha)
    assert anonymous.status_code == 303 and anonymous.headers['location'] == '/login'
    detail = html(web.nav_detail(owner_request(nav_path), sha))
    for value in (sha, saved['basis']['benchmark'], saved['inputs']['start']['as_of'][:10],
                  saved['inputs']['end']['as_of'][:10], saved['calculation']['end_nav'],
                  saved['calculation']['benchmark_return'], saved['calculation']['excess_percentage_points']):
        assert value in detail


def test_overview_uses_ledger_projection_while_policy_capture_verifies_evidence(ledger_monitor):
    configured = ledger_monitor
    from a_share_research.policy.registry import PolicyError

    (_, conn, _, directory, sha), _, _, _ = configured
    portfolio = overview.portfolio_overview(conn)
    holding, = portfolio['holdings']
    company = overview.company_overview(conn, holding['code'])
    evidence = directory / sha
    original = evidence.read_bytes()
    evidence.write_bytes(b'altered evidence fixture')

    assert overview.portfolio_overview(conn) == portfolio
    assert overview.company_overview(conn, holding['code']) == company
    with pytest.raises(PolicyError, match='证据无法核对'):
        save_monitor(configured)

    moved = evidence.rename(directory / 'missing-fixture')
    assert overview.portfolio_overview(conn)['holdings'] is None
    moved.rename(evidence)
    evidence.write_bytes(original)
    conn.execute("UPDATE account_position_books SET version=version+1 WHERE account_id='a'")
    stale = overview.company_overview(conn, holding['code'])
    assert stale['held'] is None and stale['account_gaps']
