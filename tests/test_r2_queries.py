"""R2 query contracts on captured reports; request/status variants stay in this database."""

import json

import pytest

from a_share_research import db, reading, scans, service, validation, worker
from a_share_research.limits import MAX_CALLS
from support.continuation import load
from support.valuation import BODY, RECORD


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'r2.db')
    load(connection)
    yield connection
    connection.close()


def test_company_card_keeps_six_digit_boundary(conn):
    assert service.company_card(conn, '603606')['report_id'] == 39
    with pytest.raises(service.RequestError, match='公司代码必须为六位数字'):
        service.company_card(conn, '603606.SH')


@pytest.mark.parametrize('status', ['failed', 'running'])
def test_formal_queries_require_done_but_frozen_validation_does_not(conn, tmp_path, status):
    # Change only the status of the captured, passed research; V0's samples must survive.
    # The capture loader encodes absent requests as JSON null; V0 expects SQL NULL.
    conn.execute("UPDATE runs SET request=NULL WHERE request='null'")
    before = validation.load_reports(str(tmp_path / 'r2.db'))
    assert before
    conn.execute("UPDATE runs SET status=? WHERE id=(SELECT run_id FROM reports WHERE id=39)", (status,))
    assert validation.load_reports(str(tmp_path / 'r2.db')) == before
    assert service.company_card(conn, '603606')['report_id'] == 37
    assert reading.latest_reports(conn, codes=['603606'])[0]['id'] == 39


@pytest.mark.parametrize('request_id,member', [
    ('batch-1-603606', True), ('batch-1-603606.SH', True),
    ('batch-abc', False), ('batch-1oops-603606.SH', False),
    ('batch-01-603606', False), ('batch-1-600519.SH', False),
    ('batch-1-603606.SS', False), ('batch-1-603606.SH-extra', False),
    ('batch-1-', False), ('batchB-refresh-603606', False),
])
def test_batch_membership_drives_queue_and_retry(conn, request_id, member):
    batch = service.create_batch(conn, 'monthly:2026-11')
    assert batch == 1
    # All company/report evidence is captured; only request identity and queue state vary.
    candidate = db.create_run(conn, '603606.SH', request_id)
    ordinary = db.create_run(conn, '603606.SH', 'r2-ordinary')
    assert service.is_monthly_research(conn, candidate) is member
    assert worker.next_run(conn)['id'] == (ordinary if member else candidate)
    assert reading.queue_ahead(conn, candidate)[0] == int(member)
    assert bool(service.retry_view(reading.run(conn, candidate))['note']) is member
    db.update_run(conn, candidate, status='running')
    worker.recover(conn)
    assert reading.run(conn, candidate)['status'] == ('failed' if member else 'queued')


def test_orphan_batch_stays_out_of_monthly_budget(conn):
    candidate = db.create_run(conn, '603606.SH', 'batch-999-603606.SH', status='running')
    assert not service.is_monthly_research(conn, candidate)
    assert service.research_call_limit(conn, candidate) == MAX_CALLS
    worker.recover(conn)
    assert reading.run(conn, candidate)['status'] == 'queued'


def test_queue_keeps_null_request_id_priority(conn):
    db.create_run(conn, '603606.SH', 'r2-web')
    cli = db.create_run(conn, '603606.SH')
    assert worker.next_run(conn)['id'] == cli


def test_report_metadata_does_not_expose_generated_columns(conn):
    rows = reading.api_reports(conn, '603606.SH')
    assert rows
    assert all(set(row.keys()) == {'id', 'run_id', 'code', 'name', 'created_at', 'period', 'passed', 'status'}
               for row in rows)


def test_body_only_parent_fallback_is_preserved(conn):
    # Explicit missing-request variant: body links must still work without rewriting history.
    record = conn.execute('SELECT run_id, body FROM reports WHERE id=12').fetchone()
    body = json.loads(record['body'])
    assert body['parent_report_id'] == 11
    conn.execute("UPDATE runs SET request=json_remove(request, '$.parent_report_id') WHERE id=?",
                 (record['run_id'],))
    assert record['run_id'] in {
        row['id'] for row in reading.followups(conn, '600519.SH', 11)}
    assert next(row for row in reading.stock_reports(conn, '600519') if row['id'] == 12)['parent_report_id'] == 11


@pytest.fixture
def scan_event(conn):
    document = BODY['document']
    scan = scans.submit(conn)
    conn.execute(
        'INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action) '
        "VALUES (?,?,?,?,?,?,'periodic',2,?,'deferred')",
        (scan, RECORD['code'][:6], document['sha256'], document['title'], document['announced'],
         document['url'], document['retrieved_at']))
    return scan


def test_dispatch_normalizes_both_scope_forms(conn, scan_event, tmp_path):
    conn.execute("UPDATE reports SET created_at='2026-01-01T00:00:00+08:00'")
    outputs = []
    for code in ('603606', '603606.SH'):
        variant = db.connect(tmp_path / f'{code}.db')
        conn.backup(variant)
        try:
            outputs.append(scans._dispatch(variant, scan_event, '2026-10-30T18:00:00+08:00', {code}))
        finally:
            variant.close()
    assert outputs[0]
    assert outputs[0] == outputs[1]


def test_completed_reviews_carry_what_notifications_read_and_no_payload(conn, scan_event):
    run = conn.execute('SELECT run_id FROM reports WHERE id=39').fetchone()[0]
    conn.execute('UPDATE scan_events SET run_id=? WHERE scan_id=?', (run, scan_event))
    conn.execute("UPDATE runs SET finished_at='2026-10-30T18:00:00+08:00' WHERE id=?", (run,))
    (review,) = scans.completed_reviews(conn, None, '2026-10-31T18:00:00+08:00')
    assert {'id', 'code', 'status', 'error', 'finished_at', 'changed'} <= review.keys()
    assert not review.keys() & {'company_key', 'batch_id', 'request', 'identity', 'material'}
    assert review['id'] == run
