"""Retry the captured Maotai feedback in an isolated store; no worker/model runs."""
import importlib
import json
import sys

import pytest
from starlette.requests import Request

import a_share_research
from a_share_research import db, reading, service
from a_share_research.login_token import mint
from support.continuation import load, RECORDS
from support.templates import render


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    monkeypatch.setenv('DEV_LOGIN', '1')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8627')
    monkeypatch.setenv('SESSION_SECRET', 'isolated-feedback-retry-' * 2)
    monkeypatch.setattr(a_share_research, 'web', None, raising=False)
    monkeypatch.delitem(sys.modules, 'a_share_research.web', raising=False)
    module = importlib.import_module('a_share_research.web')
    load(module.conn)
    yield module
    module.conn.close()


def browser_request(web):
    token = mint(web.conn, 1)
    return Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/', 'query_string': b'',
                    'headers': [(b'cookie', f'session={token}'.encode())], 'server': ('127.0.0.1', 8627)}), web.csrf_token(web._hash(token))


def test_failed_feedback_form_submission_retry_failure_then_success_closes_group(web):
    request, csrf = browser_request(web)
    original = dict(web.conn.execute('SELECT * FROM reports WHERE id=12').fetchone())
    payload = json.loads(web.conn.execute('SELECT request FROM runs WHERE id=18').fetchone()[0])
    web.conn.execute('DELETE FROM reports WHERE id=12')
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=18")
    html = web.run_page(request, 18).body.decode()
    assert 'action="/runs/18/retry-feedback"' in html and '重试原核查' in html
    assert 'action="/research"' not in html
    before = web.conn.total_changes
    assert web.retry_feedback(request, 18, request_id='retry-a', csrf='invalid').status_code == 403
    assert web.conn.total_changes == before
    response = web.retry_feedback(request, 18, request_id='retry-a', csrf=csrf)
    assert response.status_code == 303
    first = int(response.headers['location'].split('/')[-1])
    assert json.loads(web.conn.execute('SELECT request FROM runs WHERE id=?', (first,)).fetchone()[0]) == payload
    assert web.retry_feedback(request, 18, request_id='retry-a', csrf=csrf).headers['location'] == response.headers['location']
    assert [r['id'] for r in reading.pending_reviews(web.conn)[0]] == [first]
    # Local result stub: the first retry fails, without a worker or model.
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=?", (first,))
    second = int(web.retry_feedback(request, first, request_id='retry-b', csrf=csrf).headers['location'].split('/')[-1])
    # A successful ordinary study cannot close the explicitly different request.
    ordinary = service.submit(web.conn, '600519', 'ordinary-again')
    web.conn.execute('UPDATE reports SET run_id=? WHERE id=5', (ordinary,))
    web.conn.execute("UPDATE runs SET status='done' WHERE id=?", (ordinary,))
    assert [r['id'] for r in reading.pending_reviews(web.conn)[0]] == [second]
    # Return the actual captured successful response as the retry's isolated result.
    original['run_id'] = second
    web.conn.execute(f"INSERT INTO reports ({','.join(original)}) VALUES ({','.join('?' for _ in original)})", tuple(original.values()))
    web.conn.execute("UPDATE runs SET status='done' WHERE id=?", (second,))
    assert reading.pending_reviews(web.conn) == ([], False)
    assert web.run_page(request, second).headers['location'] == '/reports/12'


def test_failed_feedback_with_report_also_has_retry_and_revalidates_parent(web):
    request, csrf = browser_request(web)
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=18")
    body = json.loads(web.conn.execute('SELECT body FROM reports WHERE id=12').fetchone()[0])
    body['facts'] = {}
    web.conn.execute('UPDATE reports SET passed=0,body=? WHERE id=12', (json.dumps(body),))
    assert web.run_page(request, 18).headers['location'] == '/reports/12'
    assert 'action="/runs/18/retry-feedback"' in web.report_page(request, 12).body.decode()
    before = web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
    web.conn.execute('DELETE FROM reports WHERE id=11')
    assert web.retry_feedback(request, 18, request_id='missing-parent', csrf=csrf).status_code == 404
    assert web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == before


def test_retry_rejects_ordinary_completed_replay_and_incomplete_records(web):
    for run_id in (17, 18):  # ordinary and completed feedback
        with pytest.raises(service.RequestError):
            service.retry_feedback(web.conn, run_id, 'invalid')
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=17")
    request, _ = browser_request(web)
    web.conn.execute('DELETE FROM reports WHERE id=11')
    html = web.run_page(request, 17).body.decode()
    assert 'action="/research"' in html and '/retry-feedback' not in html
    web.conn.execute("UPDATE runs SET status='failed',identity=? WHERE id=18", (json.dumps({'replay_of': 18}),))
    with pytest.raises(service.RequestError): service.retry_feedback(web.conn, 18, 'replay')
    web.conn.execute("UPDATE runs SET identity=NULL,request=json_remove(request, '$.feedback') WHERE id=18")
    with pytest.raises(service.RequestError): service.retry_feedback(web.conn, 18, 'missing-feedback')


def test_home_search_stays_visible_and_question_fold_keeps_typed_text():
    for pending, error, question in [([], None, None), ([{'id': 18, 'parent_id': 11}], None, None),
                                      ([{'id': 18, 'parent_id': 11}], '请重新选择', '现金流是否一致？')]:
        html = render('home.html', pending=pending, pending_more=False, companies=[], batch_only=0,
                      active=[], batch=None, error=error, question=question)
        # The search row is never folded, whatever the pending state; a typed question reopens its fold.
        assert '<section class="start"' in html and 'id="q" name="q"' in html
        assert ('<details class="question" open>' in html) == bool(question)
        assert (error or '') in html


def test_gap_disclosure_and_native_source_navigation():
    record = next(r['report'] for r in RECORDS if r['report']['id'] == 39)
    for query, opened in [(b'scope=1', 'research-scope'), (b'evidence=1', 'evidence')]:
        request = Request({'type': 'http', 'query_string': query})
        html = render('report.html', request=request, report=record, body=record['body'], feedback=None,
                      feedback_label='', replay_of=None, rows={}, price=None, followups=[], comparison={}, market=[], fin=[])
        assert '本页未重新核验；可能包含程序提示和模型记录' in html
        assert f'id="{opened}" tabindex="-1" open' in html
        assert '?scope=1#research-scope' in html and '?evidence=1#evidence' in html
