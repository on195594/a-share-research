"""Isolated workflow metadata and captured run states; no providers or paid model calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from a_share_research import batches, db, research, service, worker


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


@pytest.mark.parametrize('status', ['queued', 'researching', 'frozen', 'tracking', 'closed', 'failed'])
def test_existing_manual_batch_suppresses_automatic(tmp_path, status):
    conn = db.connect(tmp_path / 'monthly.db')
    ident = service.create_batch(conn, 'manual')
    conn.execute('UPDATE batches SET status=? WHERE id=?', (status, ident))
    month = datetime.now(db.SHANGHAI).strftime('%Y-%m')
    result = service.monthly_batch(conn, month, submit=True)
    assert not result['created'] and result['batch']['id'] == ident
    if status == 'closed':
        with pytest.raises(service.RequestError):
            service.create_batch(conn, 'duplicate-closed-month')
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
    db.update_batch(conn, batch, q20=json.dumps([{'code': MATERIAL.code, 'run_id': run}]))
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


@pytest.mark.parametrize('pool', ['q20', 's10'])
def test_monthly_status_uses_candidate_run_links_for_captured_failures(tmp_path, pool):
    captured = json.loads((Path(__file__).parent / 'data/monthly-research-status.json').read_text())
    conn = db.connect(tmp_path / 'captured.db')
    for run in captured['runs']:
        conn.execute('INSERT INTO runs(id,request_id,code,status,error,created_at) VALUES (?,?,?,?,?,?)',
                     tuple(run[k] for k in ('id', 'request_id', 'code', 'status', 'error', 'created_at')))
    for batch in captured['batches']:
        # Moving observed links between pool fields exercises the projection only,
        # not a claim that these production large-cap candidates qualified as small caps.
        conn.execute('INSERT INTO batches(id,status,created_at,rule,q20,s10) VALUES (?,?,?,?,?,?)',
                     (batch['id'], batch['status'], batch['created_at'], json.dumps(batch['rule']),
                      json.dumps(batch['q20'] if pool == 'q20' else []),
                      json.dumps(batch['q20'] if pool == 's10' else [])))
    before = conn.total_changes
    for batch in captured['batches']:
        linked = {r['run_id'] for r in batch['q20']}
        expected = [{'code': r['code'], 'error': r['error']} for r in captured['runs']
                    if r['id'] in linked and r['status'] == 'failed']
        result = service.monthly_batch(conn, batch['created_at'][:7])
        assert result['batch']['failed_research'] == expected
        assert not result['created'] and len(expected) == 1
    assert conn.total_changes == before
    # The union covers simultaneous large/small failures and de-duplicates a shared run link.
    first, second = captured['batches']
    db.update_batch(conn, first['id'], q20=json.dumps(first['q20']),
                    s10=json.dumps(first['q20'] + second['q20']))
    expected = [{'code': r['code'], 'error': r['error']} for r in captured['runs'] if r['status'] == 'failed']
    assert service.monthly_batch(conn, first['created_at'][:7])['batch']['failed_research'] == expected
    # A queued batch with no linked candidates has no research failures.
    ident = service.create_batch(conn, 'unlinked', created_at='2026-12-01T20:00:00+08:00')
    result = service.monthly_batch(conn, '2026-12')
    assert result['batch']['id'] == ident and result['batch']['failed_research'] == []
    conn.close()


@pytest.mark.parametrize('request_id, expected', [('monthly:2026-12', 21), ('manual-december', 30)])
def test_batch_run_limit_is_enforced_and_reported(tmp_path, monkeypatch, request_id, expected):
    conn = db.connect(tmp_path / 'budget.db')
    batch_id = service.create_batch(conn, request_id)
    run_id = db.create_run(conn, '600519', request_id=f'batch-{batch_id}-600519', status='running')
    invocations = []

    def complete(*args, **kwargs):
        invocations.append(1)
        return SimpleNamespace(text='{}', message={}, usage=None, seconds=0, identity=None)

    monkeypatch.setattr(research.model, 'complete', complete)
    runner = research.Runner(conn, run_id, max_calls=99)
    assert runner.max_calls == expected
    assert research.Runner(conn, run_id, max_calls=5).max_calls == 5
    for _ in range(expected):
        runner.ask('review', 'offline fixture')
    with pytest.raises(research.ResearchError, match='模型调用次数超过上限'):
        runner.ask('review', 'must not reach provider')
    budget = service.run_budget(conn, run_id)
    assert budget['model_call_limit'] == budget['model_calls_recorded'] == expected
    assert len(invocations) == expected
    worker.recover(conn)
    state = conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()[0]
    assert state == ('failed' if expected == 21 else 'queued')
    standalone = db.create_run(conn, '600519')
    assert research.Runner(conn, standalone).max_calls == 30
    assert service.run_budget(conn)['model_call_limit'] == 30
    conn.close()
