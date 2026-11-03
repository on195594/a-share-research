"""Isolated workflow metadata, no companies, providers, worker or paid model calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest

from a_share_research import batches, db, service


def test_concurrent_monthly_requests_are_one_batch(tmp_path):
    path = tmp_path / 'monthly.db'
    db.connect(path).close()
    month = datetime.now(db.SHANGHAI).strftime('%Y-%m')
    barrier = Barrier(2)

    def submit():
        conn = db.connect(path)
        barrier.wait()
        try:
            return service.monthly_batch(conn, month, submit=True)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sum(r['created'] for r in results) == 1
    assert len({r['batch']['id'] for r in results}) == 1
    conn = db.connect(path)
    row = conn.execute('SELECT * FROM batches').fetchone()
    assert batches.load(row)['rule'] == batches.NEW_RULE
    conn.execute("UPDATE batches SET status='failed',error='test interrupted' WHERE id=?", (row['id'],))
    assert service.monthly_batch(conn, month, submit=True)['batch']['status'] == 'failed'
    assert conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0] == 1
    # Explicit manual retry after failure remains available, unlike automatic admission.
    assert service.create_batch(conn, 'manual-retry') != row['id']
    assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize('status', ['queued', 'researching', 'frozen', 'tracking', 'failed'])
def test_existing_manual_batch_suppresses_automatic(tmp_path, status):
    conn = db.connect(tmp_path / 'monthly.db')
    ident = service.create_batch(conn, 'manual')
    conn.execute('UPDATE batches SET status=? WHERE id=?', (status, ident))
    month = datetime.now(db.SHANGHAI).strftime('%Y-%m')
    result = service.monthly_batch(conn, month, submit=True)
    assert not result['created'] and result['batch']['id'] == ident
    with pytest.raises(service.RequestError, match='不补跑'):
        service.monthly_batch(conn, '2020-01', submit=True)
    conn.close()


def test_shanghai_month_boundary_reads_utc_stamp(tmp_path):
    path = tmp_path / 'monthly.db'
    conn = db.connect(path)
    # Time-only boundary inputs: UTC November ends while Shanghai December has begun.
    conn.execute("INSERT INTO batches(status,created_at,rule) VALUES ('failed','2026-11-30T16:00:00+00:00','{}')")
    reader = db.connect(path)
    reader.execute('PRAGMA busy_timeout = 0')
    with db.transaction(conn):
        assert service.monthly_batch(reader, '2026-12')['batch']['status'] == 'failed'
        assert service.monthly_batch(reader, '2026-11')['batch'] is None
    reader.close()
    conn.close()


def test_restart_does_not_restart_monthly_paid_work(tmp_path):
    from a_share_research import screening, worker
    from support.reports import MATERIAL

    conn = db.connect(tmp_path / 'monthly.db')
    month = datetime.now(db.SHANGHAI).strftime('%Y-%m')
    batch = service.monthly_batch(conn, month, submit=True)['batch']['id']
    run = db.create_run(conn, MATERIAL.code, request_id=f'batch-{batch}-{MATERIAL.code}', status='running')
    conn.execute("UPDATE batches SET status='selecting' WHERE id=?", (batch,))
    worker.recover(conn)
    screening.recover(conn)
    worker.recover(conn)
    screening.recover(conn)
    assert conn.execute('SELECT status FROM runs WHERE id=?', (run,)).fetchone()[0] == 'failed'
    assert service.monthly_batch(conn, month, submit=True)['batch']['status'] == 'failed'
    assert service.monthly_batch(conn, month)['batch']['failed_research'][0]['code'] == MATERIAL.code
    assert not screening.tick(conn)
    assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1
    conn.close()


def test_queued_monthly_batch_cannot_start_in_a_later_month(tmp_path):
    from a_share_research import screening

    conn = db.connect(tmp_path / 'late.db')
    ident = service.create_batch(conn, 'monthly:2020-01')
    assert screening.tick(conn)
    row = conn.execute('SELECT status,error FROM batches WHERE id=?', (ident,)).fetchone()
    assert row['status'] == 'failed' and '不使用后来数据补跑' in row['error']
    assert not screening.tick(conn)
    assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    conn.close()
