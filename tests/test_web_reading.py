"""Reading UI checks on captured financial periods and observed browser text.

Condition-only cases exercise template branches, not invented research results.
"""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'a_share_research/templates'
ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
MACROS = ENV.get_template('_macros.html').module


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
    assert '纠错核查' in html and '核查问题：什么价格区间可以买入' in html
    html = str(MACROS.followup_link({'id': 51, 'question': '什么价格区间可以买入'}))
    assert 'href="/runs/51"' in html and '后续追问：什么价格区间可以买入' in html


def test_long_question_is_discoverable_without_losing_text():
    # Reuse a real captured disclosure excerpt, without making a research call.
    from test_check import MATERIAL
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
                cursor = conn.execute(query.value, (35 if 'FROM runs' in query.value else '601899.SH',))
                columns = {column[0] for column in cursor.description}
                assert {'question', 'feedback_text'} <= columns
                assert cursor.fetchall() == []
                checked += 1
    assert checked == 2
    conn.close()
