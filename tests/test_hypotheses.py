"""V3 uses captured production records and prices; only eligibility metadata is mutated."""
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from a_share_research import batches, hypotheses, valuation

DATA = Path(__file__).with_name('data')
CAPTURE = json.loads((DATA / 'hypotheses_v3.json').read_text())
V0 = json.loads((DATA / 'validation_v0.json').read_text())
OLD = json.loads((DATA / 'valuation-stored-20261101.json').read_text())['records'][2]['valuation_calc']
NEW = json.loads(next(r['body'] for r in CAPTURE['reports'] if r['id'] == 70))['valuation_calc']


@pytest.fixture
def conn():
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    for table in ('runs', 'reports', 'scan_events'):
        rows = CAPTURE[table]
        columns = list(rows[0])
        database.execute(f"CREATE TABLE {table} ({','.join(columns)})")
        database.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})",
                             [[r[c] for c in columns] for r in rows])
    yield database
    database.close()


def test_direction_uses_two_real_600900_calcs_at_one_real_close():
    # Old calc is the 11-01 evaluation capture, not asserted to be the live prior card.
    day, close = '2026-11-02', NEW['close']
    result = hypotheses.direction(OLD, NEW, close, day)
    expected = valuation.at(NEW, close, day)['annual_returns']['中性'] - valuation.at(OLD, close, day)['annual_returns']['中性']
    assert result['delta'] == pytest.approx(expected)
    assert result['direction'] == '下调'
    assert hypotheses.direction(NEW, OLD, close, day)['direction'] == '上调'
    assert hypotheses.direction(NEW, NEW, close, day)['direction'] == '不变'


@pytest.mark.parametrize(('delta', 'expected'), [(0.01, '上调'), (-0.01, '下调'), (0.009999999, '不变'), (-0.009999999, '不变')])
def test_flat_boundary_is_strict(delta, expected):
    # Threshold parameters, not invented market observations.
    assert hypotheses.direction_of(delta) == expected


def test_at_reason_and_real_not_priced_are_excluded():
    assert 'reason' in hypotheses.direction(OLD, NEW, NEW['close'], NEW['exit_date'])
    not_priced = json.loads(next(r['body'] for r in CAPTURE['reports'] if r['id'] == 74))['valuation_calc']
    assert 'reason' in hypotheses.direction(OLD, not_priced, NEW['close'], '2026-11-02')
    assert 'reason' in hypotheses.direction(None, NEW, NEW['close'], '2026-11-02')


def test_prefix_match_and_true_prior_card(conn):
    (sample,) = hypotheses.load_samples(conn)
    assert sample['run_id'] == 88 and sample['report'] == 70
    assert sample['code'] == '600900.SH'  # event.code is six digits
    assert sample['prior_report'] == 48
    assert sample['prior_calc'] is None  # Do not silently replace an unpriced latest card.
    assert sample['doc_keys'] == ['1225648852']
    assert sample['announced'] == '2026-10-31'


def test_one_run_many_events_keeps_earliest_and_all_documents(conn):
    # Link another captured disclosure to the same run; no invented document/date.
    conn.execute("UPDATE scan_events SET run_id=88,action='queued_review',code='600900' WHERE id=6")
    (sample,) = hypotheses.load_samples(conn)
    assert sample['announced'] == '2026-10-15'
    assert set(sample['doc_keys']) == {'1225648852', '1225605190'}


def test_prior_card_excludes_feedback_replay_and_completion_at_cutoff(conn):
    original = hypotheses.load_samples(conn)[0]
    # Eligibility mutations of real records. Feedback report 10 is newer than report 4.
    conn.execute('UPDATE runs SET replay_of=1 WHERE id=(SELECT run_id FROM reports WHERE id=48)')
    assert hypotheses.load_samples(conn)[0]['prior_report'] == 4
    conn.execute('UPDATE runs SET replay_of=NULL, finished_at=? WHERE id=(SELECT run_id FROM reports WHERE id=48)',
                 (original['created'],))
    assert hypotheses.load_samples(conn)[0]['prior_report'] == 4  # strictly before, not <=
    conn.execute("UPDATE runs SET kind='correction' WHERE id=(SELECT run_id FROM reports WHERE id=4)")
    assert hypotheses.load_samples(conn)[0]['prior_report'] != 4


def market(batch):
    mark = batch['marks']['latest']
    return [mark['date']], {mark['date']: mark['benchmarks']['H00906.CSI']['value']}, {
        code: {mark['date']: value['close'] * value['adj']} for code, value in mark['prices'].items()}


def test_h1_real_open_to_close_and_bottom_minus_top():
    batch = CAPTURE['batch']
    result = hypotheses.score_batch(batch, *market(batch))
    groups = result['horizons']['latest']
    top, bottom = batches.ranking_halves(batch)
    mark = batch['marks']['latest']
    def mean(codes):
        return sum(mark['prices'][code]['close'] * mark['prices'][code]['adj'] /
                   (batch['entry']['prices'][code]['open'] * batch['entry']['prices'][code]['adj']) - 1
                   for code in codes) / len(codes)
    assert groups['difference'] == pytest.approx(mean(bottom) - mean(top))
    assert groups['difference'] > 0  # frozen real 11-02 open/close capture
    assert groups['TOP']['n'] == len(top) and groups['BOTTOM']['n'] == len(bottom)
    assert all(set(row['returns']) == {'latest'} for row in result['rows'])
    benchmark_return = mark['benchmarks']['H00906.CSI']['value'] / batch['entry']['benchmarks']['H00906.CSI']['value'] - 1
    assert groups['TOP']['mean'] == pytest.approx(mean(top) - benchmark_return)


def test_h1_unfilled_counted_not_treated_as_zero_return():
    batch = deepcopy(CAPTURE['batch'])
    code = batches.ranking_halves(batch)[0][0]
    batch['entry']['prices'][code]['filled'] = False  # execution eligibility, prices unchanged
    result = hypotheses.score_batch(batch, *market(batch))
    assert result['unfilled'] == {'TOP': 1, 'BOTTOM': 0}
    assert code not in [row['code'] for row in result['rows']]
    assert result['horizons']['latest']['TOP']['n'] == 8


def test_h1_missing_entry_benchmark_fails_batch():
    batch = deepcopy(CAPTURE['batch'])
    del batch['entry']['benchmarks']['H00906.CSI']
    result = hypotheses.score_batch(batch, *market(batch))
    assert 'H00906.CSI' in result['reason']


def test_h1_maturity_uses_last_benchmark_not_calendar_end():
    cal = V0['trade_cal']
    # Eligibility dates are parameters; sessions come from the real TuShare calendar.
    before = hypotheses.h1_dates('2026-07-26', [3, 6, 12], cal, '20261023')
    mature = hypotheses.h1_dates('2026-07-26', [3, 6, 12], cal, '20261026')
    assert '3m' not in before
    assert mature['3m'] == '20261026'
    assert '6m' not in mature and '12m' not in mature
    assert mature['latest'] == '20261026'


def test_h1_summary_pools_real_batch_and_does_not_call_latest_matured():
    scored = hypotheses.score_batch(CAPTURE['batch'], *market(CAPTURE['batch']))
    summaries = hypotheses.summarize_h1([scored])
    summary = next(g for g in summaries if g['horizon'] == 'latest')
    assert all(g['matured_batches'] == 0 for g in summaries)
    assert {g['horizon'] for g in summaries} == {'3m', '6m', '12m', 'latest'}
    groups = scored['horizons']['latest']
    assert summary['batches'] == 1 and summary['matured_batches'] == 0
    assert summary['mean'] == pytest.approx(groups['difference'])
    assert summary['median'] == pytest.approx(groups['difference'])
    assert summary['bottom_below_top'] == 0
    assert summary['left_tail_difference'] == pytest.approx(groups['BOTTOM']['left_tail'] - groups['TOP']['left_tail'])


def test_h3_real_unpriced_prior_is_excluded_before_waiting_for_market(conn):
    sample = hypotheses.load_samples(conn)[0]
    row = hypotheses.score_sample(sample, [], {}, {}, {})
    assert row['latency_days'] == 2 and row['late'] is False
    assert row['reason'] and row['result'] is None
    assert all(group['n'] == 0 for group in hypotheses.summarize_h3([row]))


def test_h3_pending_and_latency_boundary_with_real_fixed_price(conn):
    sample = hypotheses.load_samples(conn)[0]
    # Replay eligibility at the captured close's session; no price or calc alteration.
    sample |= {'prior_calc': OLD, 'completed': '2026-11-02T09:00:00+08:00', 'announced': '2026-10-23'}
    days, bench, _ = market(CAPTURE['batch'])
    raw = {days[0]: NEW['close']}
    row = hypotheses.score_sample(sample, days, bench, raw, {})
    assert row['entry'] == '20261102' and row['direction'] == '下调'
    assert row['latency_days'] == 10 and row['late'] is False
    assert row['consistent'] is None and row['result'] is None
    summary = next(g for g in hypotheses.summarize_h3([row]) if g['direction'] == '下调')
    assert summary['n'] == 1 and summary['pending'] == 1 and summary['matured'] == 0
    assert summary['median_latency'] == 10 and summary['consistency'] is None
    late = hypotheses.score_sample(sample | {'announced': '2026-10-22'}, days, bench, raw, {})
    assert late['late'] is True
    assert next(g for g in hypotheses.summarize_h3([late]) if g['direction'] == '下调')['late'] == 1


def test_h3_mature_outcome_direction_and_rate_denominator(conn):
    # Real 61-session stock/CSI300 capture tests the pure scoring clock/sign only;
    # this is NOT a production H00906 outcome, nor an investment validation sample.
    capture = json.loads((DATA / '600900-61d.json').read_text())
    raw = {r['trade_date']: r['close'] for r in capture['stock']}
    bench = {r['trade_date']: r['close'] for r in capture['benchmark']}
    days = sorted(raw)
    sample = hypotheses.load_samples(conn)[0] | {
        'prior_calc': OLD, 'followup_calc': NEW,
        'completed': '2026-07-20T09:00:00+08:00', 'announced': '2026-07-10'}
    row = hypotheses.score_sample(sample, days, bench, raw, raw)  # no ex-rights in capture
    assert row['result']['date'] == days[60]
    assert row['result']['excess'] > 0 and row['direction'] == '下调'
    assert row['consistent'] is False
    up = hypotheses.score_sample(sample | {'prior_calc': NEW, 'followup_calc': OLD}, days, bench, raw, raw)
    assert up['direction'] == '上调' and up['consistent'] is True
    late = hypotheses.score_sample(sample | {'announced': '2026-07-09'}, days, bench, raw, raw)
    flat = hypotheses.score_sample(sample | {'prior_calc': NEW}, days, bench, raw, raw)
    assert late['result'] is not None and late['consistent'] is None
    assert flat['direction'] == '不变' and flat['consistent'] is None
    summary = {g['direction']: g for g in hypotheses.summarize_h3([row, up, late, flat])}
    assert summary['下调']['n'] == summary['下调']['matured'] == 2
    assert summary['下调']['eligible'] == 1 and summary['下调']['consistency'] == 0
    assert summary['上调']['consistency'] == 1
    assert summary['不变']['eligible'] == 0 and summary['不变']['consistency'] is None
    # A lagging benchmark prevents maturation even when the stock calendar has 61 bars.
    pending = hypotheses.score_sample(sample, days, {d: v for d, v in bench.items() if d < days[-1]}, raw, raw)
    assert pending['result'] is None
