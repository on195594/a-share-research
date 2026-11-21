"""Reading UI checks on captured financial periods and observed browser text.

Condition-only cases exercise template branches, not invented research results.
"""
import json
import re
from pathlib import Path

import pytest
from support.templates import ENV, TEMPLATES
from a_share_research.evidence import report_source_view
from markupsafe import escape

ROOT = Path(__file__).resolve().parents[1]
MACROS = ENV.get_template('_macros.html').module


@pytest.mark.parametrize('source_kind', ['base', 'supplement'])
@pytest.mark.parametrize('page_state', ['recorded', 'empty', 'missing', 'null'])
def test_report_scope_distinguishes_unrecorded_pages_and_keeps_source_links(source_kind, page_state):
    records = json.loads((ROOT / 'tests/data/continuation-records.json').read_text())['records']
    body = next(r['report']['body'] for r in records if r['report']['id'] == 37)
    source = body if source_kind == 'base' else next(iter(body['sources'].values()))
    original_pages = source['pages']
    assert original_pages
    # Exercise only metadata-presence branches on a captured report, not new research content.
    if page_state == 'missing':
        source.pop('pages')
    elif page_state != 'recorded':
        source['pages'] = [] if page_state == 'empty' else None
    before = json.dumps(body, sort_keys=True)
    template = (TEMPLATES / 'report.html').read_text()
    start = template.index('  <details class="about" id="research-scope"')
    end = template.index('  </details>', start) + len('  </details>')
    view = report_source_view(body)
    html = ENV.from_string(template[start:end]).render(body=body, source_view=view, documents=view["sources"])
    label = '基础原文' if source_kind == 'base' else '补充原文'
    paragraph = next(p.split('</p>', 1)[0] for p in html.split('<p>') if p.startswith(label + '：'))
    assert f'href="{escape(source["document"]["url"])}"' in paragraph
    assert str(escape(source['document']['title'])) in paragraph
    if page_state == 'recorded':
        assert '已读取页码：' + '、'.join(map(str, original_pages)) + '。' in paragraph
        assert '未记录已读页码' not in paragraph
    else:
        assert '未记录已读页码。' in paragraph
        assert '已读取页码：' not in paragraph
    assert '已读取页码：。' not in html
    assert json.dumps(body, sort_keys=True) == before


def test_financial_periods_preserve_source_dates_and_explain_coverage():
    captured = json.loads((ROOT / 'tests/data/002648-three-statements.json').read_text())
    periods = {r['end_date'] for rows in captured['financials'].values() for r in rows}
    for period in periods:
        html = str(MACROS.financial_period(period))
        assert f'<time datetime="{period}">{period}</time>' in html
        label = {'03-31': '一季度', '06-30': '上半年', '09-30': '前三季度', '12-31': '全年'}[period[5:]]
        assert f'<small>{label}</small>' in html
    template = (TEMPLATES / 'report.html').read_text()
    assert '年初至报告期末累计值' in template
    assert '资产负债为期末时点值' in template


def test_financial_table_keeps_comparable_periods_after_preceding_periods_expand():
    import re

    from support.financials import CAPTURES, frames

    from a_share_research.material import financial_table
    template = (TEMPLATES / 'report.html').read_text()
    start = template.index('    <details class="block" id="data"')
    end = template.index('    </details>', start) + len('    </details>')
    table = ENV.from_string('{% from "_macros.html" import financial_period %}' + template[start:end])
    captured = CAPTURES['000858']
    for cutoff, expected in [
        ('2026-09-30', ['2024-12-31', '2025-09-30', '2025-12-31', '2026-09-30']),
        ('2025-12-31', ['2024-12-31', '2025-12-31']),
        ('2026-03-31', ['2024-12-31', '2025-03-31', '2025-12-31', '2026-03-31']),
    ]:
        fin = {k: frame[frame.end_date <= cutoff] for k, frame in frames(captured).items()}
        html = table.render(fin=financial_table(fin, {}))
        assert re.findall(r'<time datetime="([\d-]+)"', html) == expected
        assert ('更正版本待核对</small>' in html) == (cutoff != '2025-12-31')


def navigation(**present):
    template = (TEMPLATES / 'batch.html').read_text()
    start = template.index('{% if results or batch.l5')
    end = template.index('\n{% if results %}\n<section>', start)
    return ENV.from_string(template[start:end]).render(results=present.pop('results', None), batch=present)


def test_batch_navigation_only_links_to_present_focusable_sections():
    template = (TEMPLATES / 'batch.html').read_text()
    for field, target in [('results', 'returns'), ('l5', 'selected'), ('selection', 'comparison'),
                          ('q20', 'candidates'), ('universe', 'screening')]:
        html = navigation(**{field: True})
        assert html.count('href=') == 1
        assert f'href="#{target}"' in html
        assert f'id="{target}" tabindex="-1"' in template
    assert '<nav' not in navigation()


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


def test_relationship_and_feedback_only_use_explicit_fields():
    # IDs/text are known observations; passing an explicit parent tests the rendering contract.
    html = str(MACROS.research_context('什么价格区间可以买入', 34, None))
    assert '后续追问' in html and 'href="/reports/34"' in html
    html = str(MACROS.research_context(None, 34, '什么价格区间可以买入'))
    assert '核查' in html and '核查问题：什么价格区间可以买入' in html
    html = str(MACROS.followup_link({'id': 51, 'question': '什么价格区间可以买入'}))
    assert 'href="/runs/51"' in html and '后续追问：什么价格区间可以买入' in html


def test_long_question_is_discoverable_without_losing_text():
    # Reuse a real captured disclosure excerpt, without making a research call.
    from support.reports import MATERIAL
    text = '\n'.join(MATERIAL.pages.values())
    assert len(text) > 100
    html = str(MACROS.research_context(text, None, None))
    assert '<details>' in html and '展开全文' in html and text in html


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


def test_candidate_matrix_shades_percentiles_and_links_flags_to_details():
    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    template = (TEMPLATES / 'batch.html').read_text()
    start = template.index('{% set heat =')
    end = template.index('<p class="legend-row', start)
    matrix = ENV.from_string('{% from "_charts.html" import verdict_pips %}' + template[start:end])
    html = matrix.render(batch={'q20': captured['rows']}, l5=captured['l5'], q5=captured['q5'])
    # Levels in row order, compared with the unrounded captured percentiles.
    levels = [int(level) for level in re.findall(r'<td class="num h(\d)" title=', html)]
    expected = [r[key] for r in captured['rows'] for key in ('roe_pct', 'cash_pct', 'growth_pct', 'pe_pct')]
    assert levels == [4 if q >= 0.95 else 3 if q >= 0.85 else 2 if q >= 0.75 else 1 if q >= 0.6 else 0 for q in expected]
    assert len(set(levels)) == 5  # the captured batch spans every step
    assert html.count('class="picked"') == len(captured['l5'])
    flagged = [r for r in captured['rows'] if r.get('drawdown')]
    assert flagged and all(f'href="#cand-{r["code"]}">60日跌超30%' in html for r in flagged)
    assert html.count('class="matrix-flag"') == len(flagged)
    assert 'id="cand-{{ r.code }}"' in template


def test_home_verdict_filter_binds_each_card_group_bucket(tmp_path):
    from a_share_research import charts, db, reading
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
    cards[-1] = cards[-1] | {'bucket': '暂不值得'}  # marked variant: the captured set shares one bucket
    html = render('home.html', companies=cards, verdict_summary=charts.verdict_summary(reports),
                  pending=[], pending_more=False, active=[], batch=None)
    # The verdict filter hides whole groups; each group holds exactly its bucket's cards.
    groups = re.findall(r'<section class="card-group" data-verdict="([^"]+)"(.*?)</section>', html, re.S)
    shown = {label: re.findall(r'id="card-([^"]+)"', body) for label, body in groups}
    assert shown == {c['bucket']: [d['code'] for d in cards if d['bucket'] == c['bucket']] for c in cards}
    assert 'role="status" aria-live="polite" data-filter-status' in html


def test_failure_explanation_precedes_source_catalog():
    template = (TEMPLATES / 'report.html').read_text()
    assert template.index('复核意见：') < template.index('{% for key, source in documents.items() %}')


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


def test_report_layout_orders_business_financial_valuation_before_prior_agenda(tmp_path):
    from copy import deepcopy
    from types import SimpleNamespace
    from a_share_research import db, reading, valuation
    from support.continuation import BODY, load
    from support.templates import render
    from support.valuation import RECORD, ROWS, assumptions

    conn = db.connect(tmp_path / 'layout.db')
    load(conn)
    row = dict(conn.execute('SELECT * FROM reports WHERE id = 39').fetchone())
    body = deepcopy(BODY[39])
    body['valuation'] = assumptions()
    body['valuation_calc'] = valuation.compute(
        body['valuation'], body['table'], body['market_date'],
        consensus=valuation.consensus(ROWS, [2026, 2027, 2028], body['market_date']))
    body['prior_status'] = [{'ref': 'risks[0]', 'status': '仍存在',
                             'text': '风险条件仍在。', 'cites': body['risks'][0]['cites']}]
    prior = reading.prior_agenda(conn, body)
    page = lambda req=None: render(
        'report.html', report=RECORD | {'id': row['id'], 'period': row['period']}, body=body,
        feedback=None, feedback_label='', replay_of=None,
        rows={r['id']: r for r in body['table']}, price=None, followups=[],
        prior_agenda=prior, comparison={'old': None}, market=[], fin=[], request=req)
    html = page()
    conn.close()
    order = [html.index(tag) for tag in (
        '<section id="risks"', '<section id="answer"', '<section id="business"',
        '<section id="financial"', '<section id="valuation"', '<section id="checkpoints"',
        '<section id="prior-status"')]
    assert order == sorted(order)
    prior_block = html[html.index('<section id="prior-status"'):]
    prior_block = prior_block[:prior_block.index('</section>')]
    assert '<details class="block">' in prior_block
    focused_html = page(SimpleNamespace(query_params={'claim': 'prior_status:0'}))
    focused_prior = focused_html[focused_html.index('<section id="prior-status"'):]
    assert '<details class="block" open>' in focused_prior[:focused_prior.index('</section>')]


def test_noncritical_flip_sensitivity_folds_while_critical_stays_open():
    from copy import deepcopy

    from support.stance import FLIP_SAMPLES
    from support.templates import render

    def rendered(sample):
        calc = sample['valuation_calc']
        rep = {'id': sample['report_id'], 'code': sample['code'], 'name': sample['code'],
               'created_at': '2026-11-05T10:00:00+08:00', 'period': '2026-09-30',
               'passed': True, 'run_id': sample['report_id']}
        body = {'verdict': '需要谨慎', 'summary': '需要谨慎：测试。', 'risks': [], 'business': [],
                'financial': [], 'table': [], 'market_date': calc['as_of'],
                'stance': {'action': sample['action'], 'text': '立场说明。', 'cites': []},
                'stance_calc': {'price': calc['v2']['support_below'], 'valid_until': '2027-04-30'},
                'valuation_calc': deepcopy(calc)}
        html = render('report.html', report=rep, body=body, rows={}, comparison=None)
        start = html.index('<details class="block stance-sensitivity"')
        return html[start:html.index('>', start) + 1]

    critical = next(s for s in FLIP_SAMPLES if s['report_id'] == 116)
    noncritical = next(s for s in FLIP_SAMPLES if s['report_id'] == 132)
    assert ' open>' in rendered(critical)
    assert ' open>' not in rendered(noncritical)


def test_stock_page_checks_stay_with_checkpoint_and_trigger(reading_server, monkeypatch):  # noqa: F811
    server = reading_server
    from support.scans import replay_disclosure as disclosure_replay
    from support.scans import run_scan
    from support.templates import ENV

    from a_share_research import reading, service, web

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
    checkpoint = re.search(r'<li>(?:(?!</li>).)*?data-disclosure-check="checkpoints\[0\]".*?</li>', html, re.S)[0]
    assert body['checkpoints'][0]['text'] in checkpoint
    assert '偏离（低于）' in checkpoint and '[0.2691, 0.3341]' in checkpoint
    assert '亿元' in checkpoint and '业绩预告' in checkpoint and '2026-01-23' in checkpoint
    trigger = re.search(r'<div data-disclosure-trigger="add_trigger">.*?</div>', html, re.S)[0]
    assert '后续加仓条件' in trigger and '≥ 45 亿元' in trigger and '未达到' in trigger
    assert 'data-disclosure-check="checkpoints[0]"' not in trigger
    assert html.count('data-disclosure-check=') == 2
    # Both card macros share the same item-local rendering, including the folded list.
    report = reading.report(conn, service.company_card(conn, code)['report_id'])
    card = reading.card(report, body, checks=service.card_checks(conn, report['id']))
    compact = str(ENV.get_template('_card.html').module.summary_card(card))
    assert 'data-disclosure-check="checkpoints[0]"' in compact and '偏离（低于）' in compact
    class Failure(type(source)):
        def disclosures(self, code, periods):
            raise RuntimeError('变体：取数失败 <script>')
    failed = run_scan(conn, Failure(source.items))
    for route in (code, code + '.SH'):
        page = client.get(url + f'/stock/{route}')
        assert page.status_code == 200 and '披露机械核对未完成' in page.text
        assert '变体：取数失败 &lt;script&gt;' in page.text
        assert f'报告 #{report["id"]}' in page.text and f'扫描 #{failed["id"]}' in page.text
    summary = reading.report_cards(conn, [dict(report) | {'from_batch': False}], {})[0]
    compact = str(ENV.get_template('_card.html').module.summary_card(summary))
    assert '披露机械核对未完成' in compact and compact.index('机械核对') < compact.index('card-fold')
    run_scan(conn, source)
    for route in (code, code + '.SH'):
        assert '披露机械核对未完成' not in client.get(url + f'/stock/{route}').text
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
