"""Verified SQLite/PDF/account-evidence bundles; restore only into a new private directory.

This stages recovery data, never replaces a live database or rewinds the account ledger.
Original bytes are copied, not hard-linked; disposable PDF extraction caches are excluded.
"""
import argparse
from contextlib import closing
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from a_share_research import backup_refs, db, integrity
from a_share_research.account import evidence

log = logging.getLogger(__name__)
Sha = Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
Entry = Annotated[str, StringConstraints(pattern=r'^(app\.db|pdf/[0-9a-f]{64}\.pdf|account-evidence/[0-9a-f]{64})$')]


class BackupError(ValueError):
    pass


def _operation(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BackupError:
            raise
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise BackupError(str(exc)) from exc
    return checked


class FileDigest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sha256: Sha
    size: Annotated[StrictInt, Field(ge=0)]


class Required(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pdf: list[Sha]
    account_evidence: list[Sha]


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['ashare-backup-v1']
    created_at: str
    user_version: Annotated[StrictInt, Field(ge=0)]
    sqlite_sequences: dict[str, Annotated[StrictInt, Field(ge=0)]]
    required: Required
    files: dict[Entry, FileDigest]


def _safe(path: Path) -> Path:
    path = Path(path).absolute()
    if '..' in path.parts:
        raise BackupError('备份路径不能含上级目录片段')
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise BackupError('备份路径不能经过符号链接')
    return path


def _regular(path):
    handle = os.open(_safe(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(handle).st_mode):
        os.close(handle)
        raise BackupError('备份对象必须是普通文件')
    return os.fdopen(handle, 'rb')


def _copy(source, target=None):
    """Stream verified bytes; never follow links or overwrite a destination."""
    digest, size = hashlib.sha256(), 0
    with _regular(source) as stream:
        output = None
        try:
            if target is not None:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                output = os.fdopen(fd, 'wb')
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                if output:
                    output.write(chunk)
            if output:
                output.flush()
                os.fsync(output.fileno())
        finally:
            if output:
                output.close()
    return dict(sha256=digest.hexdigest(), size=size)


def _metadata(database):
    with _regular(database) as stream:
        if stream.read(20)[18:20] != b'\x01\x01':
            raise BackupError('备份数据库必须为独立回滚日志格式，不能依赖 WAL 文件')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, isolation_level=None)) as conn:
        conn.execute('BEGIN')
        sequences = dict(conn.execute('SELECT name,seq FROM sqlite_sequence')) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else {}
        return dict(user_version=conn.execute('PRAGMA user_version').fetchone()[0], sqlite_sequences=sequences,
                    required=backup_refs.required(conn))


def _sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new(directory):
    directory = _safe(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    return directory


def _write_manifest(directory, manifest):
    temporary = directory / '.manifest-pending'
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / 'manifest.json')
    _sync_directory(directory)
    _sync_directory(directory.parent)


@_operation
def create(database: Path, destination: Path, *, pdf_directory: Path | None = None,
           evidence_directory: Path | None = None) -> dict:
    """Capture a database snapshot, then all stored immutable evidence; reject missing committed references."""
    database = _safe(database)
    # Fail before creating the destination if the source is absent or not a regular file.
    with _regular(database):
        pass
    pdf_directory = _safe(pdf_directory or database.parent/'pdf')
    evidence_directory = _safe(evidence_directory or database.parent/'account-evidence')
    target = _new(destination)
    try:
        snapshot = target/'app.db'
        fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        deadline = time.monotonic()+300
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise BackupError('SQLite 快照超过五分钟；源库未改，请检查锁或磁盘后重试')
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(snapshot)) as copy:
            source.execute('BEGIN')
            source.execute('SELECT count(*) FROM sqlite_master').fetchone()  # Pin one read snapshot, including WAL commits.
            source.backup(copy, pages=1024, progress=progress, sleep=.05)
            copy.execute('PRAGMA journal_mode=DELETE')
        files = {'app.db': _copy(snapshot)}
        metadata = _metadata(snapshot)
        for name, source_dir in (('pdf',pdf_directory),('account-evidence',evidence_directory)):
            output_dir = target/name
            output_dir.mkdir(mode=0o700)
            if source_dir.exists():
                for path in sorted(source_dir.iterdir()):
                    if name == 'pdf' and path.suffix != '.pdf':
                        continue  # Text extraction caches are reproducible.
                    if name == 'account-evidence' and path.name.startswith('.pending-'):
                        continue  # Unpublished upload; committed references are checked below.
                    sha = path.stem if name == 'pdf' else path.name
                    if re.fullmatch('[0-9a-f]{64}',sha) is None:
                        raise BackupError(f'{name} 包含非法证据文件名')
                    if name == 'account-evidence':
                        evidence.verify(source_dir,sha)
                    copied = _copy(path,output_dir/path.name)
                    if copied['sha256'] != sha:
                        raise BackupError(f'{name} 文件与内容哈希不一致')
                    files[f'{name}/{path.name}'] = copied
            _sync_directory(output_dir)
        manifest = Manifest(format='ashare-backup-v1',created_at=db.now(),files=files,**metadata).model_dump(mode='json')
        _write_manifest(target,manifest)
        verify(target)
        log.info('数据库与原始证据备份完成：%s',target)
        return manifest
    except BaseException:
        shutil.rmtree(target)  # Only the exclusive directory created by this call.
        raise


def _read_manifest(directory):
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise BackupError('备份清单包含重复键')
            result[key] = value
        return result
    with _regular(directory/'manifest.json') as stream:
        try:
            manifest = Manifest.model_validate(json.loads(stream.read(),object_pairs_hook=unique))
        except ValueError as exc:
            raise BackupError('备份清单格式无效') from exc
    if 'app.db' not in manifest.files:
        raise BackupError('备份清单缺少数据库')
    return manifest.model_dump(mode='json')


@_operation
def verify(directory: Path) -> dict:
    """Verify exact inventory, every content digest, required evidence, SQLite and domain integrity without writes."""
    directory = _safe(directory)
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise BackupError('备份目录须为0700')
    manifest = _read_manifest(directory)
    inventory = set()
    for path in directory.rglob('*'):
        _safe(path)
        name = path.relative_to(directory).as_posix()
        if path.is_dir():
            if name not in ('pdf','account-evidence') or stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise BackupError('备份包含未知目录或非私有目录')
            continue
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise BackupError('备份文件须为0600')
        if name != 'manifest.json':
            inventory.add(name)
    if inventory != set(manifest['files']):
        raise BackupError('备份文件清单不完整或有额外文件')
    for name,expected in manifest['files'].items():
        actual = _copy(directory/name)
        if actual != expected:
            raise BackupError(f'备份文件哈希或长度不符：{name}')
        if name.startswith('pdf/') and actual['sha256'] != Path(name).stem:
            raise BackupError('PDF 文件名与内容哈希不符')
        if name.startswith('account-evidence/'):
            evidence.verify(directory/'account-evidence',Path(name).name)
    metadata = _metadata(directory/'app.db')
    if any(manifest[key] != value for key,value in metadata.items()):
        raise BackupError('数据库版本、序列或证据引用与清单不一致')
    for kind,subdir,suffix in (('pdf','pdf','.pdf'),('account_evidence','account-evidence','')):
        missing = [sha for sha in metadata['required'][kind] if f'{subdir}/{sha}{suffix}' not in inventory]
        if missing:
            raise BackupError(f'备份缺少 {len(missing)} 份已引用的 {kind} 原始证据')
    problems,counts = integrity.check(directory/'app.db',directory/'pdf')
    if problems:
        raise BackupError(f'备份完整性检查失败：{problems}')
    return dict(ok=True,manifest=manifest,counts=counts)


@_operation
def restore(bundle: Path, destination: Path) -> dict:
    """Stage an exact verified recovery copy; never migrate, merge or overwrite a live data directory."""
    bundle = _safe(bundle)
    checked = verify(bundle)
    target = _new(destination)
    try:
        for name in ('pdf','account-evidence'):
            (target/name).mkdir(mode=0o700)
        for name,expected in checked['manifest']['files'].items():
            if _copy(bundle/name,target/name) != expected:
                raise BackupError('复制期间备份内容变化，恢复中止')
        for name in ('pdf','account-evidence'):
            _sync_directory(target/name)
        _write_manifest(target,checked['manifest'])
        result = verify(target)
        log.info('已恢复到独立目录，尚未切换任何服务：%s',target)
        return result
    except BaseException:
        shutil.rmtree(target)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action',required=True)
    create_args = actions.add_parser('create')
    create_args.add_argument('database',type=Path)
    create_args.add_argument('destination',type=Path)
    create_args.add_argument('--pdf-directory',type=Path)
    create_args.add_argument('--evidence-directory',type=Path)
    verify_args = actions.add_parser('verify')
    verify_args.add_argument('bundle',type=Path)
    restore_args = actions.add_parser('restore')
    restore_args.add_argument('bundle',type=Path)
    restore_args.add_argument('destination',type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    try:
        if args.action == 'create':
            result = create(args.database,args.destination,pdf_directory=args.pdf_directory,evidence_directory=args.evidence_directory)
            log.info('清单记录 %d 个原始文件',len(result['files']))
        elif args.action == 'verify':
            verify(args.bundle)
            log.info('备份校验通过')
        else:
            restore(args.bundle,args.destination)
    except (ValueError,OSError,sqlite3.Error) as exc:
        parser.exit(1,f'备份操作失败：{exc}\n')


if __name__ == '__main__':
    main()
