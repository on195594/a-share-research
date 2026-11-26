"""Research uses captured disclosures and reports. Ledger associations use the existing scoped book fixture."""
import json
import re
from html import unescape

import pytest

from a_share_research import db, integrity, overview, reading, scans, service, template_filters
from a_share_research.account import cycle_analysis
from a_share_research.policy import account_input, registry as policies
from a_share_research.tools.registry import Context, invoke
from support.account_analysis import request, rewrite
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


def test_monitor_lines_render_saved_english_details_and_collapse_repeated_gaps():
    # Escalation details and gaps as saved by production monitor evaluation 519 (2026-11-20).
    gap = {'field': 'industry_change_pct', 'minimum_action': 'supply a same-day industry comparison'}
    policy = {
        'holdings': [{'code': '002050', 'name': '三花智控'}, {'code': '601899', 'name': '紫金矿业'},
                     {'code': '603606', 'name': '东方电缆'}],
        'escalations': [
            {'code': '002050', 'reason_code': 'daily_drop', 'detail': 'daily change -3.50% is at or below -3%'},
            {'code': '601899', 'reason_code': 'l3_candidate', 'detail': 'L3 state requires verification (versioned)'},
            {'code': '603606', 'reason_code': 'l3_watch', 'detail':
             'L3 condition 35 is watch (as_of 2026-10-30, next_review 2027-04-30); no new risk'},
            {'code': None, 'reason_code': 'risk_budget_exceeded', 'detail':
             'scope=portfolio; amount=81234.5 CNY; principal=unknown; unrealized_gain=unknown; ratio=8.4%; '
             'limit=8%; denominator=967078 CNY; denominator_source=explicit; account_scope=all'},
            {'code': '601899', 'reason_code': 'new_reason', 'detail': 'unmapped detail'},
        ],
        'data_gaps': [dict(gap, code=code) for code in ('002050', '601899', '603606')],
    }
    assert template_filters.monitor_lines(policy) == [
        ('三花智控', '当日涨跌 -3.50%，已到 -3% 提醒线'),
        ('紫金矿业', 'L3 持仓论点待核实：有条件尚待核实'),
        ('东方电缆', 'L3 条件 #35 观察中（资料截至 2026-10-30，下次复核 2027-04-30），无新增风险'),
        ('', '组合止损参考风险 81234.5 元，占 8.4%，超过上限 8%'),
        ('紫金矿业', 'unmapped detail'),
        ('三花智控、紫金矿业、东方电缆', '行业涨跌幅 · 补充同一交易日的行业涨跌幅'),
    ]
    # Overview read models carry no holdings list; lines fall back to codes, and own_code drops self-prefix.
    assert template_filters.monitor_lines({'escalations': [], 'data_gaps': [dict(gap, code='002050')]}) == [
        ('002050', '行业涨跌幅 · 补充同一交易日的行业涨跌幅')]
    assert template_filters.monitor_lines({'escalations': [], 'data_gaps': [dict(gap, code='002050')]}, '002050') == [
        ('', '行业涨跌幅 · 补充同一交易日的行业涨跌幅')]

    # Tier gaps recorded as both escalation and data_gap collapse; P3, cache, alert and attribution render in Chinese.
    rich = {
        'holdings': [{'code': '002050', 'name': '三花智控'}],
        'escalations': [
            {'code': '002050', 'reason_code': 'tier_review_due',
             'detail': 'verify quarterly/interim L3 evidence and three-star timing for path A'},
            {'code': '002050', 'reason_code': 'alert_review_due', 'detail': 'alert price_stop2_liquidity_defer is due'},
            {'code': '002050', 'reason_code': 'risk_budget_exceeded',
             'detail': 'scope=position; amount=12000 CNY; principal=8000 CNY; unrealized_gain=4000 CNY; '
                       'ratio=2.4%; limit=2%; denominator=500000 CNY'},
        ],
        'data_gaps': [
            {'code': '002050', 'field': 'tier',
             'minimum_action': 'verify quarterly/interim L3 evidence and three-star timing for path A'},
            {'code': '002050', 'field': 'p3_liquidity_gate', 'minimum_action': 'complete P3 review: quote_untradeable'},
            {'code': None, 'field': 'industry_context', 'minimum_action': 'restore same-industry context (CACHE_STALE)'},
        ],
        'review_prompts': [
            {'code': '002050', 'detail': '002050 收盘价 25.00 处于测算透支区'},
            {'code': None, 'detail': '论点卡状态未知'},
        ],
    }
    assert template_filters.monitor_lines(rich) == [
        ('三花智控', '分档退出条件 · 核对路径 A 的季报或中报 L3 证据及三星时点'),
        ('三花智控', '预警「第二档止损流动性延迟复核」已到复核日'),
        ('三花智控', '单只止损参考风险 12000 元，占 2.4%，超过上限 2%（其中本金风险 8000 元、浮盈风险 4000 元）'),
        ('三花智控', 'P3 流动性复核 · 完成 P3 流动性复核（个股当前不可交易）'),
        ('', '行业对照资料 · 补充同行业对照资料（缓存已过期）'),
    ]
    assert template_filters.monitor_prompts(rich) == [
        ('三花智控', '收盘价 25.00 处于测算透支区'),
        ('', '论点卡状态未知'),
    ]


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
    assert not any('NAV' in gap for gap in portfolio['gaps'])
    assert overview.monitor_detail(conn, receipt['id']) == receipt
    assert overview.receipt_detail(conn, portfolio['account']['last_receipt']['id']) == posted
    conn.execute('PRAGMA query_only=OFF')
    conn.execute('DELETE FROM account_source')
    for read, record_id in ((overview.monitor_detail, receipt['id']), (overview.receipt_detail, posted['receipt_id'])):
        with pytest.raises(service.RequestError, match='绑定账户'):
            read(conn, record_id)


def test_reopening_without_v0_framework_keeps_positions_readable_until_void_and_repost(ledger_monitor, tmp_path):
    configured = ledger_monitor
    (path, conn, principal, directory, sha), _, _, _ = configured
    book = (conn, principal, directory, sha)
    ctx = Context(conn, path, principal, account_shadow_enabled=True)
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    buy = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    bare = batch(book, [buy], version=1, shares=100, cash='10200')
    posted = invoke(ctx, 'account_submit', {'batch': bare.model_dump(mode='json')})['data']
    assert posted['status'] == 'posted' and posted['version'] == 2
    cycle = state(book)['projection']['cycles'][-1]
    assert cycle['policy_identity'] is None and cycle['shares'] == 100

    assert integrity.check(path, tmp_path / 'pdf')[0] == []
    current = invoke(ctx, 'account_current', {})['data']
    assert current['codes'] == ['600001']
    gap, = current['policy_gaps']
    assert 'v0 政策框架待补' in gap and '600001' in gap and cycle['cycle_id'] in gap
    assert overview.holdings_snapshot(conn, '600001', frozen_at=db.now())['policy_gaps'] == [gap]
    assert overview.holdings_snapshot(conn, '603606', frozen_at=db.now())['policy_gaps'] == []
    portfolio = overview.portfolio_overview(conn)
    holding, = portfolio['holdings']
    assert holding['position']['cycle_id'] == cycle['cycle_id'] and holding['position']['shares'] == 100
    assert holding['position']['cost_price'] == '10' and holding['position']['buy_date'] == '2026-11-03'
    assert holding['position']['revision_id'] is None and holding['position']['theses'] == []
    assert gap in portfolio['gaps']
    company = invoke(ctx, 'research_company_overview', {'code': '600001'})['data']
    assert company['held'] and company['position'] == holding['position'] and gap in company['account_gaps']
    with pytest.raises(policies.PolicyError, match='v0 政策框架待补') as refused:
        save_monitor(configured)
    assert refused.value.status == 409 and cycle['cycle_id'] in str(refused.value)

    profile = conn.execute("SELECT profile_identity FROM policy_v0_account_openings WHERE account_id='a'").fetchone()[0]
    complete = {'0': dict(policy_identity=profile, framework='A通用', name='Structural reopened')}
    backfill = invoke(ctx, 'account_submit', {'batch': batch(book, [buy], version=2, shares=100, cash='10200',
                                                              entries=complete).model_dump(mode='json')})['data']
    assert backfill['status'] == 'conflict' and state(book)['version'] == 2
    buy_id, = conn.execute('SELECT max(id) FROM account_journal').fetchone()
    corrected = batch(book, [buy], version=2, shares=100, cash='10200', entries=complete, voids=[buy_id])
    assert invoke(ctx, 'account_submit', {'batch': corrected.model_dump(mode='json')})['data']['status'] == 'posted'
    cycle_id = state(book)['projection']['cycles'][-1]['cycle_id']
    cycle_analysis.initialize(conn, principal, 'a', cycle_id, cycle_analysis.Initialize(
        expected_account_version=3, evidence_hashes=[sha]), directory)
    assert invoke(ctx, 'account_current', {})['data']['policy_gaps'] == []
    receipt = save_monitor(configured)
    assert receipt['capture_context']['source']['account_version'] == 3
    assert integrity.check(path, tmp_path / 'pdf')[0] == []


def test_new_cycle_without_analysis_names_the_initialization_tool(ledger_monitor):
    (path, conn, principal, directory, sha), _, _, calls = ledger_monitor
    book = (conn, principal, directory, sha)
    ctx = Context(conn, path, principal, account_shadow_enabled=True)
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    profile = conn.execute("SELECT profile_identity FROM policy_v0_account_openings WHERE account_id='a'").fetchone()[0]
    buy = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    complete = {'0': dict(policy_identity=profile, framework='A通用', name='Structural reopened')}
    assert submit(book, batch(book, [buy], version=1, shares=100, cash='10200', entries=complete))['status'] == 'posted'
    cycle_id = state(book)['projection']['cycles'][-1]['cycle_id']

    gap, = invoke(ctx, 'account_current', {})['data']['policy_gaps']
    for expected in ('新周期尚未建立账户周期分析', '600001', cycle_id,
                     'account_cycle_analysis_initialize', '无需作废成交'):
        assert expected in gap
    portfolio = overview.portfolio_overview(conn)
    assert portfolio['gaps'].count(gap) == 1
    holding, = portfolio['holdings']
    assert holding['position']['revision_id'] is None and gap not in holding['gaps']
    company = overview.company_overview(conn, '600001')
    assert company['account_gaps'].count(gap) == 1 and gap not in company['gaps']
    with pytest.raises(account_input.AnalysisBaselineGap):
        account_input.read(conn, path.parent, conn.execute(
            'SELECT mode,source_sha256,updated_at,watermark FROM account_source').fetchone())
    with pytest.raises(policies.PolicyError) as refused:
        save_monitor(ledger_monitor)
    assert refused.value.status == 409 and str(refused.value) == gap and calls == []

    cycle_analysis.initialize(conn, principal, 'a', cycle_id, cycle_analysis.Initialize(
        expected_account_version=2, evidence_hashes=[sha]), directory)
    assert invoke(ctx, 'account_current', {})['data']['policy_gaps'] == []
    assert save_monitor(ledger_monitor)['capture_context']['source']['account_version'] == 2


def test_same_instant_open_and_add_recover_only_when_voided_and_reposted_together(ledger_monitor):
    (_, conn, principal, directory, sha), _, _, _ = ledger_monitor
    book = (conn, principal, directory, sha)
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    buys = [event(price='10', broker_id=f'structural-{name}', occurred_at='2026-11-03T10:00:00+08:00')
            for name in ('same-instant-open', 'same-instant-add')]
    assert submit(book, batch(book, buys, version=1, cash='9200'))['status'] == 'posted'
    with pytest.raises(policies.PolicyError, match='若同成交时刻已有后续加仓流水.*一并作废重提'):
        save_monitor(ledger_monitor)

    profile = conn.execute("SELECT profile_identity FROM policy_v0_account_openings WHERE account_id='a'").fetchone()[0]
    complete = {'0': dict(policy_identity=profile, framework='A通用', name='Structural reopened')}
    opening_id, addition_id = [row[0] for row in conn.execute('SELECT id FROM account_journal ORDER BY id')][-2:]
    alone = batch(book, buys[:1], version=2, cash='9200', entries=complete, voids=[opening_id])
    assert submit(book, alone)['status'] == 'conflict' and state(book)['version'] == 2
    together = batch(book, buys, version=2, cash='9200', entries=complete, voids=[opening_id, addition_id])
    assert submit(book, together)['status'] == 'posted'
    cycle = state(book)['projection']['cycles'][-1]
    assert cycle['shares'] == 200 and cycle['cycle_id'] == 'event:' + conn.execute(
        'SELECT business_id FROM account_journal WHERE id=?', (opening_id,)).fetchone()[0]
    cycle_analysis.initialize(conn, principal, 'a', cycle['cycle_id'], cycle_analysis.Initialize(
        expected_account_version=3, evidence_hashes=[sha]), directory)
    receipt = save_monitor(ledger_monitor)
    assert receipt['capture_context']['source']['account_version'] == 3
    assert [h['shares'] for h in receipt['input']['local']['holdings']] == [200]


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
    for value in (research['verdict'], reading.without_verdict(research['summary'], research['verdict']), research['current']['current_zone'],
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
        assert not any('NAV' in gap for gap in data['account_gaps'])
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
    assert evaluation['result']['as_of'][:10] in portfolio and evaluation['identity'] not in portfolio  # identity lives on the evaluation page
    assert '前收盘价 · 补充有效的前收盘价' in company
    stock = html(web.stock_page(owner_request(f'/stock/{code}'), code), 404)
    assert f'href="{company_path}"' in stock
    monitor = html(web.monitor_detail(owner_request(evaluation_path), evaluation['id']))
    assert evaluation['request_id'] in monitor and evaluation['identity'] in monitor
    receipt = html(web.receipt_detail(owner_request(latest['path']), latest['id']))
    assert rejected['request_hash'] in receipt and sha in receipt
    for page, original in ((monitor, evaluation), (receipt, rejected)):
        assert json.loads(re.search(r'<pre[^>]*>(.*?)</pre>', page, re.S)[1]) == original


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
