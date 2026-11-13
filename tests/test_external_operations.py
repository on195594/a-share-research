"""Durable acquisition boundaries using captured Moutai evidence.

Collector results are deterministic protocol substitutes; these tests make no
claim of live network acquisition or independent model review.
"""
from copy import deepcopy
import json
import sqlite3

import pytest

from a_share_research import api_keys, db, external_operations as operations
from a_share_research import external_research as external, external_review, gate, research
from a_share_research import acquisition_process
from a_share_research.material import Material
from a_share_research.research_tools import ResearchTools
from a_share_research.reviewing import digest
from support.reports import MATERIAL, report


CALCULATE = dict(label='2025较2024营业总收入差额', operation='difference',
                 left={'ref': 'T.revenue', 'period': '2025-12-31'},
                 right={'ref': 'T.revenue', 'period': '2024-12-31'})


def collector(request, *, timeout):
    assert timeout > 0
    if request['action'] == 'open':
        assert request['code'] == '600519'
        return {'material': deepcopy(MATERIAL).snapshot(), 'result': {'collected': True}}
    material = Material.from_snapshot(request['material'])
    result = ResearchTools(material, None).run(request['tool'], request['arguments'])
    return {'material': material.snapshot(), 'result': result}


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    keys = {role: api_keys.authenticate(conn, api_keys.create(conn, role, role=role))
            for role in ('research', 'assistant', 'owner')}
    keys['other'] = api_keys.authenticate(conn, api_keys.create(conn, 'other'))
    monkeypatch.setattr(acquisition_process, 'run', collector)
    yield conn, keys, path
    conn.close()


def opened(conn, principal, request_id='first'):
    operation = operations.open_research(conn, principal, request_id, '600519')
    operations.execute(conn, operation['id'])
    result = operations.operation(conn, principal, operation['id'])
    assert result['status'] == 'done'
    return result['external_id']


def acquire(conn, principal, external_id, request_id='calculate', revision=0):
    return operations.acquire(conn, principal, request_id, external_id, revision, 'calculate', deepcopy(CALCULATE))


def source_run(conn):
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    return source


def test_unseen_company_open_has_no_fake_run_and_survives_reconnect(state):
    conn, keys, path = state
    principal = keys['research']
    pending = operations.open_research(conn, principal, 'first', '600519')
    assert pending['status'] == 'queued' and pending['external_id'] is None
    assert pending == operations.open_research(conn, principal, 'first', '600519')
    assert conn.execute('SELECT count(*) FROM external_research').fetchone()[0] == 0
    operations.execute(conn, pending['id'])
    final = operations.operation(conn, principal, pending['id'])
    assert final['status'] == 'done' and final['material_revision'] == 0
    assert final['started_at'] and final['deadline'] and final['finished_at']
    item = external.status(conn, principal, final['external_id'])
    assert item['source_run_id'] is None and item['material_revision'] == 0
    assert item['material_sha256'] == MATERIAL.snapshot()['sha256']
    assert item['status'] == 'collecting'
    assert final == operations.open_research(conn, principal, 'first', '600519')
    for table in ('runs', 'reports', 'calls'):
        assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    second = db.connect(path)
    try:
        assert operations.operation(second, principal, pending['id']) == final
        operations.execute(second, pending['id'])
        assert operations.operation(second, principal, pending['id']) == final
        assert not second.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        second.close()


def test_request_ids_are_shared_by_all_actions(state):
    conn, keys, _ = state
    principal = keys['research']
    source = source_run(conn)
    old = external.start(conn, principal, 'reuse', source)
    with pytest.raises(external.SubmissionError, match='编号'):
        operations.open_research(conn, principal, 'reuse', '600519')
    with pytest.raises(external.SubmissionError, match='编号'):
        acquire(conn, principal, old['id'], 'reuse')
    pending = operations.open_research(conn, principal, 'open', '600519')
    with pytest.raises(external.SubmissionError, match='编号'):
        external.start(conn, principal, 'open', source)
    with pytest.raises(external.SubmissionError, match='编号'):
        operations.open_research(conn, principal, 'open', '600938')
    with pytest.raises(external.SubmissionError, match='编号'):
        acquire(conn, principal, old['id'], 'open')
    operation = acquire(conn, principal, old['id'], 'acquire')
    assert operation == acquire(conn, principal, old['id'], 'acquire')
    with pytest.raises(external.SubmissionError, match='编号'):
        operations.open_research(conn, principal, 'acquire', '600519')
    with pytest.raises(external.SubmissionError, match='编号'):
        external.start(conn, principal, 'acquire', source)
    assert operations.operation(conn, principal, pending['id'])['status'] == 'queued'


def test_owner_is_denied_two_keys_are_isolated_account_awareness_is_fixed(state):
    conn, keys, _ = state
    with pytest.raises(external.SubmissionError) as error:
        operations.open_research(conn, keys['owner'], 'first', '600519')
    assert error.value.status == 403
    for role in ('research', 'assistant'):
        eid = opened(conn, keys[role])
        item = external.status(conn, keys[role], eid)
        assert item['account_aware'] == (role == 'assistant')
        operation = acquire(conn, keys[role], eid)
        assert operation['account_aware'] == (role == 'assistant')
        for key in (keys['other'], keys['owner']):
            with pytest.raises(external.SubmissionError):
                operations.operation(conn, key, operation['id'])
            with pytest.raises(external.SubmissionError):
                acquire(conn, key, eid, 'cross-owner')
        operations.execute(conn, operation['id'])
        assert operations.operation(conn, keys[role], operation['id'])['account_aware'] == (role == 'assistant')


@pytest.mark.parametrize('revoke_during', [False, True])
def test_revocation_before_or_after_acquisition_cannot_publish(state, monkeypatch, revoke_during):
    conn, keys, _ = state
    principal = keys['research']
    pending = operations.open_research(conn, principal, 'revoked', '600519')
    calls = []
    def work(request, *, timeout):
        calls.append(request)
        assert not conn.in_transaction
        conn.execute('DELETE FROM api_keys WHERE id=?', (principal.key_id,))
        return collector(request, timeout=timeout)
    monkeypatch.setattr(acquisition_process, 'run', work)
    if not revoke_during:
        conn.execute('DELETE FROM api_keys WHERE id=?', (principal.key_id,))
    operations.execute(conn, pending['id'])
    row = conn.execute('SELECT * FROM external_operations WHERE id=?', (pending['id'],)).fetchone()
    assert row['status'] == 'failed' and row['error'] == 'revoked'
    assert bool(calls) == revoke_during
    assert conn.execute('SELECT count(*) FROM external_research').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM external_materials').fetchone()[0] == 0
    with pytest.raises(external.SubmissionError) as error:
        operations.operation(conn, principal, pending['id'])
    assert error.value.status == 403


def test_serial_acquisition_and_immutable_submission_binding(state):
    conn, keys, path = state
    principal = keys['research']
    eid = opened(conn, principal)
    first = external.submit(conn, principal, eid, 0, report(), material_revision=0)
    assert first['accepted']
    op = acquire(conn, principal, eid)
    second_conn = db.connect(path)
    try:
        with pytest.raises(external.SubmissionError, match='冲突|未完成'):
            acquire(second_conn, principal, eid, 'concurrent')
        with pytest.raises(external.SubmissionError, match='未完成'):
            external.submit(second_conn, principal, eid, 1, report(gaps=['补充披露']), material_revision=0)
        assert external.submit(second_conn, principal, eid, 0, report(), material_revision=0) == first
        operations.execute(conn, op['id'])
        assert acquire(second_conn, principal, eid) == operations.operation(conn, principal, op['id'])
        with pytest.raises(external.SubmissionError, match='冲突'):
            acquire(second_conn, principal, eid, 'stale')
        current = external.status(second_conn, principal, eid)
        assert current['status'] == 'collecting' and current['material_revision'] == 1
        latest = external.material_snapshot(second_conn, principal, eid)
        old = external.material_snapshot(second_conn, principal, eid, material_revision=0)
        assert old['sha256'] == first['material_sha256'] != latest['sha256']
        assert latest['data']['table'][:-1] == old['data']['table']
        assert latest['data']['table'][-1]['values']['result'] == '-20.90'
        new = external.submit(second_conn, principal, eid, 1, report(), material_revision=1)
        assert new['accepted'] and new['version'] == 2 and new['material_revision'] == 1
        assert new['submission_id'] != first['submission_id']
        assert external.submit(second_conn, principal, eid, 0, report(), material_revision=0) == first
        assert external.submit(second_conn, principal, eid, 0, report(), material_revision=1) == new
        assert len(external.status(conn, principal, eid)['receipts']) == 2
    finally:
        second_conn.close()


def test_old_review_uses_frozen_material_even_after_next_acquisition(state, monkeypatch):
    conn, keys, _ = state
    principal = keys['research']
    eid = opened(conn, principal)
    receipt = external.submit(conn, principal, eid, 0, report(), material_revision=0)
    op = acquire(conn, principal, eid)
    operations.execute(conn, op['id'])
    queued = external_review.enqueue(conn, principal, receipt['submission_id'], allow=True)
    run_material = json.loads(conn.execute('SELECT material FROM runs WHERE id=?', (queued['run_id'],)).fetchone()[0])
    assert run_material['sha256'] == receipt['material_sha256']
    observed = []
    def clean_review(runner, material, draft, ledger):
        observed.append(material.snapshot()['sha256'])
        return ledger.accept(dict(version=1, draft_sha256=digest(draft), material_sha256=material.snapshot()['sha256'],
                                  issues=[], resolutions=[], risk_resolutions=[]),
                             draft, material.source_registry(), material.table, material.snapshot()['sha256'])
    monkeypatch.setattr(research, 'review', clean_review)
    external_review.execute(conn, queued['run_id'])
    assert observed == [receipt['material_sha256']]
    assert external_review.view(conn, receipt['submission_id'])['status'] == 'done'
    assert external.status(conn, principal, eid)['status'] == 'collecting'
    assert external.material_snapshot(conn, principal, eid)['sha256'] != observed[0]


def test_interruption_is_terminal_after_recovery_and_requires_new_request(state, monkeypatch):
    conn, keys, path = state
    principal = keys['research']
    pending = operations.open_research(conn, principal, 'interrupted', '600519')
    class PowerLoss(BaseException):
        pass
    def interrupt(request, *, timeout):
        raise PowerLoss()
    monkeypatch.setattr(acquisition_process, 'run', interrupt)
    with pytest.raises(PowerLoss):
        operations.execute(conn, pending['id'])
    running = operations.operation(conn, principal, pending['id'])
    assert running['status'] == 'running' and running['deadline']
    recovered = db.connect(path)
    try:
        operations.recover(recovered)
        failed = operations.operation(recovered, principal, pending['id'])
        assert failed['status'] == 'failed' and failed['error'] == 'interrupted'
        assert failed['started_at'] == running['started_at'] and failed['deadline'] == running['deadline']
        operations.execute(recovered, pending['id'])
        assert operations.open_research(recovered, principal, 'interrupted', '600519') == failed
        monkeypatch.setattr(acquisition_process, 'run', collector)
        assert opened(recovered, principal, 'explicit-new-attempt')
    finally:
        recovered.close()


@pytest.mark.parametrize('error_code', ['timeout', 'invalid_result'])
def test_failure_is_durable_and_does_not_commit_partial_material(state, monkeypatch, error_code):
    conn, keys, _ = state
    principal = keys['research']
    eid = opened(conn, principal)
    pending = acquire(conn, principal, eid)
    def fail(request, *, timeout):
        raise acquisition_process.AcquisitionProcessError(error_code)
    monkeypatch.setattr(acquisition_process, 'run', fail)
    operations.execute(conn, pending['id'])
    failed = operations.operation(conn, principal, pending['id'])
    assert failed['status'] == 'failed' and failed['error'] == error_code
    assert acquire(conn, principal, eid) == failed
    assert external.status(conn, principal, eid)['material_revision'] == 0
    assert conn.execute('SELECT count(*) FROM external_materials WHERE external_id=?', (eid,)).fetchone()[0] == 1
    assert acquire(conn, principal, eid, 'explicit-retry')['status'] == 'queued'


def test_commit_failure_rolls_back_initialized_research(state):
    conn, keys, _ = state
    pending = operations.open_research(conn, keys['research'], 'commit-fail', '600519')
    conn.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON external_materials BEGIN SELECT RAISE(ABORT,'test failure'); END")
    operations.execute(conn, pending['id'])
    failed = operations.operation(conn, keys['research'], pending['id'])
    assert failed['status'] == 'failed' and failed['error'] == 'acquisition_failed'
    assert conn.execute('SELECT count(*) FROM external_research').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM external_materials').fetchone()[0] == 0


def test_terminal_operations_and_revisions_are_immutable(state):
    conn, keys, _ = state
    eid = opened(conn, keys['research'])
    op = acquire(conn, keys['research'], eid)
    operations.execute(conn, op['id'])
    for sql in (
        'UPDATE external_operations SET status="queued" WHERE id=?',
        'DELETE FROM external_operations WHERE id=?',
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (op['id'],))
    for sql in (
        'UPDATE external_materials SET material="{}" WHERE external_id=?',
        'DELETE FROM external_materials WHERE external_id=?',
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (eid,))
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_v21_migration_preserves_existing_identities_and_review_foreign_keys(tmp_path, monkeypatch):
    path = tmp_path / 'v21.db'
    migrations = db.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', migrations[:21])
        conn = db.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 21
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'old-key'))
    source = source_run(conn)
    material = json.dumps(MATERIAL.snapshot(), ensure_ascii=False)
    snapshot_hash = MATERIAL.snapshot()['sha256']
    draft = report()
    problems = gate.check_draft(draft, MATERIAL.snapshot())
    assert not problems
    conn.execute("INSERT INTO external_research(id,owner_key_id,request_id,company_key,source_run_id,account_aware,"
                 "status,version,created_at,material_sha256,material) VALUES (31,?,'legacy','600519',?,0,'submitted',1,?,?,?)",
                 (principal.key_id, source, db.now(), snapshot_hash, material))
    conn.execute('INSERT INTO external_submissions(id,external_id,version,created_at,accepted,draft_sha256,'
                 'material_sha256,draft,problems) VALUES (41,31,1,?,1,?,?,?,?)',
                 (db.now(), gate.digest(draft), snapshot_hash, gate.canonical(draft), gate.canonical(problems)))
    review_run = db.create_run(conn, MATERIAL.code, kind='external')
    db.save_material(conn, review_run, MATERIAL.snapshot())
    conn.execute("INSERT INTO external_reviews(submission_id,run_id,status,created_at,updated_at) VALUES (41,?,'submitted',?,?)",
                 (review_run, db.now(), db.now()))
    tables = ('external_research', 'external_submissions', 'external_reviews')
    originals = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in tables}
    conn.close()
    migrated = db.connect(path)
    try:
        assert migrated.execute('PRAGMA user_version').fetchone()[0] == 22
        for table in tables:
            current = [dict(row) for row in migrated.execute(f'SELECT * FROM {table} ORDER BY 1')]
            assert [{key: row[key] for key in originals[table][0]} for row in current] == originals[table]
        revision = operations.revision(migrated, 31, 0)
        assert revision['material'] == material and revision['material_sha256'] == snapshot_hash
        assert revision['operation_id'] is None and revision['parent_revision'] is None
        assert not migrated.execute('PRAGMA foreign_key_check').fetchall()
        assert external.submit(migrated, principal, 31, 0, draft, material_revision=0)['submission_id'] == 41
        op = acquire(migrated, principal, 31)
        operations.execute(migrated, op['id'])
        new = external.submit(migrated, principal, 31, 1, draft, material_revision=1)
        assert new['submission_id'] > 41 and new['material_revision'] == 1
        assert external_review.enqueue(migrated, principal, 41, allow=True)['run_id'] == review_run
    finally:
        migrated.close()


def test_actionable_failure_detail_is_durable_without_a_material_revision(state, monkeypatch):
    from a_share_research import integrity
    conn, keys, path = state
    principal = keys['research']
    eid = opened(conn, principal)
    pending = acquire(conn, principal, eid)
    def fail(request, *, timeout):
        raise acquisition_process.AcquisitionProcessError('invalid_arguments', '可用期间：2024-12-31、2025-12-31')
    monkeypatch.setattr(acquisition_process, 'run', fail)
    operations.execute(conn, pending['id'])
    failed = operations.operation(conn, principal, pending['id'])
    assert failed['error'] == 'invalid_arguments' and failed['error_detail'].startswith('可用期间')
    assert failed['result'] is None and failed['material_revision'] is None
    assert acquire(conn, principal, eid) == failed
    assert integrity.check(path, path.parent / 'pdf')[0] == []
