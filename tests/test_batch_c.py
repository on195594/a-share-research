"""Batch C storage, sequencing and accounting using captured screening/report/market rows."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from a_share_research import batches, db, screening, service, sources
from test_advisor_sources import DailyReports, budget_calls

DATA = Path(__file__).parent / 'data'
ROWS = json.loads((DATA / 'screen-rows.json').read_text())['rows']
SMALL = json.loads((DATA / 'screen-small-rows.json').read_text())['small_companies']
PRICES = json.loads((DATA / 'screen-prices.json').read_text())
MARKET = json.loads((DATA / 'screen-entry-market.json').read_text())


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


def test_only_new_batches_receive_v12_and_existing_rows_survive_v8(tmp_path):
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
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 8
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
    from test_batch_small_reading import render_two_pools
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
    rendered_coverage = sources.coverage_counts(window, [r['code'] for r in captured['rows']], '2026-09-18')
    html = render_two_pools(captured['rows'][:1], coverage=rendered_coverage)
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


def test_small_simulation_shares_entry_prices_fees_and_cash_accounting():
    codes = list(PRICES['entry']['prices'])
    batch = {'rule': batches.NEW_RULE, 'entry': PRICES['entry'], 'marks': {'latest': PRICES['mark']},
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
