"""Read-only discovery and comparisons on real production/K2 validation records."""
import json
from pathlib import Path

import pytest
from support.templates import ENV

from a_share_research import db, reading
from support.continuation import load

ROOT = Path(__file__).resolve().parents[1]



@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'reading.db')
    load(connection)
    yield connection
    connection.close()


def report_row(conn, report_id):
    return conn.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()


def test_literal_search_name_code_empty_unknown_and_pagination(conn):
    before = conn.total_changes
    for q in ('茅台', '600519', '600519.sh'):
        rows, more = reading.search_reports(conn, q)
        assert [r['id'] for r in rows] == [12] and not more
        assert rows[0]['coverage'] == '2026-06-30'
        assert 'body' not in rows[0].keys()
    for q in ('不存在的公司', '%', '_', "' OR 1=1 --", '<script>'):
        assert reading.search_reports(conn, q) == ([], False)
    first, more = reading.search_reports(conn, '', page_size=2)
    second, last = reading.search_reports(conn, '', 2, page_size=2)
    assert more and not last
    assert [r['id'] for r in first + second] == [39, 25, 12]
    assert reading.search_reports(conn, '', 3, page_size=2) == ([], False)
    assert conn.total_changes == before


def test_search_preserves_real_source_groups_and_excludes_replays(conn):
    row = reading.search_reports(conn, '000651')[0][0]
    assert row['id'] == 25 and not row['own'] and row['from_batch']
    row = reading.search_reports(conn, '603606')[0][0]
    assert row['id'] == 39 and row['own'] and not row['from_batch']
    assert {r['id'] for r in reading.search_reports(conn, '')[0]}.isdisjoint({143, 148})


def test_explicit_parent_and_k2_prior_reuse_deterministic_tables(conn):
    before = conn.total_changes
    for child, parent in [(9, 8), (12, 11), (39, 37)]:
        result = reading.comparison(conn, report_row(conn, child))
        assert result['old']['id'] == parent and 'diff' in result
        html = ENV.get_template('_comparison.html').render(comparison=result)
        assert f'/reports/{parent}' in html and f'/reports/{child}' in html
        assert '不等于公司整体判断反转' in html and '不推断风险已解决' in html
    assert conn.total_changes == before
    assert reading.comparison(conn, report_row(conn, 39))['new_question'].startswith('相对上次研究')


def test_no_inferred_link_and_missing_parent(conn):
    assert '未记录明确关联' in reading.comparison(conn, report_row(conn, 37))['reason']
    # Missing-reference boundary on a real continuation, without fabricating another report.
    conn.execute('DELETE FROM reports WHERE id=37')
    assert '不存在或不属于' in reading.comparison(conn, report_row(conn, 39))['reason']


def test_legacy_missing_table_and_incomplete_units_are_not_compared(conn):
    body = json.loads(report_row(conn, 39)['body'])
    original = json.dumps(body)
    body.pop('table')
    conn.execute('UPDATE reports SET body=? WHERE id=39', (json.dumps(body),))
    assert '未记录财务表' in reading.comparison(conn, report_row(conn, 39))['reason']
    body = json.loads(original)
    body['table'][0].pop('unit')
    # Inject None row and None-id row to guard against TypeError when sorting excluded.
    body['table'].extend([None, {'id': None, 'label': None, 'unit': None, 'values': {}}])
    conn.execute('UPDATE reports SET body=? WHERE id=39', (json.dumps(body),))
    result = reading.comparison(conn, report_row(conn, 39))
    assert '未知指标' in result['excluded']
    assert all(r['label'] != body['table'][0]['label'] for r in result['diff']['revised'])


def test_search_render_escapes_query_and_uses_get_without_research_action():
    html = ENV.get_template('research_library.html').render(q='<script>&"', page=1, companies=[], more=False)
    assert '<script>&"' not in html and '&lt;script&gt;' in html
    assert 'method="get"' in html and 'action="/research"' not in html
    assert 'autocomplete="off"' in html
    assert 'data-library-search' in html
    assert '返回后浏览器可能保留未提交的文字，请以下方结果为准。' in html
    assert '<button type="reset" class="link">还原与本页结果一致的关键词</button>' in html
    assert '没有找到匹配' in html
    html = ENV.get_template('research_library.html').render(q='', page=1, companies=[], more=False)
    assert '还没有可查' in html


def test_reverse_link_uses_real_continuation_question_from_report_body(conn):
    import ast
    tree = ast.parse((ROOT / 'a_share_research/web.py').read_text())
    query = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                 and isinstance(node.value, str) and 'FROM runs u LEFT JOIN reports f' in node.value)
    rows = conn.execute(query, ('603606.SH', 37, '603606.SH', 37, 37)).fetchall()
    assert len(rows) == 1 and rows[0]['id'] == 56
    html = str(ENV.get_template('_macros.html').module.followup_link(rows[0]))
    assert '接续研究：相对上次研究' in html and '/runs/56' in html
