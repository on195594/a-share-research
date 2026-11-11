"""First v9→v21 release checks; invoked only by deploy's explicit initial-recovery path.

Migrate a complete private copy before stopping production, then check the stopped
database again with the same image. Recovery never restores an earlier database.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import TypedDict
import uuid

from a_share_research import backup, integrity, rollback


class InitialReleaseError(RuntimeError):
    pass


class TableState(TypedDict):
    count: int
    sha256: str


class DatabaseState(TypedDict):
    version: int
    columns: dict[str, list[str]]
    tables: dict[str, TableState]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def state(database: Path, columns: dict[str, list[str]] | None = None) -> DatabaseState:
    """Hash all selected values, including generated columns, in one read transaction."""
    with closing(sqlite3.connect(database.absolute().as_uri()+'?mode=ro', uri=True)) as conn:
        conn.execute('BEGIN')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if columns is None:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            columns = {table: [r[1] for r in conn.execute('PRAGMA table_xinfo('+_quote(table)+')')]
                       for table in tables}
        values = {}
        for table, names in columns.items():
            hashes = []
            for row in conn.execute('SELECT '+','.join(map(_quote, names))+' FROM '+_quote(table)):
                data = [{'blob': v.hex()} if isinstance(v, bytes) else v for v in row]
                hashes.append(hashlib.sha256(json.dumps(data, ensure_ascii=False, allow_nan=False,
                                                        separators=(',', ':')).encode()).hexdigest())
            values[table] = dict(count=len(hashes), sha256=hashlib.sha256('\n'.join(sorted(hashes)).encode()).hexdigest())
    return dict(version=version, columns=columns, tables=values)


def _save(directory: Path, name: str, value: dict) -> None:
    with (directory/name).open('x') as output:
        os.chmod(output.name, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')


def _run_migration(image: str, database: Path, evidence: Path) -> None:
    """Run only the pinned image's migration, without network, model keys or a worker loop."""
    name = 'ashare-initial-migration-'+uuid.uuid4().hex
    script = ('import json; from a_share_research import db; '
              'conn=db.connect(); print(json.dumps({"schema_version":conn.execute("PRAGMA user_version").fetchone()[0]})); '
              'conn.close()')
    command = ['docker', 'run', '--rm', '--pull', 'never', '--name', name, '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--cpus', '1.2', '--memory', '512m', '--pids-limit', '64',
               '--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,size=32m,mode=1777',
               '--mount', f'type=bind,src={database.absolute().parent},dst=/data',
               '--env', 'DATA_DIR=/data', '--entrypoint', 'python', image, '-c', script]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=120)
        _save(evidence, 'migration-process.json', dict(image=image, exit_code=process.returncode,
                                                     stdout=process.stdout, stderr=process.stderr))
        if process.returncode or json.loads(process.stdout) != {'schema_version': 21}:
            raise InitialReleaseError('目标镜像未完成 v21 迁移；保留副本与诊断，不恢复旧库')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise InitialReleaseError('目标镜像迁移未获有效回执；保留现场核查') from exc
    finally:
        try:
            cleanup = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=15)
            if cleanup.returncode and 'No such container' not in cleanup.stderr:
                raise InitialReleaseError('迁移容器未确认清理；不得启动服务')
        except (OSError, subprocess.SubprocessError) as exc:
            raise InitialReleaseError('迁移容器未确认清理；不得启动服务') from exc


def migrate(database: Path, image: str, evidence: Path) -> dict:
    """Require v9, migrate with the target image, and prove old-column conservation.

All writers must be stopped. Failure may leave a partially migrated database;
the caller must keep admission closed rather than restart the old application.
"""
    if database.name != 'app.db':
        raise InitialReleaseError('首次迁移只接受部署目录中的 app.db')
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    before = state(database)
    _save(evidence, 'before.json', before)
    if before['version'] != 9:
        raise InitialReleaseError('首次迁移入口只接受 v9；其他版本须使用对应发布或恢复流程')
    _run_migration(image, database, evidence)
    after = state(database, before['columns'])
    _save(evidence, 'after.json', after)
    if after['version'] != 21 or before['tables'] != after['tables']:
        raise InitialReleaseError('迁移版本或旧列守恒核对失败；保持停服，不自动恢复受理')
    with closing(sqlite3.connect(database.absolute().as_uri()+'?mode=ro', uri=True)) as conn:
        conn.execute('BEGIN')
        if (conn.execute("SELECT 1 FROM api_keys WHERE role!='research' LIMIT 1").fetchone()
                or conn.execute('PRAGMA foreign_key_check').fetchone()):
            raise InitialReleaseError('迁移后的旧 Key 权限或外键检查失败')
    problems, _ = integrity.check(database, database.parent/'pdf')
    if problems:
        raise InitialReleaseError('迁移后的领域完整性检查失败；保持停服')
    result = dict(source_version=9, target_version=21, image=image,
                  old_columns_preserved=True, integrity_passed=True)
    _save(evidence, 'migration.json', result)
    return result


def prepare(database: Path, target_images: dict[str, str], recovery_images: dict[str, str], evidence: Path) -> dict:
    """Check a consistent full copy and both recovery images before any service is stopped."""
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup.create(database, evidence/'original')
    backup.restore(evidence/'original', evidence/'data')
    migration = migrate(evidence/'data/app.db', target_images['worker'], evidence/'migration')
    compatibility = rollback.assess(recovery_images, evidence/'data/app.db', evidence/'recovery-probe')
    _save(evidence, 'recovery-compatibility.json', compatibility)
    if compatibility['ok'] is not True:
        raise InitialReleaseError('首次迁移恢复镜像未通过真实副本兼容检查；生产未停服')
    result = dict(migration=migration, recovery_images=recovery_images, compatibility=compatibility)
    _save(evidence, 'prepared.json', result)
    return result
