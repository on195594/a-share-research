"""External submission boundaries using captured annual-report excerpts.

Runs below are structural fixtures, not real independent research executions.
"""
import json
import sqlite3

import pytest

from a_share_research import api_keys, db, external_research as external
from support.reports import MATERIAL, report
from a_share_research.material import Material


@pytest.fixture
def state(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    principals = {}
    for role in ('research', 'assistant', 'owner'):
        principals[role] = api_keys.authenticate(conn, api_keys.create(conn, role, role=role))
    principals['other'] = api_keys.authenticate(conn, api_keys.create(conn, 'other'))
    source = db.create_run(conn, '600519', status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    yield conn, principals, source, tmp_path / 'app.db'
    conn.close()


def test_start_is_owned_idempotent_and_material_is_server_snapshot(state):
    conn, keys, source, _ = state
    started = external.start(conn, keys['research'], 'same', source)
    assert started == external.start(conn, keys['research'], 'same', source)
    assert not started['account_aware'] and started['version'] == 0
    assert external.material_snapshot(conn, keys['research'], started['id']) == json.loads(json.dumps(MATERIAL.snapshot()))
    assert Material.from_snapshot(external.material_snapshot(conn, keys['research'], started['id'])).snapshot() == MATERIAL.snapshot()
    aware = external.start(conn, keys['assistant'], 'same', source)
    assert aware['account_aware'] and aware['id'] != started['id']
    with pytest.raises(TypeError):
        external.start(conn, keys['research'], 'upload', source, material=MATERIAL.snapshot())
    other_source = db.create_run(conn, '600519', status='done')
    db.save_material(conn, other_source, MATERIAL.snapshot())
    with pytest.raises(external.SubmissionError, match='编号'):
        external.start(conn, keys['research'], 'same', other_source)


@pytest.mark.parametrize('kind,status', [('external', 'done'), ('diagnostic', 'done'), ('research', 'running')])
def test_untrusted_source_run_is_rejected(state, kind, status):
    conn, keys, _, _ = state
    source = db.create_run(conn, '600519', kind=kind, status=status)
    db.save_material(conn, source, MATERIAL.snapshot())
    with pytest.raises(external.SubmissionError) as error:
        external.start(conn, keys['research'], 'bad-source', source)
    assert error.value.status == 404
    assert conn.execute('SELECT count(*) FROM external_research').fetchone()[0] == 0


def test_owner_other_key_and_revoked_key_cannot_access(state):
    conn, keys, source, _ = state
    item = external.start(conn, keys['research'], 'owned', source)
    for key in (keys['owner'], keys['other']):
        for action in (
            lambda: external.status(conn, key, item['id']),
            lambda: external.material_snapshot(conn, key, item['id']),
            lambda: external.submit(conn, key, item['id'], 0, report()),
        ):
            with pytest.raises(external.SubmissionError):
                action()
    with pytest.raises(external.SubmissionError) as error:
        external.start(conn, keys['owner'], 'owner', source)
    assert error.value.status == 403
    conn.execute('DELETE FROM api_keys WHERE id=?', (keys['research'].key_id,))
    with pytest.raises(external.SubmissionError) as error:
        external.status(conn, keys['research'], item['id'])
    assert error.value.status == 403


def test_forged_citation_rejected_then_corrected_and_frozen(state):
    conn, keys, source, _ = state
    item = external.start(conn, keys['research'], 'revision', source)
    forged = report()
    forged['business'][0]['cites'][0]['quote'] = '该公司已披露不存在的全新业务并且承诺每年利润翻倍。'
    rejected = external.submit(conn, keys['research'], item['id'], 0, forged)
    assert not rejected['accepted'] and rejected['problems'] and rejected['version'] == 1
    assert external.status(conn, keys['research'], item['id'])['status'] == 'collecting'
    assert external.submit(conn, keys['research'], item['id'], 0, forged) == rejected
    accepted = external.submit(conn, keys['research'], item['id'], 1, report())
    assert accepted['accepted'] and not accepted['problems'] and accepted['version'] == 2
    assert accepted['review_status'] == 'not_queued'
    assert external.submit(conn, keys['research'], item['id'], 0, report()) == accepted
    final = external.status(conn, keys['research'], item['id'])
    assert final['status'] == 'submitted' and len(final['receipts']) == 2
    revised = external.submit(conn, keys['research'], item['id'], 2, report(gaps=['待补充']))
    assert revised['accepted'] and revised['version'] == 3
    assert revised['submission_id'] != accepted['submission_id']
    assert external.submit(conn, keys['research'], item['id'], 0, report()) == accepted
    assert external.status(conn, keys['research'], item['id'])['receipts'][:2] == final['receipts']
    assert conn.execute("SELECT count(*) FROM runs WHERE status='queued'").fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM reports').fetchone()[0] == 0


def test_second_connection_stale_version_rejected_atomically(state):
    conn, keys, source, path = state
    item = external.start(conn, keys['research'], 'concurrent', source)
    other = db.connect(path)
    try:
        stale_version = external.status(other, keys['research'], item['id'])['version']
        rejected = external.submit(conn, keys['research'], item['id'], 0, {})
        assert not rejected['accepted']
        with pytest.raises(external.SubmissionError, match='版本冲突'):
            external.submit(other, keys['research'], item['id'], stale_version, report())
        assert external.status(other, keys['research'], item['id'])['version'] == 1
        assert conn.execute('SELECT count(*) FROM external_submissions').fetchone()[0] == 1
    finally:
        other.close()


def test_immutable_receipts_and_material_identity(state):
    conn, keys, source, _ = state
    item = external.start(conn, keys['research'], 'immutable', source)
    receipt = external.submit(conn, keys['research'], item['id'], 0, report())
    for sql, args in (
        ('UPDATE external_submissions SET draft=? WHERE id=?', ('{}', receipt['submission_id'])),
        ('DELETE FROM external_submissions WHERE id=?', (receipt['submission_id'],)),
        ('UPDATE external_research SET material=? WHERE id=?', ('{}', item['id'])),
        ('UPDATE external_research SET owner_key_id=? WHERE id=?', (keys['other'].key_id, item['id'])),
        ('UPDATE external_research SET account_aware=1 WHERE id=?', (item['id'],)),
    ):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute(sql, args)
    assert external.material_snapshot(conn, keys['research'], item['id']) == json.loads(json.dumps(MATERIAL.snapshot()))
    assert external.submit(conn, keys['research'], item['id'], 0, report()) == receipt
