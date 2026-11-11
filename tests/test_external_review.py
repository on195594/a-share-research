"""External lifecycle tests use real material with deterministic reviewer protocol decisions."""
import json
from datetime import date

import pytest

from a_share_research import api_keys, db, external_research as external, external_review, research, worker
from a_share_research.reviewing import digest
from support.reports import MATERIAL, report


@pytest.fixture
def queued(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    token = api_keys.create(conn, 'external')
    principal = api_keys.authenticate(conn, token)
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    item = external.start(conn, principal, 'draft', source)
    receipt = external.submit(conn, principal, item['id'], 0, report())
    yield conn, principal, receipt['submission_id'], item['id']
    conn.close()


def clean_review(runner, material, draft, ledger):
    return ledger.accept(dict(version=1, draft_sha256=digest(draft), material_sha256=material.snapshot()['sha256'],
                              issues=[], resolutions=[], risk_resolutions=[]),
                         draft, material.source_registry(), material.table, material.snapshot()['sha256'])


def test_opt_in_idempotency_priority_and_monthly_protection(queued, monkeypatch):
    conn, principal, sid, _ = queued
    with pytest.raises(external.SubmissionError, match='启用'):
        external_review.enqueue(conn, principal, sid)
    assert conn.execute("SELECT count(*) FROM runs WHERE kind='external'").fetchone()[0] == 0
    task = external_review.enqueue(conn, principal, sid, allow=True)
    assert external_review.enqueue(conn, principal, sid, allow=True) == task
    assert worker.next_run(conn) is None
    batch = db.create_run(conn, MATERIAL.code, request_id='batch-999-600519')
    owner = db.create_run(conn, MATERIAL.code, request_id='owner')
    assert worker.next_run(conn, external_enabled=True)['id'] == owner
    db.update_run(conn, owner, status='done')
    assert worker.next_run(conn, external_enabled=True)['id'] == task['run_id']
    monkeypatch.setattr(external_review, 'protected', lambda: True)
    assert worker.next_run(conn, external_enabled=True)['id'] == batch


@pytest.mark.parametrize('day,result', [('2026-11-28',False),('2026-11-29',True),('2026-12-04',True),('2026-12-05',False)])
def test_protection_dates(day, result):
    assert external_review.protected(date.fromisoformat(day)) == result


def test_reviews_without_rewriting_and_publishes_once(queued, monkeypatch):
    conn, principal, sid, eid = queued
    task = external_review.enqueue(conn, principal, sid, allow=True)
    def forbidden(*args, **kwargs):
        pytest.fail('external review must not gather, compose, or revise')
    for name in ('gather', 'compose', 'revise'):
        monkeypatch.setattr(research, name, forbidden)
    monkeypatch.setattr(research, 'review', clean_review)
    worker.execute_run(conn, {'id': task['run_id'], 'kind': 'external'})
    final = external_review.view(conn, sid)
    assert final['status'] == 'done' and final['report_id']
    body = json.loads(conn.execute('SELECT body FROM reports WHERE id=?', (final['report_id'],)).fetchone()[0])
    assert body['summary'] == report()['summary']
    assert body['producer']['key_id'] == principal.key_id
    assert external.status(conn, principal, eid)['status'] == 'done'
    assert external.status(conn, principal, eid)['receipts'][0]['review_status'] == 'done'
    external_review.execute(conn, task['run_id'])
    assert conn.execute('SELECT count(*) FROM reports').fetchone()[0] == 1
    assert conn.execute(f'SELECT count(*) FROM reports r JOIN runs u ON u.id=r.run_id WHERE {db.FORMAL_REPORT}').fetchone()[0] == 0


class PowerLoss(BaseException):
    pass


def test_restart_resumes_frozen_draft_not_research(queued, monkeypatch):
    conn, principal, sid, _ = queued
    task = external_review.enqueue(conn, principal, sid, allow=True)
    monkeypatch.setattr(research, 'review', lambda *args: (_ for _ in ()).throw(PowerLoss()))
    with pytest.raises(PowerLoss):
        external_review.execute(conn, task['run_id'])
    assert external_review.view(conn, sid)['status'] == 'reviewing'
    worker.recover(conn)
    assert external_review.view(conn, sid)['status'] == 'reviewing'
    external_review.recover(conn)
    assert external_review.view(conn, sid)['status'] == 'submitted'
    monkeypatch.setattr(research, 'review', clean_review)
    external_review.execute(conn, task['run_id'])
    assert external_review.view(conn, sid)['status'] == 'done'
    assert external_review.view(conn, sid)['attempt'] == 2


def test_error_is_terminal_and_failed_publication_rolls_back(queued, monkeypatch):
    conn, principal, sid, _ = queued
    task = external_review.enqueue(conn, principal, sid, allow=True)
    monkeypatch.setattr(research, 'review', clean_review)
    conn.execute("CREATE TRIGGER fail_report BEFORE INSERT ON reports BEGIN SELECT RAISE(ABORT,'test failure'); END")
    external_review.execute(conn, task['run_id'])
    assert external_review.view(conn, sid)['status'] == 'failed'
    assert conn.execute('SELECT count(*) FROM reports').fetchone()[0] == 0
    external_review.recover(conn)
    assert external_review.view(conn, sid)['status'] == 'failed'


def test_release_fingerprints_cover_new_state_and_legacy_databases(queued, tmp_path):
    from a_share_research import deploy
    conn, principal, sid, _ = queued
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    from pathlib import Path
    before = deploy.fingerprints(Path(database))
    assert all(table in before for table in ('external_research', 'external_submissions', 'external_reviews', 'holdings'))
    external_review.enqueue(conn, principal, sid, allow=True)
    after = deploy.fingerprints(Path(database))
    assert before['external_reviews'] != after['external_reviews']
    with pytest.raises(RuntimeError, match='尚未空闲'):
        deploy.idle(Path(database))
    legacy = db.connect(tmp_path / 'legacy.db')
    for table in reversed(deploy.OPTIONAL_TABLES):
        legacy.execute(f'DROP TABLE IF EXISTS {table}')
    legacy.close()
    assert set(deploy.fingerprints(tmp_path / 'legacy.db')) == set(deploy.TABLES)


def test_substantive_failure_is_visible_without_unreviewed_summary(queued, monkeypatch):
    from a_share_research import service
    conn, principal, sid, _ = queued
    task = external_review.enqueue(conn, principal, sid, allow=True)
    def flag(runner, material, draft, ledger):
        return ledger.accept(dict(version=1, draft_sha256=digest(draft), material_sha256=material.snapshot()['sha256'],
                             issues=[dict(type='claim',where='summary',problem='测试协议实质问题',
                                          importance='影响结论',evidence=[{'ref':'T.op_cash'}])],
                             resolutions=[],risk_resolutions=[]),
                             draft, material.source_registry(), material.table, material.snapshot()['sha256'])
    monkeypatch.setattr(research, 'review', flag)
    external_review.execute(conn, task['run_id'])
    outcome = external_review.view(conn, sid)
    assert outcome['status'] == 'failed' and outcome['error']
    view = service.report_view(conn, outcome['report_id'], compact=True)
    assert 'summary' not in view['report']
    assert view['report']['producer']['key_id'] == principal.key_id
    assert view['purpose'] == 'external'
