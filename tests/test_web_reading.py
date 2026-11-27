"""Reading UI checks on captured financial periods and observed browser text.

Condition-only cases exercise template branches, not invented research results.
"""
import json
import re
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from support.delivery import FAILURE
from support.templates import ENV, TEMPLATES
from support.web import page_request
from a_share_research.evidence import report_source_view
from markupsafe import escape

ROOT = Path(__file__).resolve().parents[1]
MACROS = ENV.get_template('_macros.html').module


def test_current_session_follows_login_and_logout_within_a_request(web):
    request = page_request('/', cookie='session=unknown')
    assert web.current_session(request) is None

    logged_in = web.start_session(request)
    token = SimpleCookie(logged_in.headers['set-cookie'])['session'].value
    session = web.current_session(request)
    assert session == web._hash(token)
    assert web.current_session(page_request('/', cookie='session=unknown')) is None

    next_request = page_request('/', cookie=f'session={token}')
    assert web.current_session(next_request) == session
    assert web.logout(next_request, csrf=web.csrf_token(session)).status_code == 303
    assert web.current_session(next_request) is None
    assert web.current_session(page_request('/', cookie=f'session={token}')) is None


def test_session_expires_with_its_device(web, monkeypatch):
    from types import SimpleNamespace

    # Move the clock over the device deadline within this same request.
    request = page_request('/')
    web.start_session(request)
    session = web.current_session(request)
    monkeypatch.setattr(web, 'DEVICE_SECONDS', 1)
    now = web.time.time()
    monkeypatch.setattr(web, 'time', SimpleNamespace(time=lambda: now))
    response = web.remember_device(request, csrf=web.csrf_token(session), return_path='/')
    assert response.status_code == 303 and web.current_session(request) == session
    now += 1
    assert web.current_session(request) is None


@pytest.mark.parametrize('source_kind', ['base', 'supplement'])
@pytest.mark.parametrize('page_state', ['recorded', 'missing'])
def test_report_scope_distinguishes_unrecorded_pages_and_keeps_source_links(source_kind, page_state):
    records = json.loads((ROOT / 'tests/data/continuation-records.json').read_text())['records']
    body = next(r['report']['body'] for r in records if r['report']['id'] == 37)
    source = body if source_kind == 'base' else next(iter(body['sources'].values()))
    original_pages = source['pages']
    assert original_pages
    # Exercise only metadata-presence branches on a captured report, not new research content.
    if page_state == 'missing':
        source.pop('pages')
        body['problems'] = FAILURE['problems']
    before = json.dumps(body, sort_keys=True)
    template = (TEMPLATES / 'report.html').read_text()
    start = template.index('  <details class="about" id="research-scope"')
    end = template.index('  </details>', start) + len('  </details>')
    view = report_source_view(body)
    html = ENV.from_string(template[start:end]).render(body=body, source_view=view, documents=view["sources"])
    assert source['document']['url'] in html and str(escape(source['document']['title'])) in html
    if page_state == 'recorded':
        assert '、'.join(map(str, original_pages)) in html
    else:
        assert '未记录已读页码' in html
        assert str(escape(FAILURE['problems'][0]['problem'])) in html
    assert json.dumps(body, sort_keys=True) == before


def test_financial_periods_preserve_source_dates_and_explain_coverage():
    # End dates captured from the 002648 three statements.
    periods = (
        '2021-12-31', '2022-03-31', '2022-06-30', '2022-09-30',
        '2022-12-31', '2023-03-31', '2023-06-30', '2023-09-30',
        '2023-12-31', '2024-03-31', '2024-06-30', '2024-09-30',
        '2024-12-31', '2025-03-31', '2025-06-30', '2025-09-30',
        '2025-12-31', '2026-03-31', '2026-06-30', '2026-09-30',
    )
    for period in periods:
        html = str(MACROS.financial_period(period))
        assert period in html
        label = {'03-31': '一季度', '06-30': '上半年', '09-30': '前三季度', '12-31': '全年'}[period[5:]]
        assert label in html
    template = (TEMPLATES / 'report.html').read_text()
    assert '年初至报告期末累计值' in template
    assert '资产负债为期末时点值' in template


def test_financial_table_keeps_comparable_periods_after_preceding_periods_expand():

    from support.financials import CAPTURES, frames

    from a_share_research.material import financial_table
    template = (TEMPLATES / 'report.html').read_text()
    start = template.index('    <details class="block" id="data"')
    end = template.index('    </details>', start) + len('    </details>')
    table = ENV.from_string('{% from "_macros.html" import financial_period %}' + template[start:end])
    fin = {k: frame[frame.end_date <= '2026-09-30'] for k, frame in frames(CAPTURES['000858']).items()}
    html = table.render(fin=financial_table(fin, {}))
    assert all(period in html for period in ('2024-12-31', '2025-09-30', '2025-12-31', '2026-09-30'))
    assert '更正版本待核对' in html


def navigation(**present):
    template = (TEMPLATES / 'batch.html').read_text()
    start = template.index('{% if results or batch.l5')
    end = template.index('\n{% if results %}\n<section>', start)
    return ENV.from_string(template[start:end]).render(results=present.pop('results', None), batch=present)


def test_batch_navigation_only_links_to_present_focusable_sections():
    html = navigation(l5=True)
    assert 'href="#selected"' in html
    assert 'href="#returns"' not in html


def test_candidate_links_include_the_real_company_name():
    template = (TEMPLATES / 'batch.html').read_text()
    start = template.index('{% elif r.report_id and r.verdict %}')
    end = template.index('{% elif r.research_status == "failed" %}', start)
    fragment = template[start:end].replace('{% elif r.report_id and r.verdict %}', '{% if r.report_id and r.verdict %}', 1) + '{% endif %}'
    # On-line observation: 紫金矿业 report 35 is marked 需要谨慎.
    html = ENV.from_string(fragment).render(r={'report_id': 35, 'name': '紫金矿业', 'verdict': '需要谨慎'})
    assert '>紫金矿业：需要谨慎</a>' in html
    html = ENV.from_string(fragment).render(r={'report_id': 35, 'name': '紫金矿业'})
    assert '>紫金矿业：复核未通过</a>' in html


def test_candidate_factor_periods_and_base_from_frozen_batch():
    captured = json.loads((ROOT / 'tests/data/batch-factor-labels.json').read_text())
    template = (TEMPLATES / 'batch.html').read_text()
    start = template.index('{% set years =')
    end = template.index('</dl>', start) + len('</dl>')
    factors = ENV.from_string('{% import "_batch.html" as b %}' + template[start:end])
    for row in captured['rows']:
        html = factors.render(r=row, batch=captured)
        assert html.count('2023–2025') == 2
        assert '2022→2025' in html and '初筛日 <span class="date">2026-10-23</span>' in html
        expected = ('1.82', '+31.7%', '282.13') if row['code'] == '300750.SZ' else ('1.30', '+163.7%', '2.50')
        assert f'>{expected[0]}</dd>' in html and f'>{expected[1]}</dd>' in html
        assert f'基数 {expected[2]} 亿' in html
        for missing in ('years', 'profit_dedt'):
            legacy = {key: value for key, value in row.items() if key != missing}
            html = factors.render(r=legacy, batch=captured)
            assert '2023–2025' not in html and '2022→2025' not in html and '基数' not in html
            assert f'>{expected[0]}</dd>' in html and f'>{expected[1]}</dd>' in html
    assert '经营现金流合计 ÷ 合并净利润合计' in template
    assert '扣非净利润的 3 年复合增速' in template
    assert 'PE（TTM，归母口径）' in template


def test_history_shows_observed_question_without_inferring_a_parent():
    html = str(MACROS.research_context('什么价格区间可以买入', None, None))
    assert '研究问题：什么价格区间可以买入' in html
    assert '未关联原报告' in html
    assert '查看原报告' not in html
    assert str(MACROS.research_context(None, None, None)).strip() == ''
    assert '未记录研究问题' in str(MACROS.research_context(None, 34, None))


def test_long_question_is_discoverable_without_losing_text():
    # Reuse a real captured disclosure excerpt, without making a research call.
    from support.reports import MATERIAL
    text = '\n'.join(MATERIAL.pages.values())
    assert len(text) > 100
    html = str(MACROS.research_context(text, None, None))
    assert text in html


def test_context_queries_execute_against_current_schema_without_writing_research(tmp_path):
    from a_share_research import db, reading
    from support.continuation import load

    conn = db.connect(tmp_path / 'reading.db')
    load(conn)
    before = conn.total_changes
    for rows in (reading.followups(conn, '603606.SH', 37), reading.stock_reports(conn, '603606.SH')):
        assert rows
        assert all({'question', 'feedback_text'} <= set(row.keys()) for row in rows)
    assert conn.total_changes == before
    conn.close()


def test_candidate_matrix_links_flags_to_details():
    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    template = (TEMPLATES / 'batch.html').read_text()
    start = template.index('{% set heat =')
    end = template.index('<p class="legend-row', start)
    matrix = ENV.from_string('{% from "_charts.html" import verdict_pips %}' + template[start:end])
    html = matrix.render(batch={'q20': captured['rows']}, l5=captured['l5'], q5=captured['q5'])
    flagged = next(r for r in captured['rows'] if r.get('drawdown'))
    assert f'href="#cand-{flagged["code"]}"' in html and '60日跌超30%' in html


def test_home_cards_only_the_shortlist_and_states_stale_coverage_once(tmp_path):
    from a_share_research import db, reading
    from support.continuation import load
    from support.templates import render
    conn = db.connect(tmp_path / 'home.db')
    try:
        load(conn)
        reports = reading.latest_reports(conn, include_checkpoint=False)
        cards = reading.report_cards(conn, reports, {})
    finally:
        conn.close()
    assert len(cards) > 1
    # Marked variants: the captured set shares one bucket, so give the home page one of each kind.
    cards = [cards[0] | {'bucket': '值得继续研究'}] + [c | {'bucket': '需要谨慎'} for c in cards[1:]]
    due = ('2026-09-30', '2026 年三季报')
    stale = [c for c in cards if c['coverage'] < due[0]]
    assert stale
    html = render('home.html', companies=cards, stale=stale, due=due, pending=[], pending_more=False, active=[], batch=None)
    assert re.findall(r'id="card-([^"]+)"', html) == [cards[0]['code']]
    assert f'href="/research-library">全部 {len(cards)} 家公司的研究与判断' in html
    assert html.count('未覆盖 2026 年三季报') == 1 and f'{len(stale)} 家公司的研究资料未覆盖' in html
    assert 'id="attention"' in html


def test_home_batch_projection_and_last_passed_scope(web, monkeypatch):
    from a_share_research import batches, reading
    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    batch_id = web.conn.execute(
        "INSERT INTO batches(status, created_at, rule, q20, universe) VALUES ('proposed', ?, ?, ?, ?)",
        ('2026-10-23', json.dumps(batches.RULE), json.dumps(captured['rows']), json.dumps(captured['markets'])),
    ).lastrowid
    passed_queries = []
    latest_reports = reading.latest_reports

    def record_scope(conn, **kwargs):
        if kwargs.get('passed'):
            passed_queries.append(kwargs.get('codes'))
        return latest_reports(conn, **kwargs)

    monkeypatch.setattr(reading, 'latest_reports', record_scope)
    response = web.home_page(page_request('/'), 'owner')
    batch = response.context['batch']
    assert batch['id'] == batch_id and batch['created_at'] == '2026-10-23' and batch['status'] == 'proposed'
    assert 'universe' not in batch.keys() and 'coverage' not in batch.keys()
    assert '2026 年 10 月批次' in response.body.decode()
    assert response.context['companies'] and not passed_queries

    # Status-only variants of the captured batch and continuation expose the other home branches.
    web.conn.execute("UPDATE batches SET status='tracking' WHERE id=?", (batch_id,))
    web.conn.execute("UPDATE reports SET passed=0 WHERE id=39")
    response = web.home_page(page_request('/'), 'owner')
    assert response.context['batch']['q20'] == reading.latest_batch(web.conn)['q20']
    failed = next(c for c in response.context['companies'] if c['id'] == 39)
    assert failed['last_passed']['id'] == 37 and passed_queries == [[failed['code']]]
    web.conn.execute("DELETE FROM reports")
    passed_queries.clear()
    assert web.home_page(page_request('/'), 'owner').context['companies'] == []
    assert not passed_queries


def test_contextual_followup_preserves_risk_identity_and_escapes_source():
    from support.continuation import BODY
    risk = BODY[39]['risks'][0]
    macro = ENV.get_template('_macros.html').module
    html = macro.items([risk], {}, '风险', section='risks', followup=True)
    assert 'data-followup-location="risks[0]"' in html and 'href="#more"' in html
    assert 'data-followup-location' not in macro.items([risk], {}, '风险', section='risks')
    # Marked text-only hostile variant; no model or investment output is generated.
    hostile = dict(risk, text='"<script>alert(1)</script>')
    safe = macro.items([hostile], {}, '风险', section='risks', followup=True)
    assert '<script>' not in safe and '&lt;script&gt;' in safe


def test_report_page_exposes_prior_status(web):
    from a_share_research import reading
    from support.continuation import BODY
    # Exercise the prior-status field with a marked variant of the captured continuation.
    entry = {'ref': 'risks[0]', 'status': '仍存在', 'text': '风险条件仍在。', 'cites': BODY[39]['risks'][0]['cites']}
    body = BODY[39] | {'prior_status': [entry]}
    web.conn.execute("UPDATE reports SET body=? WHERE id=39", (json.dumps(body),))
    prior = reading.prior_agenda(web.conn, body)
    expected = reading.comparison(web.conn, reading.report(web.conn, 39))
    request = page_request('/reports/39')
    web.start_session(request)
    response = web.report_page(request, 39)
    assert response.status_code == 200
    assert response.context['comparison'] == expected and response.context['prior_agenda'] == prior
    html = response.body.decode()
    assert entry['text'] in html and prior[0]['previous'] in html


def test_stock_page_renders_disclosure_checks_and_recovers(reading_server, monkeypatch):  # noqa: F811
    server = reading_server
    from support.scans import replay_disclosure as disclosure_replay
    from support.scans import run_scan

    from a_share_research import web

    conn, client, url, *_ = server
    code, body, source = disclosure_replay(conn)
    monkeypatch.setattr(web, 'current_session', lambda request: 'offline-owner')
    before = client.get(url + f'/stock/{code}')
    assert before.status_code == 200
    assert 'data-disclosure-' not in before.text
    run_scan(conn, source)
    response = client.get(url + f'/stock/{code}')
    assert response.status_code == 200
    html = response.text
    assert body['checkpoints'][0]['text'] in html and '偏离（低于）' in html
    assert '后续加仓条件' in html and '未达到' in html

    class Failure(type(source)):
        def disclosures(self, code, periods):
            raise RuntimeError('变体：取数失败 <script>')
    failed = run_scan(conn, Failure(source.items))
    page = client.get(url + f'/stock/{code}.SH')
    assert page.status_code == 200 and '披露机械核对未完成' in page.text
    assert '变体：取数失败 &lt;script&gt;' in page.text and f'扫描 #{failed["id"]}' in page.text
    run_scan(conn, source)
    assert '披露机械核对未完成' not in client.get(url + f'/stock/{code}.SH').text
    class Conflict(type(source)):
        def disclosures(self, code, periods):
            raw = super().disclosures(code, periods)
            # Explicit bound-doubling variant of the captured forecast.
            raw['forecast'][0]['net_profit_min'] *= 2
            raw['forecast'][0]['net_profit_max'] *= 2
            return raw
    run_scan(conn, Conflict(source.items))
    page = client.get(url + f'/stock/{code}.SH')
    assert '同一披露版本内容冲突' in page.text and '观测值 无可判数值' in page.text


def test_research_library_missing_verdict_keeps_clickable_report_text():
    from support.templates import render
    html = render('research_library.html', q='', history=False, whole_library=False, page=1,
                  companies=[{'id': 7, 'code': '600519.SH', 'name': '贵州茅台', 'passed': 1, 'verdict': None,
                              'created_at': '2026-11-22T10:00:00+08:00', 'coverage': '2026-09-30',
                              'own': 1, 'from_batch': 0}],
                  hits=[], error='', scope_company=None, more=False,
                  history_url='/research-library', all_url='/research-library',
                  previous_url='/research-library', next_url='/research-library')
    assert 'href="/reports/7"' in html and '>查看报告</a>' in html


def test_batch_page_drops_deleted_run_links_and_device_lookup_stays_on_connections(reading_server, monkeypatch):
    from a_share_research import reading, web
    conn, client, url, *_ = reading_server
    monkeypatch.setattr(web, 'current_session', lambda request: 'offline-owner')

    # Verify active_device is queried on /connections but skipped on ordinary pages like /.
    device_calls = []
    real_active_device = web.active_device
    def spy(request):
        device_calls.append(request.url.path)
        return real_active_device(request)
    monkeypatch.setattr(web, 'active_device', spy)
    assert client.get(url + '/').status_code == 200
    assert '/' not in device_calls
    conn_page = client.get(url + '/connections')
    assert conn_page.status_code == 200 and '记住此设备 30 天' in conn_page.text
    assert '/connections' in device_calls

    # Frozen batch with a deleted failed run_id shows the failure without a dead /runs/{id} link.
    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    row = dict(captured['rows'][0], run_id=999999, report_id=None, verdict=None,
               research_status='failed', research_error='模型超时')
    rule = {'version': 'screen-v1.1', 'picks': 5, 'picks_per_industry': 2, 'cost': 0.002, 'benchmarks': {}}
    cursor = conn.execute(
        "INSERT INTO batches (status, as_of, rule, q20, q5, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ('frozen', '20261101', json.dumps(rule), json.dumps([row]), json.dumps([]),
         '2026-11-01T10:00:00+08:00', '2026-11-01T10:00:00+08:00'),
    )
    conn.commit()
    assert reading.existing_run_ids(conn, [999999]) == set()
    batch_html = client.get(url + f'/batches/{cursor.lastrowid}').text
    assert '模型超时' in batch_html and '/runs/999999' not in batch_html
