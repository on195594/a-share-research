"""Reading UI checks on captured financial periods and observed browser text.

Condition-only cases exercise template branches, not invented research results.
"""
import json
from pathlib import Path

import pytest
from support.templates import ENV, TEMPLATES
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
    html = ENV.from_string(template[start:end]).render(body=body)
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
    assert '并非单季值' in template
    assert '资产负债为期末时点值' in template


def test_financial_table_keeps_comparable_periods_after_preceding_periods_expand():
    import re
    from a_share_research.research import financial_table
    from test_financial_series import CAPTURES, frames
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
    import ast
    from a_share_research import db
    tree = ast.parse((ROOT / 'a_share_research/web.py').read_text())
    conn = db.connect(tmp_path / 'empty.db')
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'execute' and node.args:
            query = node.args[0]
            if isinstance(query, ast.Constant) and isinstance(query.value, str) and ' AS question' in query.value:
                cursor = conn.execute(query.value, (('601899.SH', 35, '601899.SH', 35, 35) if 'FROM runs' in query.value else ('601899.SH',)))
                columns = {column[0] for column in cursor.description}
                assert {'question', 'feedback_text'} <= columns
                assert cursor.fetchall() == []
                checked += 1
    assert checked == 2
    conn.close()
