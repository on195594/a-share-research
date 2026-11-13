"""Acquisition release gates, using real captured report excerpts and PDF bytes.

These are local compatibility/storage checks, not Docker or production acceptance.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from a_share_research import (api_keys, backup_refs, db, deploy, external_operations,
                             external_research, initial_release, integrity, rollback, rollback_probe)
from a_share_research import acquisition_process, external_acquisition
from a_share_research.tools.server import create_app
from support.reports import MATERIAL, report


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'structural-release-key'))
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    item = external_research.start(conn, principal, 'held', source)
    yield conn, path, principal, item['id']
    conn.close()


def acquire(state, monkeypatch):
    conn, _, principal, external_id = state
    operation = external_operations.acquire(conn, principal, 'calculate', external_id, 0, 'calculate', {
        'label': '经营现金流同比', 'operation': 'growth_pct',
        'left': {'ref': 'T.op_cash', 'period': '2025-12-31'},
        'right': {'ref': 'T.op_cash', 'period': '2024-12-31'},
    })
    # Exercise real deterministic tool/ledger code; process lifecycle is tested separately.
    monkeypatch.setattr(acquisition_process, 'run', lambda request, **kwargs: external_acquisition.execute(request))
    external_operations.execute(conn, operation['id'])
    result = external_operations.operation(conn, principal, operation['id'])
    assert result['status'] == 'done'
    return result


def test_backup_reference_projection_reads_every_revision():
    # Storage-only rows isolate backup selection; no company research/assertions are created.
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE external_materials(revision INTEGER,material TEXT)')
        expected = []
        for number, filename in enumerate(('601138-buyback-announcement.pdf', '601088-h-share-announcement.pdf')):
            sha = hashlib.sha256((Path(__file__).parent / 'data' / filename).read_bytes()).hexdigest()
            expected.append(sha)
            conn.execute('INSERT INTO external_materials VALUES (?,?)',
                         (number, json.dumps({'data': {'doc': {'sha256': sha}}})))
        assert backup_refs.required(conn)['pdf'] == sorted(expected)


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_idle_refuses_inflight_acquisition_even_without_any_research_queue(state, status):
    conn, path, principal, _ = state
    deploy.idle(path)
    operation = external_operations.open_research(conn, principal, 'new-company', '600938')
    if status == 'running':
        conn.execute("UPDATE external_operations SET status='running',started_at=?,deadline=? WHERE id=?",
                     (db.now(), '2099-01-01T00:00:00+08:00', operation['id']))
    with pytest.raises(RuntimeError, match='取证'):
        deploy.idle(path)
    conn.execute("UPDATE external_operations SET status='failed',finished_at=?,error='structural-stop' WHERE id=?",
                 (db.now(), operation['id']))
    deploy.idle(path)


def test_integrity_and_rollback_replay_old_submission_after_new_revision(state, tmp_path, monkeypatch):
    conn, path, principal, external_id = state
    original = external_research.submit(conn, principal, external_id, 0, report())
    acquire(state, monkeypatch)
    assert integrity.check(path, tmp_path / 'pdf')[0] == []
    assert conn.execute('SELECT material_revision FROM external_submissions WHERE id=?',
                        (original['submission_id'],)).fetchone()[0] == 0
    directory = tmp_path / 'probe'
    rollback.snapshot(path, directory)
    assert rollback_probe.probe(directory)['ok'] is True
    with closing(sqlite3.connect(directory / 'bundle/app.db')) as restored:
        assert restored.execute('SELECT count(*) FROM external_materials').fetchone()[0] == 2
        assert restored.execute('SELECT status FROM external_operations').fetchone()[0] == 'done'
    # Emulate an earlier image's supported migrations, without migrating or editing the bundle.
    before = (directory / 'bundle/app.db').read_bytes()
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:21])
    result = rollback_probe.probe(directory)
    assert result['ok'] is False
    assert result['reason'] in ('scan_requests_unsupported', 'external_acquisition_unsupported')
    assert (directory / 'bundle/app.db').read_bytes() == before


@pytest.mark.parametrize('damage', ['material_hash', 'snapshot_hash', 'parent_revision', 'operation_hash', 'operation_link'])
def test_integrity_detects_corrupt_revision_or_operation(state, tmp_path, monkeypatch, damage):
    conn, path, _, _ = state
    acquire(state, monkeypatch)
    assert integrity.check(path, tmp_path / 'pdf')[0] == []
    # Explicit offline corruption: the normal application rejects these writes.
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'external_%'").fetchall():
        conn.execute('DROP TRIGGER ' + row[0])
    conn.execute('PRAGMA foreign_keys=OFF')
    statements = {
        'snapshot_hash': "UPDATE external_materials SET material=json_set(material,'$.sha256','bad') WHERE revision=1",
        'material_hash': "UPDATE external_materials SET material_sha256='bad' WHERE revision=1",
        'parent_revision': 'UPDATE external_materials SET parent_revision=NULL WHERE revision=1',
        'operation_hash': "UPDATE external_operations SET parameters_sha256='bad'",
        'operation_link': 'UPDATE external_operations SET material_revision=99',
    }
    conn.execute(statements[damage])
    problems = integrity.check(path, tmp_path / 'pdf')[0]
    assert problems, damage
    assert any('材料' in item or '取证' in item for item in problems), problems


@pytest.mark.parametrize('missing', ['external_operations', 'external_materials'])
def test_tools_readiness_requires_acquisition_tables(state, missing):
    conn, path, *_ = state
    app = create_app(path, external_enabled=True)
    ready = next(route.endpoint for route in app.routes if route.path == '/ready')
    assert ready()['database_ready'] is True
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute('DROP TABLE ' + missing)
    response = ready()
    assert response.status_code == 503
    assert json.loads(response.body)['database_ready'] is False


def test_initial_release_uses_current_target_and_preserves_v9_rows(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:9])
        with closing(db.connect(path)) as conn:
            # Deliberately structural credential, with no production or financial input.
            conn.execute('INSERT INTO api_keys(name,token_hash,created_at) VALUES (?,?,?)',
                         ('structural', hashlib.sha256(b'structural').hexdigest(), db.now()))
    before = initial_release.state(path)
    def local_migration(image, database, evidence):
        assert image == 'structural-current-image'
        with closing(db.connect(database)) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == len(db.MIGRATIONS)
    monkeypatch.setattr(initial_release, '_run_migration', local_migration)
    result = initial_release.migrate(path, 'structural-current-image', tmp_path / 'evidence')
    assert result['target_version'] == len(db.MIGRATIONS) > 21
    after = initial_release.state(path, before['columns'])
    assert after['tables'] == before['tables']
    assert integrity.check(path, tmp_path / 'pdf')[0] == []
