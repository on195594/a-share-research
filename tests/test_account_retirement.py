"""Structural legacy copies only; these tests do not constitute broker acceptance."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from a_share_research.account import mirror, retirement
from test_account import source_database


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / 'old'
    directory.mkdir(mode=0o700)
    path = source_database(directory / 'cache.db')
    (directory / retirement.STATE).write_text('{"structural": true}')
    yield path
    # Restore only task-owned fixture directories so pytest can remove them.
    directory.chmod(0o700)
    for child in directory.iterdir():
        if not child.is_symlink():
            child.chmod(0o700 if child.is_dir() else 0o600)


def sha(source):
    return mirror.digest(mirror.read_source(source))


def trigger_count(source):
    conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        return conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'account_retired_%'").fetchone()[0]
    finally:
        conn.close()


def test_seal_preserves_content_and_is_idempotent(source):
    expected = sha(source)
    before = retirement.inspect(source, source.parent)
    assert not before['fenced'] and before['trigger_count'] == 0
    assert before['state_sha256'] == hashlib.sha256((source.parent / retirement.STATE).read_bytes()).hexdigest()
    result = retirement.fence(source, source.parent, expected)
    assert result['fenced'] and result['source_sha256'] == expected
    assert result['trigger_count'] == 3 * (len(mirror.MANIFEST) + len(mirror.SKIPPED_TABLES))
    assert sha(source) == expected
    assert retirement.fence(source, source.parent, expected) == result
    assert retirement.verify(source, source.parent, expected) == result
    assert source.stat().st_mode & 0o777 == 0o400
    assert source.parent.stat().st_mode & 0o777 == 0o500


def test_new_process_reads_but_cannot_write_or_replace(source):
    if os.getuid() == 0:
        pytest.skip('root bypasses ordinary filesystem access checks')
    retirement.fence(source, source.parent, sha(source))
    script = '''
import sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1])
conn = sqlite3.connect(p)
assert conn.execute('SELECT count(*) FROM holdings').fetchone()[0] == 2
try:
    conn.execute("UPDATE holdings SET notes='changed'")
except sqlite3.DatabaseError:
    pass
else:
    raise AssertionError('SQL write allowed')
for name, temporary in (('account-snapshot.json', 'account-snapshot.json.tmp'),
                        ('stop-alert-state.json', 'stop-alert-state.tmp')):
    try:
        (p.parent / temporary).write_text('{}')
        (p.parent / temporary).replace(p.parent / name)
    except PermissionError:
        pass
    else:
        raise AssertionError('side file replacement allowed')
'''
    subprocess.run([sys.executable, '-c', script, str(source)], check=True, timeout=10)


def test_guards_survive_owner_restoring_filesystem_write_permissions(source):
    expected = sha(source)
    retirement.fence(source, source.parent, expected)
    source.parent.chmod(0o700)
    source.chmod(0o600)
    conn = sqlite3.connect(source)
    try:
        for sql in ("UPDATE holdings SET notes='changed'", 'DELETE FROM quote_snapshots',
                    "INSERT INTO schema_migrations VALUES('unsafe', '2026-11-11')"):
            with pytest.raises(sqlite3.IntegrityError, match='账户已迁移'):
                conn.execute(sql)
    finally:
        conn.close()
    with pytest.raises(retirement.RetirementError, match='尚未完整封锁'):
        retirement.verify(source, source.parent, expected)
    assert retirement.fence(source, source.parent, expected)['fenced']


def test_connection_opened_before_fence_cannot_reuse_cached_writer(source):
    conn = sqlite3.connect(source)
    sql = 'UPDATE holdings SET notes=notes'
    conn.execute(sql)
    conn.commit()
    try:
        expected = sha(source)
        retirement.fence(source, source.parent, expected)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
        assert sha(source) == expected
    finally:
        conn.close()


def test_hash_mismatch_does_not_mutate(source):
    content = source.read_bytes()
    modes = (source.stat().st_mode, source.parent.stat().st_mode)
    with pytest.raises(retirement.RetirementError, match='内容已变化'):
        retirement.fence(source, source.parent, '0' * 64)
    assert source.read_bytes() == content
    assert modes == (source.stat().st_mode, source.parent.stat().st_mode)
    assert trigger_count(source) == 0


def test_recheck_content_inside_write_transaction(source, monkeypatch):
    original = retirement.inspect
    expected = sha(source)
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE holdings SET notes='concurrent change'")
        return result
    monkeypatch.setattr(retirement, 'inspect', changed)
    with pytest.raises(retirement.RetirementError, match='内容已变化'):
        retirement.fence(source, source.parent, expected)
    assert trigger_count(source) == 0


@pytest.mark.parametrize('variant', ['unexpected', 'symlink', 'hardlink', 'shared', 'schema'])
def test_unsafe_layout_and_schema_are_rejected(source, tmp_path, variant):
    expected = sha(source)
    if variant == 'unexpected':
        (source.parent / 'runtime.env').write_text('structural')
    elif variant == 'symlink':
        (source.parent / retirement.STATE).unlink()
        (source.parent / retirement.STATE).symlink_to(source.parent / 'account-snapshot.json')
    elif variant == 'hardlink':
        os.link(source, tmp_path / 'other.db')
    elif variant == 'shared':
        source.parent.chmod(0o750)
    else:
        with sqlite3.connect(source) as conn:
            conn.execute('CREATE TABLE unexpected (id INTEGER)')
    with pytest.raises(retirement.RetirementError):
        retirement.fence(source, source.parent, expected)
    assert trigger_count(source) == 0


def test_trigger_name_collision_is_not_overwritten(source):
    name = next(iter(retirement._triggers()))
    with sqlite3.connect(source) as conn:
        conn.execute(f'CREATE TRIGGER {name} BEFORE UPDATE ON holdings BEGIN SELECT 1; END')
    with pytest.raises(retirement.RetirementError, match='名称冲突'):
        retirement.fence(source, source.parent, sha(source))
    assert trigger_count(source) == 1


def test_partial_chmod_failure_keeps_triggers_and_can_resume(source, monkeypatch):
    expected = sha(source)
    original = Path.chmod
    def fail(path, mode, *args, **kwargs):
        if path == source.parent and mode == 0o500:
            raise PermissionError('structural interruption')
        return original(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'chmod', fail)
        with pytest.raises(retirement.RetirementError, match='已有封锁保持不变'):
            retirement.fence(source, source.parent, expected)
    assert trigger_count(source) == len(retirement._triggers())
    assert source.stat().st_mode & 0o777 == 0o400
    assert not retirement.inspect(source, source.parent)['fenced']
    assert retirement.fence(source, source.parent, expected)['fenced']


def test_busy_wal_reader_leaves_guards_and_retry_completes(source):
    writer = sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode=WAL')
    reader = sqlite3.connect(source)
    reader.execute('BEGIN')
    reader.execute('SELECT * FROM holdings').fetchall()
    expected = sha(source)
    try:
        with pytest.raises(retirement.RetirementError, match='活动连接'):
            retirement.fence(source, source.parent, expected)
        assert trigger_count(source) == len(retirement._triggers())
    finally:
        reader.close()
        writer.close()
    assert retirement.fence(source, source.parent, expected)['fenced']


@pytest.mark.parametrize('present', [False, True])
def test_separate_state_directory_and_missing_state(source, tmp_path, present):
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    if present:
        (state / retirement.STATE).write_bytes(b'{"separate":true}')
    try:
        result = retirement.fence(source, state, sha(source))
        expected = hashlib.sha256(b'{"separate":true}').hexdigest() if present else None
        assert result['fenced'] and result['state_sha256'] == expected
        assert state.stat().st_mode & 0o777 == 0o500
    finally:
        state.chmod(0o700)
        if present:
            (state / retirement.STATE).chmod(0o600)


def test_explicit_archives_remain_unchanged(source, tmp_path):
    archive = source.parent / 'backups'
    archive.mkdir(mode=0o700)
    (archive / 'historical.json').write_bytes(b'original archive')
    receipt = source.parent / 'rollback-receipt.json'
    receipt.write_bytes(b'original receipt')
    target = tmp_path / 'runtime-target'
    target.mkdir(mode=0o700)
    link = source.parent / 'runtime'
    link.symlink_to(target, target_is_directory=True)
    entries = (archive, receipt, link)
    modes = {p: p.lstat().st_mode for p in (*entries, target)}
    expected = sha(source)
    result = retirement.fence(source, source.parent, expected, preserve_entries=entries)
    assert result['fenced'] and result['preserved_entries'] == sorted(map(str, entries))
    assert modes == {p: p.lstat().st_mode for p in (*entries, target)}
    assert (archive / 'historical.json').read_bytes() == b'original archive'
    assert receipt.read_bytes() == b'original receipt' and link.readlink() == target
    assert retirement.fence(source, source.parent, expected, preserve_entries=entries) == result
    assert retirement.verify(source, source.parent, expected, preserve_entries=entries) == result
    with pytest.raises(retirement.RetirementError):
        retirement.verify(source, source.parent, expected)


@pytest.mark.parametrize('variant', ['unlisted', 'missing', 'protected', 'state-temp', 'outside', 'nested', 'duplicate'])
def test_invalid_preservation_inventory_rejects_without_mutation(source, tmp_path, variant):
    archive = source.parent / 'backups'
    archive.mkdir(mode=0o700)
    expected = sha(source)
    entries = (archive,)
    if variant == 'unlisted':
        (source.parent / 'new-unlisted-file').write_text('new')
    elif variant == 'missing':
        entries += (source.parent / 'missing',)
    elif variant == 'protected':
        entries += (source,)
    elif variant == 'state-temp':
        temporary = source.parent / retirement.STATE_TEMP
        temporary.write_text('old temporary state')
        entries += (temporary,)
    elif variant == 'outside':
        entries += (tmp_path,)
    elif variant == 'nested':
        child = archive / 'inside'
        child.write_text('nested')
        entries += (child,)
    else:
        entries += (archive,)
    with pytest.raises(retirement.RetirementError):
        retirement.fence(source, source.parent, expected, preserve_entries=entries)
    assert trigger_count(source) == 0
    assert source.parent.stat().st_mode & 0o777 == 0o700


def test_home_directory_is_never_sealed(source, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: source.parent))
    with pytest.raises(retirement.RetirementError, match='用户主目录'):
        retirement.fence(source, source.parent, sha(source))
    assert trigger_count(source) == 0


def test_existing_old_runtime_state_temporary_is_sealed(source):
    temporary = source.parent / retirement.STATE_TEMP
    temporary.write_bytes(b'old interrupted state')
    assert retirement.fence(source, source.parent, sha(source))['fenced']
    assert temporary.read_bytes() == b'old interrupted state'
    assert temporary.stat().st_mode & 0o777 == 0o400
