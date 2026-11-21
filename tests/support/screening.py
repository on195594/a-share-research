"""Captured inputs for candidate pool and screening window replays."""

import copy
import json
from pathlib import Path

from a_share_research import batches, db, market
from support.market_replay import DailyReports

DATA = Path(__file__).resolve().parents[1] / 'data'
ROWS = json.loads((DATA / 'screen-rows.json').read_text())['rows']
SMALL = json.loads((DATA / 'screen-small-rows.json').read_text())['small_companies']


def cache_partial_window(monkeypatch):
    monkeypatch.delenv('TUSHARE_CACHE', raising=False)
    monkeypatch.delenv('TUSHARE_TOKEN', raising=False)
    replay = DailyReports()
    market.query(replay, 'trade_cal', exchange='SSE', is_open='1',
                          start_date='20260620', end_date='20260918')
    market.query(replay, 'report_rc', start_date='20260918', end_date='20260918')
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


def completed_pools(conn, *, large_passed, small_passed, rule=batches.NEW_RULE, small_draft=True):
    """Replay observed candidate verdicts; mutate review outcomes to exercise sequencing.

    Pool membership is only a workflow fixture, not a claim about market-cap eligibility.
    Saved small picks exercise restart/resume without a live model completion.
    """
    captured = json.loads((DATA / 'batch-candidate-matrix.json').read_text())
    large, small = copy.deepcopy(captured['rows'][3:4]), copy.deepcopy(captured['rows'][:1])
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
