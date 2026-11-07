"""R2 query identities on captured reports, disclosure and closing-price evidence."""

import pytest

from a_share_research import db, research, scans, screening
from support.continuation import load
from support.valuation import BODY, RECORD


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'queries.db')
    load(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize('query', [
    lambda conn, code: research._research_context(conn, code, 37),
    lambda conn, code: research._prior_report(conn, code, 999),
    lambda conn, code: screening.reusable_report(conn, code, '2026-10-20'),
])
def test_research_queries_normalize_both_code_forms(conn, query):
    bare = query(conn, '603606')
    assert bare
    assert query(conn, '603606.SH') == bare


@pytest.fixture
def scan_event(conn):
    # A scan linkage variant of the captured official disclosure, not a new financial fact.
    document = BODY['document']
    scan = scans.submit(conn)
    conn.execute(
        'INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action) '
        "VALUES (?,?,?,?,?,?,'periodic',2,?,'deferred')",
        (scan, RECORD['code'][:6], document['sha256'], document['title'], document['announced'],
         document['url'], document['retrieved_at']))
    return scan


def test_scan_events_normalize_both_code_forms(conn, scan_event):
    bare = scans.events(conn, code='603606')
    assert len(bare) == 1
    assert scans.events(conn, code='603606.SH') == bare


def test_latest_close_normalizes_both_code_forms(conn):
    day, close = next(iter(next(row for row in BODY['table'] if row['id'] == 'M.close')['values'].items()))
    conn.execute('INSERT INTO subscriptions(code,added_at,last_close,last_close_date) VALUES (?,?,?,?)',
                 (RECORD['code'], RECORD['created_at'], close, day))
    bare = scans.latest_close(conn, '603606')
    assert bare['last_close'] == float(close)
    assert scans.latest_close(conn, '603606.SH') == bare


def test_dispatch_normalizes_both_scope_forms(conn, scan_event, tmp_path):
    # Date-only variant: move the stored judgments before the captured announcement.
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
    # Attach the captured completed report to the captured disclosure for this read contract.
    run = conn.execute('SELECT run_id FROM reports WHERE id=39').fetchone()[0]
    conn.execute('UPDATE scan_events SET run_id=? WHERE scan_id=?', (run, scan_event))
    conn.execute("UPDATE runs SET finished_at='2026-10-30T18:00:00+08:00' WHERE id=?", (run,))
    (review,) = scans.completed_reviews(conn, None, '2026-10-31T18:00:00+08:00')
    assert {'id', 'code', 'status', 'error', 'finished_at', 'changed'} <= review.keys()
    assert not review.keys() & {'company_key', 'batch_id', 'request', 'identity', 'material'}
    assert review['id'] == run
