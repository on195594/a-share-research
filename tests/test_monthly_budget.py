"""Offline authorization limits; never invokes a provider."""
from types import SimpleNamespace

import pytest

from a_share_research import db, research, service, worker


@pytest.mark.parametrize('request_id, expected', [('monthly:2026-12', 21), ('manual-december', 30)])
@pytest.mark.parametrize('code, suffix', [('600519', '.SH'), ('600519.SH', '.SH'), ('000001', '.SZ'), ('600519', '')])
def test_batch_run_limit_is_enforced_and_reported(tmp_path, monkeypatch, request_id, expected, code, suffix):
    conn = db.connect(tmp_path / 'budget.db')
    batch_id = service.create_batch(conn, request_id)
    run_id = db.create_run(conn, code, request_id=f'batch-{batch_id}-{code[:6]}{suffix}', status='running')
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
