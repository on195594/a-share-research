"""Fence a drained legacy account directory before binding its final mirror.

This is an operational guard, not a boundary against an owner who deliberately
changes permissions, renames the directory or removes triggers. Stop old hosts
and scheduled writers first. Failures never remove guards or restore write bits.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat

from a_share_research.account import mirror

STATE = 'stop-alert-state.json'
STATE_TEMP = 'stop-alert-state.tmp'
SIDE = {*mirror.SIDE_FILES, STATE}


class RetirementError(ValueError):
    """The source cannot be safely sealed or its seal cannot be proved."""


def _path(path: Path, *, directory: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    if directory and path in (Path(path.anchor), Path.home().absolute()):
        raise RetirementError('不能封锁文件系统根目录或用户主目录')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RetirementError('旧库路径及父目录不能是符号链接')
    info = path.stat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.getuid() or not directory and info.st_nlink != 1:
        raise RetirementError('旧库目录和文件须为当前用户所有的普通路径，文件不能有硬链接')
    if directory and info.st_mode & 0o077:
        raise RetirementError('旧库及状态目录须为属主专用目录')
    return path


def _layout(source: Path, state_directory: Path, preserve_entries: tuple[Path, ...]) -> tuple[Path, Path, list[Path], list[Path]]:
    source = _path(source)
    directory = _path(source.parent, directory=True)
    state_directory = _path(state_directory, directory=True)
    if source.name in SIDE or source.name.endswith(('-wal', '-shm', '-journal', '.tmp', '.mirror-tmp')):
        raise RetirementError('旧库文件名与侧文件或临时文件冲突')
    directories = list(dict.fromkeys((directory, state_directory)))
    protected = {source.name, *(source.name + suffix for suffix in ('-wal', '-shm', '-journal'))}
    protected |= SIDE | {STATE_TEMP} | {name + suffix for name in SIDE for suffix in ('.tmp', '.mirror-tmp')}
    preserved = set()
    for entry in preserve_entries:
        entry = Path(os.path.abspath(entry))
        if entry in preserved or entry.parent not in directories or entry.name in protected:
            raise RetirementError('保留项须是封锁目录内不重复的顶层附属路径，不能豁免受保护文件')
        entry.lstat()  # Explicit archived symlinks may be dangling; never follow them.
        preserved.add(entry)
    files = []
    for folder in directories:
        allowed = {STATE, STATE_TEMP, STATE + '.tmp'}
        if folder == directory:
            allowed |= {source.name, *(source.name + suffix for suffix in ('-wal', '-shm', '-journal'))}
            allowed |= SIDE | {name + suffix for name in SIDE for suffix in ('.tmp', '.mirror-tmp')}
        for child in folder.iterdir():
            if child in preserved:
                continue
            if child.name not in allowed:
                raise RetirementError('旧库或状态目录含其他用途文件，拒绝封锁共享目录')
            files.append(_path(child))
    return source, state_directory, directories, files


def _triggers() -> dict[str, str]:
    result = {}
    for table in sorted(set(mirror.MANIFEST) | mirror.SKIPPED_TABLES):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'account_retired_{table}_{operation.lower()}'
            result[name] = (f'CREATE TRIGGER {name} BEFORE {operation} ON "{table}" '
                            "BEGIN SELECT RAISE(ABORT, '账户已迁移；旧库只读'); END")
    return result


def _guards(conn: sqlite3.Connection) -> int:
    existing = dict(conn.execute('SELECT name, sql FROM sqlite_master WHERE type=\'trigger\''))
    expected = _triggers()
    if any(name in existing and existing[name] != sql for name, sql in expected.items()):
        raise RetirementError('旧库封锁触发器名称冲突，拒绝覆盖')
    return sum(name in existing for name in expected)


def _snapshot(source: Path, state_directory: Path, preserve_entries: tuple[Path, ...]) -> dict:
    source, state_directory, directories, files = _layout(source, state_directory, preserve_entries)
    conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        conn.execute('BEGIN')
        mirror._check_source(conn)
        sha = mirror.digest(mirror._read(conn, source.parent))
        count = _guards(conn)
        journal = conn.execute('PRAGMA journal_mode').fetchone()[0]
    finally:
        conn.close()
    state = state_directory / STATE
    state_sha = hashlib.sha256(state.read_bytes()).hexdigest() if state.exists() else None
    writable = any(path.stat().st_mode & 0o222 for path in [*directories, *files])
    temporary_sqlite = any(source.with_name(source.name + suffix).exists()
                           for suffix in ('-wal', '-shm', '-journal'))
    return dict(source=str(source), state_directory=str(state_directory), source_sha256=sha,
                preserved_entries=sorted(str(Path(os.path.abspath(entry))) for entry in preserve_entries),
                state_sha256=state_sha, trigger_count=count,
                fenced=count == len(_triggers()) and journal == 'delete' and not writable and not temporary_sqlite)


def inspect(source: Path, state_directory: Path, *, preserve_entries: tuple[Path, ...] = ()) -> dict:
    """Read seal status. Explicit top-level archives are inventoried by name, never read or changed.

    Preserving an entry does not attest its contents or seal writers inside it.
    Unlisted extra entries still reject the dedicated directory.
    """
    try:
        return _snapshot(source, state_directory, preserve_entries)
    except (OSError, sqlite3.Error, mirror.MirrorError):
        raise RetirementError('无法读取旧库封锁状态；请检查目录、数据库及旧写入进程') from None


def verify(source: Path, state_directory: Path, expected_sha256: str, *, preserve_entries: tuple[Path, ...] = ()) -> dict:
    """Prove persistent guards, read-only files/directories and final business hash."""
    result = inspect(source, state_directory, preserve_entries=preserve_entries)
    if result['source_sha256'] != expected_sha256:
        raise RetirementError('旧库内容已变化，须重新完成末次镜像和准备')
    if not result['fenced']:
        raise RetirementError('旧库尚未完整封锁；不得绑定新账本')
    return result


def fence(source: Path, state_directory: Path, expected_sha256: str, *, preserve_entries: tuple[Path, ...] = ()) -> dict:
    """Seal a dedicated, drained source. Retry completes partial seals without reopening permissions.

    A checkpoint or permission failure may leave triggers installed. Keep old
    writers stopped and retry; no failure path removes the already installed seal.
    """
    before = inspect(source, state_directory, preserve_entries=preserve_entries)
    if before['source_sha256'] != expected_sha256:
        raise RetirementError('旧库内容已变化，须重新完成末次镜像和准备')
    if before['fenced']:
        return before
    source = Path(before['source'])
    state_directory = Path(before['state_directory'])
    try:
        conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            complete = _guards(conn) == len(_triggers()) and conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        finally:
            conn.close()
        if not complete:
            conn = sqlite3.connect(source.as_uri() + '?mode=rw', uri=True, timeout=1, isolation_level=None)
            try:
                conn.execute('BEGIN EXCLUSIVE')
                mirror._check_source(conn)
                if mirror.digest(mirror._read(conn, source.parent)) != expected_sha256:
                    raise RetirementError('旧库内容已变化，须重新完成末次镜像和准备')
                _guards(conn)
                existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                for name, sql in _triggers().items():
                    if name not in existing:
                        conn.execute(sql)
                conn.commit()
                if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                    raise RetirementError('旧库仍有活动连接，封锁触发器已保留；请排空后重试')
                if conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise RetirementError('旧库日志模式未能封锁，触发器已保留')
            finally:
                conn.close()
        _, _, directories, files = _layout(source, state_directory, preserve_entries)
        for path in files:
            path.chmod(0o400)
        for path in directories:
            path.chmod(0o500)
        return verify(source, state_directory, expected_sha256, preserve_entries=preserve_entries)
    except (OSError, sqlite3.Error, mirror.MirrorError):
        raise RetirementError('旧库封锁未完成；已有封锁保持不变，请排空连接并检查权限后重试') from None
