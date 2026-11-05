"""Card cautions from the six captured 11-05 stability reports; never calls a model/provider."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from a_share_research import db, service, stance, valuation

CAPTURED = json.loads((Path(__file__).parent / 'data/valuation-warning-reports-20261105.json').read_text())['reports']


def saved_card(conn, record, *, day=None):
    """Keep the captured report unchanged; only candidate membership/current date are test inputs."""
    code = record['code'][:6]
    body = record['body']
    calc = body.get('valuation_calc') or {}
    run = service.submit(conn, code, 'cautions-' + str(record['id']), name=record['name'])
    db.save_report(conn, run, record['code'], record['name'], record['period'], True, record['verdict'], body)
    db.update_run(conn, run, status='done', finished_at=record['finished_at'])
    conn.execute('UPDATE reports SET created_at=? WHERE run_id=?', (record['created_at'], run))
    conn.execute("INSERT INTO batches(status,created_at,rule,l5,frozen_at) VALUES ('frozen',?,'{}',?,?)",
                 (record['created_at'], json.dumps({'codes': [record['code']]}), record['created_at']))
    if calc.get('close') is not None:
        conn.execute("INSERT INTO scans(status,requested_at,summary) VALUES ('done',?,?)",
                     (record['created_at'], json.dumps({'closes': {code: {
                         'last_close': calc['close'], 'last_close_date': day or calc['as_of']}}})))
    return service.company_card(conn, code)


@pytest.mark.parametrize('record', CAPTURED, ids=lambda r: str(r['id']))
def test_captured_report_and_current_boundary_reach_both_card_endpoints_unchanged(tmp_path, record):
    conn = db.connect(tmp_path / 'isolated.db')
    card = saved_card(conn, record)
    calc = record['body']['valuation_calc']
    frozen = card['valuation_calc']
    assert frozen['close'] == calc['close']
    assert frozen['v2']['support_below'] == calc['v2']['support_below']
    assert frozen['boundary'] is stance.on_boundary(calc)
    assert card['current']['valuation']['boundary'] is stance.on_boundary(calc)
    assert card['stance_calc'] == record['body']['stance_calc']
    assert card['open_questions'] == []  # no recorded unanswered issues, not a completeness certificate
    before = conn.total_changes
    conn.execute('PRAGMA query_only=ON')
    assert service.company_cards(conn)['cards'] == [card | {'origin': 'l5'}]
    assert conn.total_changes == before
    stored = json.loads(conn.execute('SELECT body FROM reports').fetchone()[0])
    assert stored == record['body']  # read-time flags never rewrite the frozen report
    conn.close()


@pytest.mark.parametrize('index,day,report_boundary,current_boundary', [
    (1, '2027-10-30', True, False),
    (5, '2027-01-30', False, True),
])
def test_rolling_date_cannot_erase_or_replace_the_report_time_classification(tmp_path, index, day, report_boundary, current_boundary):
    # Explicit date-only perturbation, with the captured close, scenarios and hurdle inputs unchanged.
    record = CAPTURED[index]
    conn = db.connect(tmp_path / 'isolated.db')
    card = saved_card(conn, record, day=day)
    assert card['valuation_calc']['boundary'] is report_boundary
    rolled = valuation.at(record['body']['valuation_calc'], record['body']['valuation_calc']['close'], day)
    assert card['current']['valuation'] == rolled | {'boundary': current_boundary}
    assert stance.on_boundary(rolled) is current_boundary
    conn.close()


@pytest.mark.parametrize('calc', [None, {'not_priced': True, 'as_of': '2026-11-04', 'close': 16.9}])
def test_unpriced_or_missing_frozen_basis_is_unknown_not_false(tmp_path, calc):
    record = deepcopy(CAPTURED[1])
    record['body']['valuation_calc'] = calc
    conn = db.connect(tmp_path / 'isolated.db')
    card = saved_card(conn, record)
    assert card['valuation_calc'] is None if calc is None else card['valuation_calc']['boundary'] is None
    current = card['current']['valuation']
    assert current is None if calc is None else current['boundary'] is None
    conn.close()
