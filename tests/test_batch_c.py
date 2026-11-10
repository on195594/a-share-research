"""Batch C storage, sequencing and accounting using captured screening/report/market rows."""
import copy
import json
import re
import sqlite3
from pathlib import Path

import pytest

from a_share_research import batches, charts, db, model, reading, screening, service, sources
from support.templates import ENV
from test_advisor_sources import DailyReports, budget_calls

DATA = Path(__file__).parent / 'data'
ROWS = json.loads((DATA / 'screen-rows.json').read_text())['rows']
SMALL = json.loads((DATA / 'screen-small-rows.json').read_text())['small_companies']
PRICES = json.loads((DATA / 'screen-prices.json').read_text())
MARKET = json.loads((DATA / 'screen-entry-market.json').read_text())
V12_RULE = batches.NEW_RULE | {'version': 'screen-v1.2',
                              'small': batches.NEW_RULE['small'] | {'mechanical_per_industry': 2}}


def cache_partial_window(monkeypatch):
    monkeypatch.delenv('TUSHARE_CACHE', raising=False)
    monkeypatch.delenv('TUSHARE_TOKEN', raising=False)
    replay = DailyReports()
    sources.tushare_query(replay, 'trade_cal', exchange='SSE', is_open='1',
                          start_date='20260620', end_date='20260918')
    sources.tushare_query(replay, 'report_rc', start_date='20260918', end_date='20260918')
    return replay


def proposed(conn, *, status='proposed'):
    q20 = [r | {'verdict': '需要谨慎'} for r in ROWS[:6]]
    s10 = [r | {'verdict': '需要谨慎'} for r in SMALL]
    values = (status, db.now(), '20260918', json.dumps(batches.NEW_RULE), json.dumps(q20), '[]',
              json.dumps(s10), '[]', json.dumps({'draft': {'picks': [{'code': q20[0]['code']}]}}),
              json.dumps({'draft': {'picks': []}}))
    conn.execute('INSERT INTO batches (status,created_at,as_of,rule,q20,q5,s10,sq3,selection,small_selection) '
                 'VALUES (?,?,?,?,?,?,?,?,?,?)', values)
    return q20, s10


def test_only_new_batches_receive_v13_and_existing_rows_survive_v8(tmp_path):
    path = tmp_path / 'v7.db'
    old = sqlite3.connect(path, isolation_level=None)
    old.row_factory = sqlite3.Row
    old.executescript(db.SCHEMA)
    for migrate in db.MIGRATIONS[:7]:
        migrate(old)
    old.execute('PRAGMA user_version=7')
    for status in ('queued', 'frozen', 'tracking'):
        old.execute('INSERT INTO batches (status,created_at,rule,q20,q5,l5,entry,marks) VALUES (?,?,?,?,?,?,?,?)',
                    (status, '2026-10-24', json.dumps(batches.RULE), json.dumps(ROWS), '[]',
                     json.dumps({'codes': list(PRICES['entry']['prices'])}), json.dumps(PRICES['entry']),
                     json.dumps({'latest': PRICES['mark']})))
    before = [dict(r) for r in old.execute('SELECT * FROM batches')]
    old.close()
    conn = db.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 9
    assert conn.total_changes == 0
    for row in before:
        saved = dict(conn.execute('SELECT * FROM batches WHERE id=?', (row['id'],)).fetchone())
        assert {key: saved[key] for key in row} == row
        assert all(saved[key] is None for key in ('s10', 'sq3', 'sl3', 'small_selection', 'coverage'))
    new_id = service.create_batch(conn, 'new-c')
    new = batches.load(conn.execute('SELECT * FROM batches WHERE id=?', (new_id,)).fetchone())
    assert new['rule'] == batches.NEW_RULE and batches.RULE['version'] == 'screen-v1.1'
    assert new['rule']['weights'] == batches.RULE['weights']
    conn.close()


def test_sl3_comparison_and_freeze_enforce_one_company_per_industry(tmp_path, monkeypatch):
    cache_partial_window(monkeypatch)
    conn = db.connect(tmp_path / 'c.db')
    q20, s10 = proposed(conn)
    pair = [r for r in s10 if r['industry'] == s10[0]['industry']][:2]
    assert len(pair) == 2
    candidates = [r | {'report': {'verdict': r['verdict']}} for r in s10]
    picks = [{'code': r['code'], 'return_source': '利润修复', 'why_over_higher_ranked': '现金更好',
              'invalidation': '毛利率下滑'} for r in pair]
    assert any('超过 1 家' in e for e in screening.selection_problems(
        {'picks': picks}, candidates, batches.small_rule(batches.NEW_RULE)))
    with pytest.raises(service.RequestError, match='小市值组同一行业最多 1 家'):
        service.freeze_batch(conn, 1, [q20[0]['code']], '', small_codes=[r['code'] for r in pair])
    service.freeze_batch(conn, 1, [q20[0]['code']], '', small_codes=[pair[0]['code']])
    frozen = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert frozen['sl3']['codes'] == [pair[0]['code']]
    assert set(frozen['coverage']) == {r['code'] for r in q20 + s10}
    assert all(c['covered_days'] == 1 and c['trading_days'] > 1 for c in frozen['coverage'].values())
    assert budget_calls() == 1
    conn.close()


def test_empty_small_draft_auto_freezes_with_large_and_partial_coverage(tmp_path, monkeypatch):
    cache_partial_window(monkeypatch)
    conn = db.connect(tmp_path / 'c.db')
    proposed(conn, status='selecting')
    selection = batches.load(conn.execute('SELECT * FROM batches').fetchone())['selection']
    assert screening._auto_freeze(conn, 1, selection)
    frozen = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert frozen['status'] == 'frozen' and frozen['sl3']['codes'] == []
    assert frozen['sl3']['frozen_by'] == 'auto' and frozen['l5']['frozen_by'] == 'auto'
    assert all(c['covered_days'] == 1 for c in frozen['coverage'].values())
    assert budget_calls() == 1
    conn.close()


@pytest.mark.parametrize('rule', [batches.RULE, V12_RULE, batches.NEW_RULE], ids=['v11', 'v12', 'v13'])
def test_empty_large_draft_with_candidates_requires_owner_picks(tmp_path, monkeypatch, rule):
    cache_partial_window(monkeypatch)
    conn = db.connect(tmp_path / 'c.db')
    q20, s10 = proposed(conn, status='selecting')
    # Existing captured rows; only workflow state changes, no financial inputs change.
    q20 = [r | {'return_60d': None} for r in q20]
    selection = {'draft': {'picks': []}}
    db.update_batch(conn, 1, rule=json.dumps(rule), q20=json.dumps(q20),
                    small_selection=json.dumps({'draft': {'picks': [{'code': s10[0]['code']}]}}))
    assert screening._auto_freeze(conn, 1, selection)
    assert not screening.tick(conn)
    with pytest.raises(service.RequestError, match='选择 1–5 家'):
        service.freeze_batch(conn, 1, [], '')
    assert conn.execute('SELECT status FROM batches').fetchone()[0] == 'proposed'
    with pytest.raises(service.RequestError, match='选择 1–5 家'):
        service.freeze_batch(conn, 1, [r['code'] for r in q20], '')
    service.freeze_batch(conn, 1, [q20[0]['code']], '')
    frozen = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert frozen['l5']['codes'] == [q20[0]['code']]
    assert frozen['l5']['frozen_by'] == 'owner'
    conn.close()


@pytest.mark.parametrize('verdict', [None, '暂不值得'])
@pytest.mark.parametrize('by', ['owner', 'auto'])
def test_empty_large_freezes_only_without_selectable_candidates(tmp_path, monkeypatch, verdict, by):
    cache_partial_window(monkeypatch)
    conn = db.connect(tmp_path / 'c.db')
    q20, s10 = proposed(conn)
    selection = {'draft': {'picks': []}}
    db.update_batch(conn, 1, q20=json.dumps([r | {'verdict': verdict, 'return_60d': None} for r in q20]),
                    selection=json.dumps(selection),
                    small_selection=json.dumps({'draft': {'picks': [{'code': s10[0]['code']}]}}))
    with pytest.raises(service.RequestError):
        service.freeze_batch(conn, 1, [q20[0]['code']], '')
    if by == 'auto':
        # A complete draft left in proposed by an older worker must be recovered.
        assert screening.tick(conn)
    else:
        service.freeze_batch(conn, 1, [], '')
    frozen = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert frozen['status'] == 'frozen'
    assert frozen['l5']['codes'] == [] and frozen['l5']['frozen_by'] == by
    assert frozen['sl3']['codes'] == [s10[0]['code']] and frozen['sl3']['frozen_by'] == by
    conn.close()


def completed_pools(conn, *, large_passed, small_passed, rule=batches.NEW_RULE, small_draft=True):
    """Replay observed candidate verdicts; mutate review outcomes to exercise sequencing.

    Pool membership is only a workflow fixture, not a claim about market-cap eligibility.
    Saved small picks exercise restart/resume without a live model completion.
    """
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
    large, small = copy.deepcopy(captured['rows'][1:2]), copy.deepcopy(captured['rows'][:1])
    for rows, passed in ((large, large_passed), (small, small_passed)):
        for row in rows:
            row['run_id'] = db.create_run(conn, row['code'], None, name=row['name'])
            db.update_run(conn, row['run_id'], status='done')
            db.save_report(conn, row['run_id'], row['code'], row['name'], '', passed,
                           row['verdict'], {'verdict': row['verdict']})
    conn.execute('INSERT INTO batches (status,created_at,as_of,rule,q20,q5,s10,sq3,small_selection) '
                 'VALUES (?,?,?,?,?,?,?,?,?)',
                 ('researching', db.now(), '20260918', json.dumps(rule), json.dumps(large), '[]',
                  json.dumps(small), '[]',
                  json.dumps({'draft': {'picks': [{'code': small[0]['code']}]}}) if small_draft else None))
    return large, small


@pytest.mark.parametrize('small_picks', [True, False], ids=['small-selected', 'both-drafts-empty'])
def test_zero_passed_large_resumes_small_comparison_and_auto_freezes(tmp_path, monkeypatch, small_picks):
    cache_partial_window(monkeypatch)
    conn = db.connect(tmp_path / 'c.db')
    _, small = completed_pools(conn, large_passed=False, small_passed=True)
    if not small_picks:
        db.update_batch(conn, 1, small_selection=json.dumps({'draft': {'picks': []}}))
    assert screening.tick(conn)
    frozen = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert frozen['status'] == 'frozen' and frozen['error'] is None
    assert frozen['selection']['judgment'] == model.judgment_identity()
    assert frozen['selection']['draft'] == {
        'picks': [], 'rejected': [], 'concentration': '',
        'gaps': ['大市值组没有研究通过的候选，全部席位留现金']}
    assert frozen['l5']['codes'] == [] and frozen['l5']['frozen_by'] == 'auto'
    assert frozen['sl3']['codes'] == ([small[0]['code']] if small_picks else [])
    assert frozen['sl3']['frozen_by'] == 'auto'
    assert frozen['q20'][0]['verdict'] is None
    assert frozen['s10'][0]['verdict'] == small[0]['verdict']
    conn.close()


@pytest.mark.parametrize('rule,small_passed', [(batches.NEW_RULE, False), (batches.RULE, True)],
                         ids=['neither-pool-selectable', 'legacy-large-required'])
def test_no_selectable_candidates_keeps_existing_failure(tmp_path, rule, small_passed):
    conn = db.connect(tmp_path / 'c.db')
    completed_pools(conn, large_passed=False, small_passed=small_passed, rule=rule, small_draft=False)
    assert screening.tick(conn)
    failed = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert failed['status'] == 'failed' and '没有研究通过的候选' in failed['error']
    assert failed['l5'] is None and failed['sl3'] is None
    conn.close()


def test_small_comparison_still_runs_when_large_selection_is_saved(tmp_path, monkeypatch):
    cache_partial_window(monkeypatch)
    conn = db.connect(tmp_path / 'c.db')
    large, _ = completed_pools(conn, large_passed=True, small_passed=False, small_draft=False)
    db.update_batch(conn, 1, selection=json.dumps({'draft': {'picks': [{'code': large[0]['code']}]}}))
    assert screening.tick(conn)
    frozen = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert frozen['status'] == 'frozen'
    assert frozen['small_selection']['draft']['gaps'] == ['小市值组没有研究通过且可选的候选，全部席位留现金']
    assert frozen['sl3']['codes'] == [] and frozen['l5']['codes'] == [large[0]['code']]
    conn.close()


def test_cached_coverage_counts_distinct_organizations_and_never_backfills(monkeypatch):
    replay = cache_partial_window(monkeypatch)
    # No token and an exhausted budget: cache-only must still read the one captured day.
    monkeypatch.setenv('REPORT_RC_DAILY_BUDGET', '0')
    window = sources.report_rc_window('2026-09-18', cache_only=True)
    coverage = sources.coverage_counts(window, ['603606.SH', ROWS[0]['code']], '2026-09-18')
    company = coverage['603606.SH']
    orgs = {r['org_name'] for r in window['rows'] if r['ts_code'] == '603606.SH'}
    assert company['organizations'] == len(orgs) == 1
    assert company['covered_days'] == 1 < company['trading_days']
    assert len(company['missing_days']) == company['trading_days'] - 1
    assert company['start'] == '2026-06-20' and company['end'] == '2026-09-18'
    assert coverage[ROWS[0]['code']]['organizations'] == 0
    assert budget_calls() == 1 and replay.calls == ['20260918']
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
    rendered_coverage = sources.coverage_counts(window, [r['code'] for r in captured['rows']], '2026-09-18')
    html = _render_two_pools(captured['rows'][:1], coverage=rendered_coverage)
    assert '2026-06-20–2026-09-18' in html
    assert f"{len(company['missing_days'])} 日无缓存" in html


def test_small_research_is_queued_only_after_q20_finishes(tmp_path):
    conn = db.connect(tmp_path / 'c.db')
    large = copy.deepcopy(ROWS[:1])
    small = copy.deepcopy(SMALL[:2])
    large = screening.link_research(conn, 1, large)
    # Existing captured momentum values are unnecessary here; presence prevents legacy backfill.
    large[0]['return_60d'] = None
    conn.execute('INSERT INTO batches (status,created_at,rule,q20,q5,s10,sq3) VALUES (?,?,?,?,?,?,?)',
                 ('researching', db.now(), json.dumps(batches.NEW_RULE), json.dumps(large), '[]', json.dumps(small), '[]'))
    assert not screening.tick(conn)
    assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1
    # A failed large research still completes its phase and does not prevent small deep dives.
    db.update_run(conn, large[0]['run_id'], status='failed')
    assert screening.tick(conn)
    batch = batches.load(conn.execute('SELECT * FROM batches').fetchone())
    assert batch['status'] == 'researching'
    assert all(c['run_id'] > large[0]['run_id'] for c in batch['s10'])
    assert all(conn.execute('SELECT status FROM runs WHERE id=?', (c['run_id'],)).fetchone()[0] == 'queued'
               for c in batch['s10'])
    assert not screening.tick(conn)
    conn.close()


@pytest.mark.parametrize('rule', [V12_RULE, batches.NEW_RULE], ids=['v12', 'v13'])
def test_small_simulation_shares_entry_prices_fees_and_cash_accounting(rule):
    codes = list(PRICES['entry']['prices'])
    batch = {'rule': rule, 'entry': PRICES['entry'], 'marks': {'latest': PRICES['mark']},
             'q20': [{'code': c} for c in codes[:2]], 'q5': codes[:2], 'l5': {'codes': codes[:2]},
             'sq3': codes[2:3], 'sl3': {'codes': codes[2:3]}}
    assert screening.entry_codes(batch) == codes[:3]
    result = batches.returns(batch)['values']
    expected = batches.group_return(PRICES['entry'], PRICES['mark'], codes[2:3], 3, batches.RULE['cost'])
    assert result['SQ3'] == result['SL3'] == expected
    empty = batches.returns(batch | {'sq3': [], 'sl3': {'codes': []}})['values']
    assert empty['SQ3'] == empty['SL3'] == pytest.approx(-batches.RULE['cost'])
    legacy = batches.returns(batch | {'rule': batches.RULE})['values']
    assert 'SQ3' not in legacy and 'SL3' not in legacy
    assert all(result[g] == legacy[g] for g in ('Q20', 'Q5', 'L5'))


@pytest.mark.parametrize('seats', [3, 5])
def test_empty_group_with_seats_is_cash(seats):
    assert batches.group_return(PRICES['entry'], PRICES['mark'], [], seats, batches.RULE['cost']) == pytest.approx(-0.002)
    assert batches.group_return(PRICES['entry'], PRICES['mark'], [], seats, 0) == 0
    assert batches.group_return(PRICES['entry'], PRICES['mark'], [], 0, batches.RULE['cost']) is None


@pytest.mark.parametrize('rule', [batches.RULE, V12_RULE, batches.NEW_RULE], ids=['v11', 'v12', 'v13'])
def test_cash_returns_render_while_missing_marks_and_zero_seats_stay_uncomputed(rule):
    batch = {'id': 1, 'status': 'tracking', 'created_at': '2026-09-18', 'rule': rule,
             'entry': PRICES['entry'], 'marks': {'latest': PRICES['mark']},
             'q20': [], 'q5': [], 'l5': {'codes': []}, 'sq3': [], 'sl3': {'codes': []}}
    results = batches.returns(batch)
    groups = ['L5', 'Q5'] + (['SQ3', 'SL3'] if 'small' in rule else [])
    assert all(results['values'][g] == pytest.approx(-rule['cost']) for g in groups)
    assert results['values']['Q20'] is None
    performance = charts.batch_returns(batch, results, {})
    assert performance['groups']['rows'][0]['text'] == '-0.20%'
    assert performance['groups']['rows'][2]['text'] == '—'
    assert '未计算' in performance['groups']['rows'][2]['tip']
    # Both legacy and dual-pool pages consume the same saved horizon result and pct macro.
    batch['marks']['horizons'] = {'6m': results}
    template = 'batch_v12.html' if 'small' in rule else 'batch.html'
    html = ENV.get_template(template).render(batch=batch, results=results, performance=performance,
                                             pools=[], names={}, frozen_by={})
    assert '<td class="num">-0.20%</td>' in html
    assert '<td class="num">—</td>' in html
    for missing in ({'entry': None}, {'marks': {}}, {'marks': {'latest': None}}):
        assert batches.returns(batch | missing) is None
        assert charts.batch_returns(batch | missing, None, {}) is None


def test_csi1000_mapping_and_shared_total_return_entry_derivation():
    assert screening.PRICE_INDEX['H00852.CSI'] == '000852.SH'
    assert 'H00852.CSI' in batches.NEW_RULE['benchmarks']
    assert 'H00852.CSI' not in batches.RULE['benchmarks']
    # The same entry arithmetic is validated on captured CSI800/300 rows. No CSI1000
    # response is available offline; do not relabel another index's prices as CSI1000.
    for code in batches.RULE['benchmarks']:
        total = MARKET['index_daily'][code][0]
        price = MARKET['index_daily'][screening.PRICE_INDEX[code]][0]
        assert screening.benchmark_entry_point(total['pre_close'], price['open'], price['pre_close']) == (
            total['pre_close'] * price['open'] / price['pre_close'])


def test_automatic_monthly_comparison_failure_never_requeues_paid_work(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / 'automatic.db')
    completed_pools(conn, large_passed=True, small_passed=False, small_draft=False)
    db.update_batch(conn, 1, request_id='monthly:2026-12')
    calls = []

    def failed_comparison(*args):
        # Transport-failure injection only; never call a model or invent a successful output.
        calls.append(1)
        raise screening.ScreenError('offline comparison transport failure')

    monkeypatch.setattr(screening, 'select', failed_comparison)
    assert screening.tick(conn)
    assert conn.execute('SELECT status FROM batches').fetchone()[0] == 'failed'
    assert not screening.tick(conn) and len(calls) == 1
    conn.close()


# --- Batch Small Pool Reading Tests ---

def _render_two_pools(small_rows, status="proposed", coverage=None):
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
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
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
    html = _render_two_pools(captured['rows'][:1])
    assert html.count('action="/batches/1/freeze"') == 1
    assert 'name="codes"' in html and 'name="small_codes"' in html
    assert 'id="small-candidates"' in html and 'id="small-comparison"' in html
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert '尚未记录（冻结时保存）' in html
    assert '两组互不挤占，空位按现金计；小市值组可留空' in html


def test_empty_small_pool_keeps_large_freeze_available():
    html = _render_two_pools([])
    assert 'name="codes"' in html and '冻结两组所选' in html
    assert 'id="small-pool"' in html and 'id="small-comparison"' in html
    assert 'id="small-candidates"' not in html


def test_unlinked_small_candidates_wait_without_query_or_finished_count():
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
    row = {k: v for k, v in captured['rows'][0].items()
           if k not in ('run_id', 'research_status', 'report_id', 'verdict')}
    with sqlite3.connect(':memory:') as conn:
        finished, rows = reading.research_state(conn, [row])
    assert not finished and rows[0]['research_status'] == 'pending'
    html = _render_two_pools(rows, status='researching')
    large_finished = sum(r.get('research_status') not in ('pending', 'queued', 'running')
                         for r in captured['rows'])
    assert f'{large_finished} / {len(captured["rows"]) + 1} 家已完成' in html
    assert '等待大市值组研究完成后排队' in html
    small_matrix = html.split('id="small-candidates"', 1)[1].split('</table>', 1)[0]
    assert '未通过复核或研究失败' not in small_matrix


def test_failed_task_remains_linked_without_report():
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
    row = dict(captured['rows'][0], research_status='failed', report_id=None, verdict=None,
               run_id=987, research_error='失败状态变体')
    html = _render_two_pools([row], status='researching')
    small_matrix = html.split('id="small-candidates"', 1)[1].split('</table>', 1)[0]
    assert 'href="/runs/987"' in small_matrix
    assert 'href="/runs/987">查看失败详情' in html
