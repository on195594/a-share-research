"""First-release guards on captured research, not Docker or production acceptance.

Only the image migration boundary is replaced with the real local migrations.
Legacy credentials and deliberate corruption are marked structural fixtures.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from a_share_research import db, initial_release, integrity
from support import continuation


TARGET = {'web': 'sha256:' + 'a' * 64, 'worker': 'sha256:' + 'b' * 64}
RECOVERY = {'web': 'sha256:' + 'c' * 64, 'worker': 'sha256:' + 'd' * 64}


@pytest.fixture
def v9(tmp_path, monkeypatch):
    directory = tmp_path / 'source'
    directory.mkdir(mode=0o700)
    path = directory / 'app.db'
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:9])
        # Reports 37/39 reference material/PDFs absent from this fixture. Select
        # the self-contained captured records without altering their fields.
        old.setattr(continuation, 'RECORDS', [r for r in continuation.RECORDS
                                            if not r['report']['body'].get('material_sha256')])
        with closing(db.connect(path)) as conn:
            continuation.load(conn)
            for name in ('retained-structural-key', 'deleted-structural-key'):
                conn.execute('INSERT INTO api_keys(name,token_hash,created_at) VALUES (?,?,?)',
                             (name, hashlib.sha256(name.encode()).hexdigest(), db.now()))
            conn.execute("DELETE FROM api_keys WHERE name='deleted-structural-key'")
            assert conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', ('api_keys',)).fetchone()[0] == 2
    path.chmod(0o600)
    pdf = Path(__file__).parent / 'data/601138-buyback-announcement.pdf'
    contents = pdf.read_bytes()
    (directory / 'pdf').mkdir(mode=0o700)
    copied = directory / 'pdf' / (hashlib.sha256(contents).hexdigest() + '.pdf')
    copied.write_bytes(contents)
    copied.chmod(0o600)
    assert integrity.check(path, directory / 'pdf')[0] == []
    return path


def local_migration(image, database, evidence):
    """Real migration code, explicitly not a container/image compatibility test."""
    assert image == TARGET['worker']
    with closing(db.connect(database)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == len(db.MIGRATIONS)


def destination(tmp_path, name='migration'):
    return tmp_path / name


def test_state_reads_generated_columns_and_sequences_without_changing_database(v9):
    original = v9.read_bytes()
    snapshot = initial_release.state(v9)
    assert snapshot['version'] == 9
    assert 'company_key' in snapshot['columns']['runs']
    assert 'company_key' in snapshot['columns']['reports']
    assert snapshot['tables']['runs']['count'] > 0
    assert snapshot['tables']['sqlite_sequence']['count'] > 0
    assert initial_release.state(v9) == snapshot
    assert v9.read_bytes() == original
    # Hash a generated result independently of its ordinary input column.
    generated = {'runs': ['id', 'company_key']}
    before = initial_release.state(v9, generated)
    with closing(sqlite3.connect(v9, isolation_level=None)) as conn:
        conn.execute("UPDATE runs SET code='000001' WHERE id=7")  # Deliberate captured-row corruption.
    assert initial_release.state(v9, generated)['tables']['runs'] != before['tables']['runs']


def test_migrate_preserves_captured_rows_generated_values_and_deleted_key_sequence(v9, tmp_path, monkeypatch):
    before = initial_release.state(v9)
    monkeypatch.setattr(initial_release, '_run_migration', local_migration)
    evidence = destination(tmp_path)
    report = initial_release.migrate(v9, TARGET['worker'], evidence)
    after = initial_release.state(v9, before['columns'])
    assert after['version'] == len(db.MIGRATIONS)
    assert after['tables'] == before['tables']
    with closing(sqlite3.connect(v9)) as conn:
        assert conn.execute('SELECT role FROM api_keys').fetchall() == [('research',)]
        assert conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', ('api_keys',)).fetchone()[0] == 2
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 0
    assert integrity.check(v9, v9.parent / 'pdf')[0] == []
    assert json.loads((evidence / 'migration.json').read_text()) == report


@pytest.mark.parametrize('version', [8, 10, 21, 22])
def test_wrong_source_version_refuses_before_running_a_migration(v9, tmp_path, monkeypatch, version):
    with closing(sqlite3.connect(v9)) as conn:
        conn.execute(f'PRAGMA user_version={version}')
    before = v9.read_bytes()
    def forbidden(*args):
        pytest.fail('wrong source version must not start the image migration')
    monkeypatch.setattr(initial_release, '_run_migration', forbidden)
    with pytest.raises(initial_release.InitialReleaseError):
        initial_release.migrate(v9, TARGET['worker'], destination(tmp_path))
    assert v9.read_bytes() == before


@pytest.mark.parametrize('damage', ['old_value', 'old_sequence', 'role', 'new_foreign_key',
                                    'future_version', 'unchanged_version'])
def test_incomplete_or_changed_migration_cannot_pass(v9, tmp_path, monkeypatch, damage):
    def damaged(image, database, evidence):
        if damage == 'unchanged_version':
            return
        local_migration(image, database, evidence)
        with closing(sqlite3.connect(database, isolation_level=None)) as conn:
            if damage == 'old_value':
                conn.execute("UPDATE reports SET name=name || ' structural corruption' WHERE id=5")
            elif damage == 'old_sequence':
                conn.execute("UPDATE sqlite_sequence SET seq=1 WHERE name='api_keys'")
            elif damage == 'role':
                conn.execute('DROP TRIGGER api_keys_role_immutable')
                conn.execute("UPDATE api_keys SET role='assistant'")
            elif damage == 'new_foreign_key':
                # An orphan in a new table cannot be caught by old-column hashes.
                conn.execute('INSERT INTO scan_requests VALUES (?,?,?,?)',
                             ('key:' + 'f' * 64, 'structural-orphan', 999, db.now()))
            else:
                conn.execute(f'PRAGMA user_version={len(db.MIGRATIONS) + 1}')
    monkeypatch.setattr(initial_release, '_run_migration', damaged)
    with pytest.raises(initial_release.InitialReleaseError):
        initial_release.migrate(v9, TARGET['worker'], destination(tmp_path))


@pytest.mark.parametrize('compatible', [True, False])
def test_prepare_probes_the_migrated_complete_copy_and_keeps_source_unchanged(
        v9, tmp_path, monkeypatch, compatible):
    source_bytes = {p.relative_to(v9.parent): p.read_bytes()
                    for p in v9.parent.rglob('*') if p.is_file()}
    original = initial_release.state(v9)
    monkeypatch.setattr(initial_release, '_run_migration', local_migration)
    checked = []
    def assess(images, database, evidence):
        assert images == RECOVERY
        assert database.resolve() != v9.resolve()
        migrated = initial_release.state(database, original['columns'])
        assert migrated['version'] == len(db.MIGRATIONS) and migrated['tables'] == original['tables']
        assert integrity.check(database, database.parent / 'pdf')[0] == []
        original_pdf = next((v9.parent / 'pdf').iterdir())
        assert (database.parent / 'pdf' / original_pdf.name).read_bytes() == original_pdf.read_bytes()
        checked.append(database)
        return {'ok': compatible, 'scope': 'read_only',
                'reason': 'read_compatible' if compatible else 'image_probe_failed',
                'images': {role: {'ok': compatible, 'image': image,
                                 'reason': 'read_compatible' if compatible else 'scan_requests_unsupported'}
                           for role, image in images.items()}}
    monkeypatch.setattr(initial_release.rollback, 'assess', assess)
    evidence = destination(tmp_path, 'prepare')
    if compatible:
        initial_release.prepare(v9, TARGET, RECOVERY, evidence)
    else:
        with pytest.raises(initial_release.InitialReleaseError):
            initial_release.prepare(v9, TARGET, RECOVERY, evidence)
    assert checked
    assert initial_release.state(v9) == original
    assert all((v9.parent / name).read_bytes() == content for name, content in source_bytes.items())


@pytest.mark.parametrize('operation', ['migrate', 'prepare'])
def test_existing_evidence_is_preserved_without_starting_migration(v9, tmp_path, monkeypatch, operation):
    evidence = destination(tmp_path)
    evidence.mkdir(mode=0o700)
    retained = evidence / 'keep.txt'
    retained.write_text('existing diagnostic evidence')
    before = v9.read_bytes()
    def forbidden(*args):
        pytest.fail('an existing evidence directory must not start migration')
    monkeypatch.setattr(initial_release, '_run_migration', forbidden)
    with pytest.raises(FileExistsError):
        if operation == 'migrate':
            initial_release.migrate(v9, TARGET['worker'], evidence)
        else:
            initial_release.prepare(v9, TARGET, RECOVERY, evidence)
    assert list(evidence.iterdir()) == [retained]
    assert retained.read_text() == 'existing diagnostic evidence'
    assert v9.read_bytes() == before


@pytest.mark.parametrize('failure', ['timeout', 'invalid_json', 'wrong_version', 'nonzero'])
def test_image_migration_failure_removes_only_its_own_container(tmp_path, monkeypatch, failure):
    """Process-boundary simulation, not a claim that Docker was exercised."""
    evidence = destination(tmp_path)
    evidence.mkdir(mode=0o700)
    launched, removed = [], []
    output = {'invalid_json': 'incomplete response', 'wrong_version': '{"schema_version":20}',
              'nonzero': json.dumps({'schema_version': len(db.MIGRATIONS)})}.get(failure)
    def run(command, **kwargs):
        if command[:2] == ['docker', 'run']:
            launched.append(command[command.index('--name') + 1])
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            return subprocess.CompletedProcess(command, 1 if failure == 'nonzero' else 0, output, '')
        assert command[:3] == ['docker', 'rm', '-f']
        removed.append(command[3])
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(initial_release.subprocess, 'run', run)
    with pytest.raises(initial_release.InitialReleaseError):
        initial_release._run_migration(TARGET['worker'], tmp_path / 'app.db', evidence)
    assert len(launched) == 1
    assert removed == launched  # Never clean an unrelated or guessed container name.
    if failure != 'timeout':
        diagnostic = json.loads((evidence / 'migration-process.json').read_text())
        assert diagnostic['stdout'] == output


@pytest.mark.parametrize('cleanup_failure', ['nonzero', 'timeout'])
def test_target_schema_receipt_does_not_override_unconfirmed_container_cleanup(
        tmp_path, monkeypatch, cleanup_failure):
    """A successful migration process cannot authorize service start while cleanup is uncertain."""
    evidence = destination(tmp_path)
    evidence.mkdir(mode=0o700)
    launched, removed = [], []
    def run(command, **kwargs):
        if command[:2] == ['docker', 'run']:
            launched.append(command[command.index('--name') + 1])
            return subprocess.CompletedProcess(command, 0, json.dumps({'schema_version': len(db.MIGRATIONS)}), '')
        assert command[:3] == ['docker', 'rm', '-f']
        removed.append(command[3])
        if cleanup_failure == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return subprocess.CompletedProcess(command, 1, '', 'container still busy')
    monkeypatch.setattr(initial_release.subprocess, 'run', run)
    with pytest.raises(initial_release.InitialReleaseError, match='清理'):
        initial_release._run_migration(TARGET['worker'], tmp_path / 'app.db', evidence)
    assert len(launched) == 1 and removed == launched
    diagnostic = json.loads((evidence / 'migration-process.json').read_text())
    assert diagnostic['exit_code'] == 0 and json.loads(diagnostic['stdout']) == {'schema_version': len(db.MIGRATIONS)}
