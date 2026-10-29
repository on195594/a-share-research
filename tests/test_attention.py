"""Read-only attention derived from real captured requests, with lifecycle/absence boundaries."""
import copy
import json

import pytest

from a_share_research import db, reading
from test_continuation import BODY, load, render, RECORDS


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'attention.db')
    load(connection)
    yield connection
    connection.close()


def test_completed_requests_and_ordinary_risks_do_not_become_tasks(conn):
    before = conn.total_changes
    assert reading.pending_reviews(conn) == ([], False)
    # Future calendar time is not an input: ordinary risks, new report dates and
    # undated checkpoints never create pending requests.
    assert BODY[39]['checkpoints'] and BODY[39]['prior']
    assert conn.total_changes == before


@pytest.mark.parametrize('status', ['queued', 'running', 'failed', 'done'])
def test_saved_feedback_without_a_result_remains_explicit(conn, status):
    # Withhold the captured result, exercise lifecycle states without creating research text.
    conn.execute('DELETE FROM reports WHERE id=12')
    conn.execute('UPDATE runs SET status=? WHERE id=18', (status,))
    before = conn.total_changes
    rows, more = reading.pending_reviews(conn)
    assert not more and len(rows) == 1
    assert rows[0]['parent_id'] == 11 and rows[0]['id'] == 18 and rows[0]['anchor'] == '#answer'
    assert rows[0]['question'] == next(r['run']['request']['feedback']['text'] for r in RECORDS if r['report']['id'] == 12)
    assert 'body' not in rows[0] and conn.total_changes == before
    conn.execute('DELETE FROM reports WHERE id=11')
    assert reading.pending_reviews(conn) == ([], False)  # no guessed parent


def test_identical_requests_deduplicate_before_limit_and_completion(conn):
    # A repeated delivery of the actual captured request, not an invented report.
    conn.execute("INSERT INTO runs(id,kind,code,status,created_at,request)"
                 " SELECT 1000,kind,code,'queued',created_at,request FROM runs WHERE id=18")
    conn.execute("INSERT INTO runs(id,kind,code,status,created_at,request)"
                 " SELECT 1001,kind,code,'queued',created_at,request FROM runs WHERE id=18")
    rows, more = reading.pending_reviews(conn, limit=1)
    assert [r['id'] for r in rows] == [1001] and not more
    # Request replay must not supersede the live item.
    conn.execute('UPDATE runs SET identity=? WHERE id=1001', (json.dumps({'replay_of': 18}),))
    assert reading.pending_reviews(conn, limit=1)[0][0]['id'] == 1000
    conn.execute('DELETE FROM runs WHERE id IN (1000,1001)')
    assert reading.pending_reviews(conn) == ([], False)


def test_failed_review_is_not_closed_and_unknown_parent_is_not_guessed(conn):
    conn.execute("UPDATE runs SET status='failed' WHERE id=18")
    conn.execute('UPDATE reports SET passed=0 WHERE id=12')
    assert reading.pending_reviews(conn)[0][0]['label'] == '核查未通过'
    conn.execute("UPDATE runs SET request=json_remove(request, '$.parent_report_id') WHERE id=18")
    assert reading.pending_reviews(conn) == ([], False)


def test_evidence_dates_keep_all_recorded_disclosures_and_missing_distinct():
    body = copy.deepcopy(BODY[39])
    info = reading.evidence_context(body, '2026-06-30')
    expected = sorted({body['document']['announced']} | {s['document']['announced'] for s in body['sources'].values()})
    assert info['announced'] == expected and len(expected) > 1
    assert info['coverage'] == '2026-06-30' and info['gaps'] == body['gaps']
    body['document'].pop('announced')
    info = reading.evidence_context(body, None)
    assert info['missing_dates'] == 1 and info['coverage'] is None
    assert reading.evidence_context({}, None) == {'coverage': None, 'announced': [], 'missing_dates': 1, 'gaps': []}
    body['gaps'] += body['gaps']  # repeated source text is shown once, order preserved
    assert reading.evidence_context(body, None)['gaps'] == BODY[39]['gaps']


def test_home_and_legacy_report_distinguish_unknown_from_no_saved_request(conn):
    html = render('home.html', pending=[], pending_more=False, companies=[], batch_only=0, active=[], batch=None)
    assert '没有符合上述范围的待核查记录' in html
    assert '不能确认这两类事项' in html
    assert '主动研究过的公司 · 0 家' in html and '查已有研究' in html
    record = next(r['report'] for r in RECORDS if r['report']['id'] == 11)
    html = render('report.html', report=record, body=record['body'], feedback=None, feedback_label='', replay_of=None,
                  rows={r['id']: r for r in record['body']['table']}, price=None, followups=[],
                  comparison={}, market=[], fin=[])
    assert '公告可得日期未记录' in html and '基础披露抓取于：未记录' in html
    assert '本报告未记录经营序列' in html
    assert '不代表资料充分或没有风险' in html


def test_failed_report_hides_judgment_but_keeps_evidence_gaps():
    record = copy.deepcopy(next(r['report'] for r in RECORDS if r['report']['id'] == 39))
    record['passed'] = 0
    body = record['body']
    body['facts'] = {}  # failure boundary; no approved claim is invented
    html = render('report.html', report=record, body=body, feedback=None, feedback_label='', replay_of=None,
                  rows={}, price=None, followups=[], comparison={}, market=[], fin=[])
    assert body['summary'] not in html and '不展示结论' in html
    assert body['gaps'][0] in html
