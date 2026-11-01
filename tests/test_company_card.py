"""A2 projections and localhost HTTP use real saved reports; no worker/model calls."""
from copy import deepcopy
import json

import pytest

from a_share_research import db, service, valuation
from support.continuation import load
from support.valuation import BODY, RECORD, ROWS, assumptions
from test_service_http import server  # Existing isolated real HTTP fixture.


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
    with pytest.raises(service.RequestError) as error:
        service.company_card(conn, '603606.SH')
    assert error.value.status == 422
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
