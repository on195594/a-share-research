"""Retry the captured Maotai feedback in an isolated store; no worker/model runs."""
import importlib
import json
import sys
from datetime import datetime, timedelta

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
    original.pop('company_key')  # generated; cannot be written back
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
    assert 'action="/runs/17/retry"' in html and '/retry-feedback' not in html
    web.conn.execute("UPDATE runs SET status='failed',identity=? WHERE id=18", (json.dumps({'replay_of': 18}),))
    with pytest.raises(service.RequestError): service.retry_feedback(web.conn, 18, 'replay')
    web.conn.execute("UPDATE runs SET identity=NULL,request=json_remove(request, '$.feedback') WHERE id=18")
    with pytest.raises(service.RequestError): service.retry_feedback(web.conn, 18, 'missing-feedback')


def test_home_search_stays_visible_and_question_fold_keeps_typed_text():
    for pending, error, question in [([], None, None), ([{'id': 18, 'parent_id': 11}], None, None),
                                      ([{'id': 18, 'parent_id': 11}], '请重新选择', '现金流是否一致？')]:
        html = render('home.html', pending=pending, pending_more=False, companies=[],
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
        assert '本报告记录的缺口' in html
        assert f'id="{opened}" tabindex="-1" open' in html
        assert '?scope=1#research-scope' in html and '?evidence=1#evidence' in html


def test_retry_keeps_followup_and_price_kind_and_is_idempotent(web):
    request, csrf = browser_request(web)
    followup = service.submit(web.conn, '600519', 'followup-failure', question='现金流是否一致？', parent_report_id=11)
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=?", (followup,))
    assert followup in [r['id'] for r in reading.followups(web.conn, '600519.SH', 11)]
    assert followup in [r['id'] for r in reading.active_runs(web.conn)]
    before = web.conn.total_changes
    assert web.retry_run(request, followup, csrf='invalid').status_code == 403
    assert web.conn.total_changes == before
    response = web.retry_run(request, followup, csrf=csrf)
    retry = int(response.headers['location'].split('/')[-1])
    assert web.retry_run(request, followup, csrf=csrf).headers['location'] == response.headers['location']
    original = web.conn.execute('SELECT request FROM runs WHERE id=?', (followup,)).fetchone()[0]
    assert web.conn.execute('SELECT request FROM runs WHERE id=?', (retry,)).fetchone()[0] == original
    assert followup not in [r['id'] for r in reading.active_runs(web.conn)]
    price = db.create_run(web.conn, '600519.SH', 'price-failed', kind='price', status='failed')
    html = web.run_page(request, price).body.decode()
    assert '重试价格参考' in html and 'action="/research"' not in html
    new_price = service.retry_run(web.conn, price)
    assert service.retry_run(web.conn, price) == new_price
    assert web.conn.execute('SELECT kind FROM runs WHERE id=?', (new_price,)).fetchone()[0] == 'price'
    with pytest.raises(service.RequestError):
        service.retry_run(web.conn, new_price)


def test_failed_followup_report_retries_saved_question(web):
    request, _ = browser_request(web)
    # Captured report with only its task state changed to failed for the UI branch.
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=17")
    web.conn.execute("UPDATE reports SET passed=0,body=json_set(body, '$.facts', json('{}')) WHERE id=11")
    html = web.report_page(request, 11).body.decode()
    assert 'action="/runs/17/retry"' in html
    assert '重新研究</button>' not in html


def test_search_explains_fallback_and_null_legacy_request_renders(web):
    request, _ = browser_request(web)
    report = web.conn.execute('SELECT run_id FROM reports WHERE id=39').fetchone()
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=?", (report['run_id'],))
    web.conn.execute('UPDATE reports SET passed=0 WHERE id=39')
    old = json.loads(web.conn.execute('SELECT body FROM reports WHERE id=37').fetchone()[0])
    term = old['risks'][0]['text'][:12]
    html = web.research_library(request, content=term, page=1).body.decode()
    assert '最新正式报告' in html and '复核未通过' in html and '当前结果来自最近通过报告' in html
    assert 'id="search-query"' in html  # The current library uses one unified search box.
    # Captured old rows have JSON null, not SQL NULL, in request.
    assert web.report_page(request, 37).status_code == 200


def test_failed_task_leaves_home_list_once_asked_again_or_stale(web):
    ids = lambda: [r['id'] for r in reading.active_runs(web.conn)]
    fail = lambda run: web.conn.execute("UPDATE runs SET status='failed' WHERE id=?", (run,))
    # A feedback retry takes a fresh form request id, so only its saved request identifies it.
    feedback = service.submit(web.conn, '600519', 'feedback-failure', parent_report_id=11,
                              feedback={'location': 'summary', 'text': '核查一下'})
    fail(feedback)
    assert feedback in ids()
    fail(service.retry_feedback(web.conn, feedback, 'form-request-id'))
    assert feedback not in ids()
    # The ordinary research form takes a fresh request id too.
    plain = service.submit(web.conn, '600519', 'plain-failure')
    fail(plain)
    assert plain in ids()
    service.submit(web.conn, '600519.SH', 'asked-again')
    assert plain not in ids()
    # A different question does not answer the failed one.
    question = service.submit(web.conn, '600519', 'question-failure', question='现金流是否一致？')
    fail(question)
    fail(service.submit(web.conn, '600519', 'other-company-question', question='分红是否持续？'))
    assert question in ids()
    # Same question for another company is not a retry either.
    other = service.submit(web.conn, '603606', 'other-company', question='现金流是否一致？')
    assert question in ids() and other in ids()
    web.conn.execute("UPDATE runs SET created_at=? WHERE id=?",
                     ((datetime.now(db.SHANGHAI) - timedelta(days=reading.RECENT_FAILURE_DAYS + 1)).isoformat(timespec='seconds'), question))
    assert question not in ids()


def test_company_page_heads_formal_research_not_feedback(web):
    request, _ = browser_request(web)
    newest = next(r for r in reading.stock_reports(web.conn, '600519.SH') if not r['replay_of'])
    assert newest['id'] == 12 and newest['feedback_text']
    html = web.stock_page(request, '600519.SH').body.decode()
    assert 'href="/reports/11">阅读完整报告' in html
    # The feedback report stays reachable and can still be selected explicitly.
    assert '/reports/12' in html and web.stock_page(request, '600519.SH', report=12).status_code == 200


def test_retry_button_names_what_it_retries_and_batch_task_stays_detached(web):
    request, _ = browser_request(web)
    plain = service.submit(web.conn, '600519', 'plain-label')
    web.conn.execute("UPDATE runs SET status='failed', error='失败状态变体' WHERE id=?", (plain,))
    html = web.run_page(request, plain).body.decode()
    assert '重新运行这次研究' in html and '重试原研究问题' not in html and '不回填本批次' not in html
    batch = service.submit(web.conn, '600519', 'batch-7-600519', question='现金流是否一致？')
    web.conn.execute("UPDATE runs SET status='failed', error='失败状态变体' WHERE id=?", (batch,))
    html = web.run_page(request, batch).body.decode()
    assert '重试原研究问题' in html and '不回填本批次' in html
    web.conn.execute("UPDATE runs SET status='failed', request_id='batch-7-600519.SH' WHERE id=17")
    web.conn.execute("UPDATE reports SET passed=0,body=json_set(body, '$.facts', json('{}')) WHERE id=11")
    assert '不回填本批次' in web.report_page(request, 11).body.decode()


def test_historical_report_links_newer_formal_even_when_it_failed(web):
    request, _ = browser_request(web)
    html = web.report_page(request, 37).body.decode()
    assert '正在阅读历史报告' in html and '/reports/39' in html
    assert '正在阅读历史报告' not in web.report_page(request, 39).body.decode()
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=(SELECT run_id FROM reports WHERE id=39)")
    web.conn.execute('UPDATE reports SET passed=0 WHERE id=39')
    assert '最新正式研究（复核未通过）' in web.report_page(request, 37).body.decode()


def test_price_progress_is_not_research_progress(web):
    request, _ = browser_request(web)
    run = db.create_run(web.conn, '600519.SH', 'local-price-progress', kind='price', status='running')
    db.update_run(web.conn, run, step='price', started_at=db.now())
    html = web.run_page(request, run).body.decode()
    assert '正在获取行情并计算价格参考' in html
    assert 'aria-label="研究进度"' not in html and '撰写报告' not in html


def test_library_scope_survives_new_query_and_clears_explicitly(web):
    request, _ = browser_request(web)
    html = web.research_library(request, q='现金流', history=True, scope='all', page=2).body.decode()
    assert 'name="history" value="true"' in html and 'name="scope" value="all"' in html
    assert 'name="page"' not in html
    assert 'history=true' in html and 'scope=all' in html
    cleared = web.research_library(request, q='', history=True, scope='all', page=1).body.decode()
    assert 'name="history"' not in cleared and 'name="scope"' not in cleared
