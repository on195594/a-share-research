"""Producer filtering precedes dedup; financial content is from captured annual-report excerpts."""
import json

import pytest

from a_share_research import agenda, db, external_scoring as external, validation
from support.reports import MATERIAL, report


def save(conn, *, key=1, aware=False, contract=1, kind='external', status='done', passed=True):
    # Test-only identity variants of the same captured evidence and prediction.
    run = db.create_run(conn, MATERIAL.code, kind=kind, status=status)
    body = report() | {'table': MATERIAL.table, 'judgment': {'contract': contract},
                       'producer': {'kind': 'external', 'key_id': key, 'account_aware': aware, 'submission_id': run},
                       'prior': {'period': '2025-09-30'}, 'prior_status': []}
    cursor = conn.execute('INSERT INTO reports(run_id,code,name,period,passed,body,created_at,coverage) VALUES (?,?,?,?,?,?,?,?)',
                         (run, MATERIAL.code, MATERIAL.name, '2025-12-31', int(passed), json.dumps(body),
                          f'2026-11-11T12:00:{run:02d}+08:00', '2025-12-31'))
    return cursor.lastrowid


def test_producer_and_contract_filtering_before_forecast_and_agenda_dedup(tmp_path):
    path = str(tmp_path / 'app.db')
    conn = db.connect(tmp_path / 'app.db')
    foreign = save(conn, key=2)
    aware = save(conn, aware=True)
    old = save(conn, contract=2)
    selected = save(conn)
    update = save(conn)
    worker = save(conn, kind='research')
    save(conn, status='failed')
    save(conn, passed=False)
    conn.close()
    items = external.load_forecasts(path, key_id=1, account_aware=False, contract=1)
    assert [(i['report'], i['first']) for i in items] == [(selected, True), (update, False)]
    assert all(i['producer']['key_id'] == 1 and i['producer']['account_aware'] is False for i in items)
    assert external.load_forecasts(path, key_id=2, account_aware=False)[0]['report'] == foreign
    assert external.load_forecasts(path, key_id=1, account_aware=True)[0]['report'] == aware
    assert external.load_forecasts(path, key_id=1, account_aware=False, contract=2)[0]['report'] == old
    rows = external.load_agenda(path, key_id=1, account_aware=False, contract=1)
    assert [r['id'] for r in agenda.sample(rows)['picked']] == [selected]
    assert rows[0]['producer']['key_id'] == 1
    # Legacy loaders still see only the worker cohort, never external reports.
    assert {i['report'] for i in validation.load_forecasts(path)} == {worker}
    assert {r['id'] for r in validation.load_agenda(path)} == {worker}
    assert external.load_forecasts(path, key_id=99, account_aware=False) == []


@pytest.mark.parametrize('key,aware', [(True, False), (0, False), (1, 0), (1, None)])
def test_explicit_valid_cohort_required(tmp_path, key, aware):
    with pytest.raises(ValueError):
        external.load_forecasts(str(tmp_path / 'missing.db'), key_id=key, account_aware=aware)
