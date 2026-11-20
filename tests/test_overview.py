"""Research uses captured disclosures and reports. Ledger associations use the existing scoped book fixture."""
import pytest
from fastapi.testclient import TestClient

from a_share_research import db, overview, scans, service
from a_share_research.account import cycle_analysis
from a_share_research.tools.registry import Context, invoke
from support.continuation import load
from test_account_analysis import request
from test_analysis_transitions import rewrite
from test_policy_ledger_monitor import bound_book, configured as monitor_fixture, run as save_monitor  # noqa: F401
from test_position_store import batch, event, state, submit
from test_scans import CATALOG, priced, run_scan


configured = monitor_fixture


def test_reopening_detaches_thesis_and_monitor_from_the_closed_cycle(configured):
    (path, conn, principal, directory, sha), _, active, _ = configured
    book = (conn, principal, directory, sha)
    cycle_analysis.update(conn, principal, 'a', 'legacy:1', request(book, [rewrite(retire_l3_ids=[])]), directory)
    receipt = save_monitor(configured)
    ctx = Context(conn, path, principal, account_shadow_enabled=True)
    old = invoke(ctx, 'account.portfolio_overview', {})['data']['holdings'][0]
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
    current = invoke(ctx, 'research.company_overview', {'code': old['code']})['data']
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


def test_corrupt_monitor_is_a_gap_in_overviews_and_a_conflict_on_its_deep_link(configured, monkeypatch):
    (_, conn, _, _, _), _, _, _ = configured
    receipt = save_monitor(configured)
    conn.execute('DROP TRIGGER policy_evaluations_no_update')
    conn.execute("UPDATE policy_evaluations SET capture_context_hash=? WHERE id=?", ('0' * 64, receipt['id']))

    portfolio = overview.portfolio_overview(conn)
    holding, = portfolio['holdings']
    company = overview.company_overview(conn, holding['code'])
    assert company['held'] and company['position'] == holding['position']
    for result in (portfolio, company):
        assert result['policy'] is None and '政策采集来源哈希不一致' in result['gaps']

    url = 'http://localhost'
    monkeypatch.setenv('PUBLIC_BASE_URL', url)
    monkeypatch.setenv('DEV_LOGIN', '1')
    monkeypatch.setenv('SESSION_SECRET', 'isolated-test-only-not-a-real-secret')
    from a_share_research import web
    monkeypatch.setattr(web, 'settings', web.Settings.from_env())
    monkeypatch.setattr(web, 'conn', conn)
    with TestClient(web.app, base_url=url, follow_redirects=False) as client:
        assert client.post('/auth/dev', headers={'Origin': url}).status_code == 303
        response = client.get(f"/policy/evaluations/{receipt['id']}")
        assert response.status_code == 409 and response.json()['detail'] == '政策采集来源哈希不一致'


def test_public_research_and_open_disclosures_survive_an_unavailable_account(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    try:
        load(conn)
        priced(conn)
        code = '603606'
        scans.replace_subscriptions(conn, [code], catalog=CATALOG)
        scan = run_scan(conn)
        conn.execute('PRAGMA query_only=ON')
        company = overview.company_overview(conn, code)
        assert company['held'] is None and company['gaps']
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
    finally:
        conn.close()
