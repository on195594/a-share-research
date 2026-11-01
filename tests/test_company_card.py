"""A2 projections and localhost HTTP use real saved reports; no worker/model calls."""
from copy import deepcopy
import json

import pytest

from a_share_research import db, scans, service, valuation
from support.continuation import load
from support.valuation import BODY, RECORD, ROWS, assumptions
from test_service_http import server  # Existing isolated real HTTP fixture.
from test_scans import AT, CATALOG, CapturedSources, complete_cards_at, priced, run_scan


def test_card_latest_passed_non_feedback_and_read_only(tmp_path):
    conn = db.connect(tmp_path / 'card.db')
    load(conn)
    body = deepcopy(BODY)
    body['valuation'] = assumptions()
    body['valuation_calc'] = valuation.compute(body['valuation'], body['table'], body['market_date'],
                                               consensus=valuation.consensus(ROWS, [2026, 2027, 2028], body['market_date']))
    # Add the new fields to an actual saved report using the normal persistence API.
    run = service.submit(conn, '603606', 'a2-current')
    report_id = db.save_report(conn, run, RECORD['code'], RECORD['name'], RECORD['period'], True, RECORD['verdict'], body)
    db.update_run(conn, run, status='done')
    report_id = conn.execute('SELECT id FROM reports WHERE run_id=?', (run,)).fetchone()[0]
    total = conn.total_changes
    card = service.company_card(conn, '603606')
    assert conn.total_changes == total
    assert card['status'] == '有效' and card['report_id'] == report_id
    assert card['period'] == RECORD['period'] and card['summary'] == body['summary']
    assert card['risks'] == [r['text'] for r in body['risks'][:3]]
    assert card['valuation_calc']['zone'] == body['valuation_calc']['zone']
    assert card['valuation_calc']['consensus']['status'] == 'ok'
    assert card['valuation_calc']['scenarios']['中性'] == {'annual_return': body['valuation_calc']['scenarios']['中性']['annual_return']}
    compact = service.report_view(conn, report_id, compact=True)['report']
    assert compact['valuation'] == body['valuation'] and compact['valuation_calc'] == body['valuation_calc']
    # Later feedback, failed, running, and replay rows must not replace the reviewed research.
    for label in ['feedback', 'failed', 'running', 'replay']:
        newer = service.submit(conn, '603606', 'a2-' + label)
        db.save_report(conn, newer, RECORD['code'], RECORD['name'], RECORD['period'], label != 'failed', RECORD['verdict'], body)
        db.update_run(conn, newer, status='running' if label == 'running' else 'failed' if label == 'failed' else 'done')
        if label == 'feedback':
            conn.execute('UPDATE runs SET request=? WHERE id=?', (json.dumps({'feedback': {'location': 'summary', 'text': body['summary']}}), newer))
        if label == 'replay':
            conn.execute('UPDATE runs SET replay_of=? WHERE id=?', (run, newer))
        assert service.company_card(conn, '603606')['report_id'] == report_id
    conn.close()


def test_legacy_card_and_no_report(tmp_path):
    conn = db.connect(tmp_path / 'legacy.db')
    with pytest.raises(service.RequestError) as error:
        service.company_card(conn, '603606')
    assert error.value.status == 404
    load(conn)
    card = service.company_card(conn, '600519')
    assert card['valuation_calc'] is None
    assert card['summary'] and card['risks']
    assert card['completed_at'] == card['created_at']
    with pytest.raises(service.RequestError) as error:
        service.company_card(conn, '603606.SH')
    assert error.value.status == 422
    conn.close()


def test_card_completion_uses_run_finished_at(tmp_path):
    conn = db.connect(tmp_path / 'completed.db')
    load(conn)
    card = service.company_card(conn, '603606')
    # Explicitly adjust the captured run's finish time to distinguish it from report creation.
    finished_at = '2026-10-28T18:00:00+08:00'
    conn.execute('UPDATE runs SET finished_at=? WHERE id=(SELECT run_id FROM reports WHERE id=?)',
                 (finished_at, card['report_id']))
    updated = service.company_card(conn, '603606')
    assert updated['completed_at'] == finished_at
    assert updated['created_at'] == card['created_at'] != finished_at
    assert updated['report_id'] == card['report_id']
    conn.close()


@pytest.mark.parametrize(('failed_at', 'pending'), [
    ('2026-11-01T01:59:59+00:00', False),
    ('2026-11-01T02:00:00+00:00', False),
    ('2026-11-01T02:00:01+00:00', True),
])
def test_manual_passed_card_supersedes_old_failed_scan_review(tmp_path, failed_at, pending):
    conn = db.connect(tmp_path / 'manual-card.db')
    load(conn)
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    first = run_scan(conn)
    failed_run, = first['summary']['queued_runs']
    db.update_run(conn, failed_run, status='failed', error='复核未通过',
                  finished_at='2026-10-31T10:00:00+08:00')
    assert service.company_card(conn, '603606')['status'] == '待复核'
    manual = service.submit(conn, '603606', 'manual-after-failure')
    db.save_report(conn, manual, RECORD['code'], RECORD['name'], RECORD['period'],
                   True, RECORD['verdict'], deepcopy(BODY))
    report = conn.execute('SELECT id FROM reports WHERE run_id=?', (manual,)).fetchone()[0]
    completed_at = '2026-11-01T10:00:00+08:00'
    conn.execute('UPDATE reports SET created_at=? WHERE id=?', (completed_at, report))
    db.update_run(conn, manual, status='done', finished_at=completed_at)
    db.update_run(conn, failed_run, finished_at=failed_at)
    total = conn.total_changes
    card = service.company_card(conn, '603606')
    assert conn.total_changes == total
    assert card['report_id'] == report and card['completed_at'] == completed_at
    assert card['status'] == ('待复核' if pending else '有效')
    assert bool(card['pending_events']) == pending
    assert ('note' in card['current']) == pending
    if pending:
        assert card['pending_events'][0]['run_id'] == failed_run
    conn.close()


@pytest.mark.parametrize('valuation_fails', [False, True])
def test_announcement_failure_pauses_card_until_same_code_recovers(tmp_path, valuation_fails):
    conn = db.connect(tmp_path / 'scan-failure.db')
    load(conn)
    complete_cards_at(conn, at=AT)
    priced(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    source = CapturedSources()
    next(i for i in source.items if i['code'] == '603606')['raw']['announcementTime'] = None
    first = run_scan(conn, source)
    assert first['status'] == 'done' and not first['events']
    changes = conn.total_changes
    card = service.company_card(conn, '603606')
    assert conn.total_changes == changes
    assert card['status'] == '待复核'
    assert card['current']['note'] == '公告扫描未完成，区间暂停解读'
    assert card['current']['last_close'] == first['summary']['closes']['603606']['last_close']
    failure, = card['pending_events']
    assert failure == {'kind': 'scan_failure', 'scan_id': first['id'], **first['summary']['failures'][0]}
    assert 'run_id' not in failure and 'doc_key' not in failure
    assert first['summary']['cards']['603606'] == card
    assert any(n['reason'] == 'pending_review' for n in first['notify'])
    scans.replace_subscriptions(conn, ['600519'], catalog=CATALOG)
    assert run_scan(conn)['status'] == 'done'
    assert service.company_card(conn, '603606')['pending_events'] == [failure]
    # A later interrupted scan with only a scope is not evidence of successful collection.
    partial = scans.submit(conn)
    conn.execute("UPDATE scans SET status='failed',summary=? WHERE id=?",
                 (json.dumps({'scope': [{'code': '603606'}]}), partial))
    assert service.company_card(conn, '603606')['pending_events'] == [failure]

    class InvalidCloseCapture(CapturedSources):
        def valuation(self, code):
            return super().valuation(code) | {'trade_date': None}

    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    recovered = run_scan(conn, InvalidCloseCapture() if valuation_fails else CapturedSources())
    assert recovered['status'] == 'done'
    assert [f['stage'] for f in recovered['summary']['failures']] == (['valuation'] if valuation_fails else [])
    card = service.company_card(conn, '603606')
    assert card['status'] == '有效' and card['pending_events'] == []
    assert 'note' not in card['current']
    assert recovered['summary']['cards']['603606'] == card
    conn.close()


def test_card_http_authentication_read_only_and_404(server):
    conn, client, url, run, report, failed, queued = server
    before = conn.total_changes
    route = url + '/api/companies/600519/card'
    assert client.get(route, headers={'Authorization': 'Bearer invalid'}, timeout=3).status_code == 401
    assert client.get(url + '/api/companies/603606/card', timeout=3).status_code == 404
    assert client.get(url + '/api/companies/bad/card', timeout=3).status_code == 422
    response = client.get(route, timeout=3)
    assert response.status_code == 200
    assert response.json()['report_id'] == report and response.json()['version'] == 'research-v1'
    assert conn.total_changes == before
