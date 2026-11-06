"""Double-pool template branches using unchanged captured candidates; no screening claims."""
import json
import re
from pathlib import Path

from a_share_research import charts, reading
from support.templates import ENV

ROOT = Path(__file__).resolve().parents[1]


def render_two_pools(small_rows, status="proposed", coverage=None):
    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    batch = {'id': 1, 'status': status, 'created_at': '2026-12-01', 'as_of': '20261023',
             'rule': {'version': 'screen-v1.2', 'picks': 5, 'picks_per_industry': 2},
             'q20': captured['rows'], 'q5': captured['q5'], 'l5': None, 'universe': None,
             'selection': {'draft': {'picks': []}}, 's10': small_rows, 'coverage': coverage or {}}
    small = dict(batch, q20=small_rows, q5=[], selection={'draft': {'picks': []}},
                 rule=dict(batch['rule'], picks=3, picks_per_industry=1))
    pools = []
    for item, prefix, label, field in [(batch, '', '大市值组', 'codes'),
                                       (small, 'small-', '小市值组', 'small_codes')]:
        pools.append({'batch': item, 'prefix': prefix, 'label': label, 'codes_field': field,
                      'description': '', 'mechanical_label': '机械前三' if prefix else '机械前五',
                      'mechanical_short': '前三' if prefix else '前五',
                      'draft': {}, 'draft_codes': [], 'overview': charts.batch_overview(item),
                      'sparks': {}, 'market_strip': None})
    return ENV.get_template('batch_v12.html').render(
        batch=batch, pools=pools, names={r['code']: r['name'] for r in captured['rows']},
        results=None, performance=None, csrf='', frozen_by={})


def test_two_pool_form_has_independent_fields_and_unique_ids():
    # Reuse one observed row solely to exercise the small-pool template branch.
    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    html = render_two_pools(captured['rows'][:1])
    assert html.count('action="/batches/1/freeze"') == 1
    assert 'name="codes"' in html and 'name="small_codes"' in html
    assert 'id="small-candidates"' in html and 'id="small-comparison"' in html
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert '尚未记录（冻结时保存）' in html
    assert '两组互不挤占，空位按现金计；小市值组可留空' in html


def test_empty_small_pool_keeps_large_freeze_available():
    html = render_two_pools([])
    assert 'name="codes"' in html and '冻结两组所选' in html
    assert 'id="small-pool"' in html and 'id="small-comparison"' in html
    assert 'id="small-candidates"' not in html


def test_unlinked_small_candidates_wait_without_query_or_finished_count():
    import sqlite3

    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    # The observed financial values stay unchanged; remove linkage fields to exercise
    # the deliberate stage before the worker queues small-company research.
    row = {k: v for k, v in captured['rows'][0].items()
           if k not in ('run_id', 'research_status', 'report_id', 'verdict')}
    with sqlite3.connect(':memory:') as conn:
        # No runs table exists, so this also proves the unlinked row is not queried.
        finished, rows = reading.research_state(conn, [row])
    assert not finished and rows[0]['research_status'] == 'pending'
    html = render_two_pools(rows, status='researching')
    large_finished = sum(r.get('research_status') not in ('pending', 'queued', 'running')
                         for r in captured['rows'])
    assert f'{large_finished} / {len(captured["rows"]) + 1} 家已完成' in html
    assert '等待大市值组研究完成后排队' in html
    small_matrix = html.split('id="small-candidates"', 1)[1].split('</table>', 1)[0]
    assert '未通过复核或研究失败' not in small_matrix


def test_failed_task_remains_linked_without_report():
    captured = json.loads((ROOT / 'tests/data/batch-candidate-matrix.json').read_text())
    row = dict(captured['rows'][0], research_status='failed', report_id=None, verdict=None,
               run_id=987, research_error='失败状态变体')
    html = render_two_pools([row], status='researching')
    small_matrix = html.split('id="small-candidates"', 1)[1].split('</table>', 1)[0]
    assert 'href="/runs/987"' in small_matrix
    assert 'href="/runs/987">查看失败详情' in html
