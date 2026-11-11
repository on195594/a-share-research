"""Captured research records plus invented account/storage fixtures; never production data."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import stat

import pytest

from a_share_research import api_keys, backup, db
from a_share_research.account import evidence, ledger
from test_integrity import research_db, PDF, SHA


def database_rows(path):
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        result = {table: sorted(conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr) for table in tables}
        result['__user_version__'] = conn.execute('PRAGMA user_version').fetchone()[0]
        return result


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / 'source'
    directory.mkdir(mode=0o700)
    conn, run = research_db(directory)
    token = api_keys.create(conn, 'invented-backup-account', role='assistant')
    principal = api_keys.authenticate(conn, token)
    sha = evidence.store(directory / 'account-evidence', b'INVENTED ACCOUNT OPENING; NOT BROKER OR INVESTMENT EVIDENCE')
    ledger.initialize(conn, principal, 'structural', dict(positions={}, cash='10000'),
        datetime.fromisoformat('2026-11-01T00:00:00+08:00'), directory / 'account-evidence', sha, tolerance=Decimal(0))
    # Allocate and delete a run so preserving sqlite_sequence is observably different from MAX(id).
    removed = db.create_run(conn, '600001', status='queued')
    conn.execute('DELETE FROM runs WHERE id=?', (removed,))
    yield directory, conn, principal, sha, run
    conn.close()


def create(source, tmp_path):
    target = tmp_path / 'bundle'
    manifest = backup.create(source[0] / 'app.db', target)
    return target, manifest


def test_backup_restore_preserve_every_row_sequences_and_private_permissions(source, tmp_path):
    database = source[0] / 'app.db'
    before = database_rows(database)
    bundle, manifest = create(source, tmp_path)
    assert manifest['format'] == 'ashare-backup-v1'
    assert manifest['required']['account_evidence'] == [source[3]]
    assert json.loads((bundle / 'manifest.json').read_text()) == manifest
    assert manifest['user_version'] == before['__user_version__']
    assert database_rows(bundle / 'app.db') == before
    original_bundle = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    assert backup.verify(bundle)
    assert {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()} == original_bundle
    restored = tmp_path / 'restored'
    assert backup.restore(bundle, restored)
    assert {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()} == original_bundle
    assert database_rows(restored / 'app.db') == before == database_rows(database)
    assert evidence.read(restored / 'account-evidence', source[3]) == evidence.read(source[0] / 'account-evidence', source[3])
    for root in (bundle, restored):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for path in root.rglob('*'):
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    for name, metadata in manifest['files'].items():
        content = (bundle / name).read_bytes()
        assert metadata == dict(sha256=hashlib.sha256(content).hexdigest(), size=len(content))


@pytest.mark.parametrize('damage', ['missing', 'tampered'])
def test_committed_account_evidence_must_exist_and_match_hash(source, tmp_path, damage):
    path = source[0] / 'account-evidence' / source[3]
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'Tampered invented evidence')
    target = tmp_path / 'bundle'
    with pytest.raises(backup.BackupError):
        backup.create(source[0] / 'app.db', target)
    assert not target.exists()


def test_failed_request_with_unknown_evidence_does_not_block_backup(source, tmp_path):
    directory, conn, principal, _, _ = source
    rejected = ledger.submit(conn, principal, 'structural', ledger.Batch(expected_version=0,
        events=[], evidence_hashes=['0' * 64], snapshot=dict(positions={}, cash='10000', market_values={}, total_assets='10000')),
        directory / 'account-evidence')
    assert rejected['status'] == 'missing_material'
    bundle, manifest = create(source, tmp_path)
    assert '0' * 64 not in manifest['required']['account_evidence']
    assert backup.verify(bundle)
    assert database_rows(bundle / 'app.db') == database_rows(directory / 'app.db')


@pytest.mark.parametrize('operation', ['create', 'restore'])
def test_existing_destination_is_never_overwritten(source, tmp_path, operation):
    target = tmp_path / 'existing'
    target.mkdir()
    marker = target / 'keep'
    marker.write_bytes(b'Existing unrelated data')
    if operation == 'create':
        action = lambda: backup.create(source[0] / 'app.db', target)
    else:
        bundle, _ = create(source, tmp_path)
        action = lambda: backup.restore(bundle, target)
    with pytest.raises(backup.BackupError):
        action()
    assert marker.read_bytes() == b'Existing unrelated data'
    assert list(target.iterdir()) == [marker]


@pytest.mark.parametrize('damage', ['extra', 'missing', 'tampered', 'symlink', 'traversal'])
def test_verify_rejects_unsafe_or_incomplete_bundle_without_restore_output(source, tmp_path, damage):
    bundle, manifest = create(source, tmp_path)
    name = 'account-evidence/' + source[3]
    if damage == 'extra':
        (bundle / 'unlisted').write_bytes(b'Unexpected bytes')
    elif damage == 'missing':
        (bundle / name).unlink()
    elif damage == 'tampered':
        (bundle / name).write_bytes(b'Changed evidence')
    elif damage == 'symlink':
        (bundle / name).unlink()
        (bundle / name).symlink_to(source[0] / name)
    else:
        manifest['files']['../outside'] = manifest['files'][name]
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError):
        backup.verify(bundle)
    restored = tmp_path / 'restored'
    with pytest.raises(backup.BackupError):
        backup.restore(bundle, restored)
    assert not restored.exists()


def test_manifest_cannot_omit_required_evidence_even_when_file_list_is_consistent(source, tmp_path):
    bundle, manifest = create(source, tmp_path)
    name = 'account-evidence/' + source[3]
    (bundle / name).unlink()
    del manifest['files'][name]
    manifest['required']['account_evidence'] = []
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError):
        backup.verify(bundle)


def test_source_symlink_evidence_is_not_followed(source, tmp_path):
    name = source[0] / 'account-evidence' / source[3]
    elsewhere = tmp_path / 'original-evidence'
    name.rename(elsewhere)
    name.symlink_to(elsewhere)
    with pytest.raises(backup.BackupError):
        backup.create(source[0] / 'app.db', tmp_path / 'bundle')
    assert not (tmp_path / 'bundle').exists()


def attach_storage_pdf(source):
    """Real 工业富联 PDF bytes with a storage-only record, never linked to the 茅台 research."""
    from a_share_research.evidence import material_hash
    directory, conn, *_ = source
    pdf_dir = directory / 'pdf'
    pdf_dir.mkdir(mode=0o700, exist_ok=True)
    path = pdf_dir / (SHA + '.pdf')
    path.write_bytes(PDF.read_bytes())
    path.chmod(0o600)
    data = dict(code='601138.SH', name='工业富联',
        doc=dict(code='601138.SH', title='真实回购公告PDF；结构存储边界用例，不是研究结论', sha256=SHA),
        storage_fixture='structural-only; no research assertions')
    snapshot = dict(version=1, data=data, sha256=material_hash(data))
    run = db.create_run(conn, '601138.SH', status='queued')
    db.update_run(conn, run, material=json.dumps(snapshot), identity=json.dumps({'material_sha256': snapshot['sha256']}))
    return path


def test_required_real_pdf_bytes_roundtrip_and_cache_is_excluded(source, tmp_path):
    pdf = attach_storage_pdf(source)
    pdf.with_suffix('.layout.json').write_text('{"storage_fixture":"disposable extraction cache"}')
    bundle, manifest = create(source, tmp_path)
    assert manifest['required']['pdf'] == [SHA]
    assert (bundle / 'pdf' / pdf.name).read_bytes() == PDF.read_bytes()
    assert not (bundle / 'pdf' / pdf.with_suffix('.layout.json').name).exists()
    restored = tmp_path / 'restored'
    backup.restore(bundle, restored)
    assert (restored / 'pdf' / pdf.name).read_bytes() == PDF.read_bytes()


@pytest.mark.parametrize('damage', ['missing', 'tampered'])
def test_required_pdf_missing_or_corrupt_refuses_bundle(source, tmp_path, damage):
    path = attach_storage_pdf(source)
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'Corrupt storage boundary bytes, not a PDF')
    with pytest.raises(backup.BackupError):
        backup.create(source[0] / 'app.db', tmp_path / 'bundle')
    assert not (tmp_path / 'bundle').exists()


def test_source_commit_after_snapshot_does_not_change_backup_rows_or_required_references(source, tmp_path, monkeypatch):
    directory, conn, principal, original_sha, _ = source
    before = database_rows(directory / 'app.db')
    copy = backup._copy
    committed = []
    def concurrent_copy(path, target=None):
        if Path(path).name == 'app.db' and not committed:
            sha = evidence.store(directory / 'account-evidence', b'Invented concurrent deposit evidence')
            result = ledger.submit(conn, principal, 'structural', ledger.Batch(expected_version=0,
                evidence_hashes=[sha], events=[dict(kind='deposit', cash='10', occurred_at='2026-11-02T10:00:00+08:00')],
                snapshot=dict(positions={}, cash='10010', market_values={}, total_assets='10010')), directory / 'account-evidence')
            assert result['status'] == 'posted'
            committed.append(sha)
        return copy(path, target)
    monkeypatch.setattr(backup, '_copy', concurrent_copy)
    bundle, manifest = create(source, tmp_path)
    assert committed
    assert database_rows(bundle / 'app.db') == before
    assert database_rows(directory / 'app.db') != before
    assert manifest['required']['account_evidence'] == [original_sha]
    assert backup.verify(bundle)['ok']


def test_restore_copy_failure_cleans_only_its_new_destination(source, tmp_path, monkeypatch):
    bundle, _ = create(source, tmp_path)
    original = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    target = tmp_path / 'restored'
    copy = backup._copy
    def failed_copy(path, output=None):
        if output is not None and Path(output).name == source[3]:
            raise backup.BackupError('Injected structural copy failure')
        return copy(path, output)
    monkeypatch.setattr(backup, '_copy', failed_copy)
    with pytest.raises(backup.BackupError, match='Injected'):
        backup.restore(bundle, target)
    assert not target.exists()
    assert {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()} == original


def test_verify_checks_domain_integrity_even_when_database_file_hash_is_recomputed(source, tmp_path):
    bundle, manifest = create(source, tmp_path)
    database = bundle / 'app.db'
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE runs SET material=json_set(material,'$.data.name','Corrupted structural name') WHERE id=?",
                     (source[4],))
    content = database.read_bytes()
    manifest['files']['app.db'] = dict(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match='完整性'):
        backup.verify(bundle)


def test_wal_mode_bundle_is_rejected_without_creating_or_modifying_files(source, tmp_path):
    bundle, manifest = create(source, tmp_path)
    database = bundle / 'app.db'
    conn = sqlite3.connect(database)
    try:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    finally:
        conn.close()
    content = database.read_bytes()
    manifest['files']['app.db'] = dict(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    before = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    with pytest.raises(backup.BackupError):
        backup.verify(bundle)
    assert {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob('*') if p.is_file()} == before
