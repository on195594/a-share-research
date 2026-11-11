"""Owner-local account cutover: prepare, persist the old write fence, then bind once.

Operational exit gates and stopping/draining old hosts precede these commands.
Failures never reopen the old writer, start services or change investment policy.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from a_share_research import api_keys, backup, db, gate
from a_share_research.account import retirement
from a_share_research.policy import account_input, cutover as readiness, identity, registry, stop_state
from a_share_research.policy.evaluation import validate_parameters


class CutoverError(ValueError):
    pass


SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class FileIdentity(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    device: int
    inode: int


class Plan(BaseModel):
    """Private, explicit preparation receipt; not proof of business exit gates."""
    model_config = ConfigDict(strict=True, extra='forbid', frozen=True)
    schema_version: int = Field(ge=1, le=1)
    source: str
    state_directory: str
    target: str
    source_file: FileIdentity
    target_file: FileIdentity
    preserved_entries: tuple[str, ...]
    account_id: str
    account_scope: str
    mirror_row: tuple[str, str, str, str]
    state_sha256: SHA | None
    opening_sha256: SHA
    activation_id: int = Field(gt=0)
    policy_identity: str

    @field_validator('source', 'state_directory', 'target')
    @classmethod
    def absolute_path(cls, value):
        if not Path(value).is_absolute() or str(Path(value)) != value or '..' in Path(value).parts:
            raise ValueError('切换路径须为规范绝对路径')
        return value

    @field_validator('account_id', 'account_scope')
    @classmethod
    def text(cls, value):
        return registry._text(value, '账户标识/范围')

    @field_validator('preserved_entries')
    @classmethod
    def preserved_paths(cls, values):
        for value in values:
            cls.absolute_path(value)
        return values


def _file(path: Path) -> FileIdentity:
    # Closing another fd for a connected SQLite file releases this process's
    # POSIX locks. Inspect metadata without opening the database a second time.
    info = backup._safe(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise CutoverError('切换数据库须属于当前用户且为无硬链接的普通文件')
    return FileIdentity(device=info.st_dev, inode=info.st_ino)


def _target(conn) -> Path:
    row = next((row for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), None)
    if row is None or not row[2]:
        raise CutoverError('切换要求已存在的本地数据库')
    return backup._safe(Path(row[2]))


def _paths(conn, source, state_directory):
    source, state_directory = backup._safe(source), backup._safe(state_directory)
    target = _target(conn)
    if any(target.is_relative_to(directory) for directory in (source.parent, state_directory)):
        raise CutoverError('新库不能位于即将封锁的旧库或状态目录内')
    if _file(source) == _file(target):
        raise CutoverError('新旧库不能指向同一文件')
    return source, state_directory, target


def _row(conn):
    row = conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source WHERE id=1').fetchone()
    if row is None:
        raise CutoverError('须先完成末次镜像')
    return tuple(row)


def _policy(conn, scope):
    activation = registry._current(conn)
    if activation is None:
        raise CutoverError('须先由 owner 启用已确认的账户政策')
    manifest = registry._version(conn, activation['identity'])
    identity.check_installed(manifest)
    parameters = manifest['parameters']
    validate_parameters(parameters)
    if parameters.get('account_scope') != scope or parameters['source'] == 'not_applied_account_scope_required':
        raise CutoverError('生效政策的账户范围与切换范围不同或未确认')
    now = datetime.fromisoformat(db.now())
    if (datetime.fromisoformat(parameters['effective_from']) > now
            or datetime.fromisoformat(parameters['confirmed_at']) > now
            or parameters.get('effective_to') is not None and now > datetime.fromisoformat(parameters['effective_to'])):
        raise CutoverError('账户政策尚未生效或已经到期')
    return activation


def prepare(conn, principal, source: Path, state_directory: Path, account_id: str, account_scope: str,
            *, preserve_entries: tuple[Path, ...] = ()) -> Plan:
    """Read-only readiness and exact state capture; never mirror, migrate or fence."""
    with db.transaction(conn, immediate=False):
        registry._authorize(conn, principal, owner=True)
        source, state_directory, target = _paths(conn, source, state_directory)
        old = retirement.inspect(source, state_directory, preserve_entries=preserve_entries)
        row = _row(conn)
        if old['source_sha256'] != row[1]:
            raise CutoverError('旧库与末次镜像不一致，须重新镜像并核对期初')
        verified = readiness.validate(conn, target.parent, account_id, account_scope, row)
        activation = _policy(conn, account_scope)
        return Plan(schema_version=1, source=str(source), state_directory=str(state_directory), target=str(target),
                    source_file=_file(source), target_file=_file(target), preserved_entries=tuple(old['preserved_entries']),
                    account_id=account_id,
                    account_scope=account_scope, mirror_row=row, state_sha256=old['state_sha256'],
                    opening_sha256=verified['state_sha256'], activation_id=activation['id'],
                    policy_identity=activation['identity'])


def _locations(conn, plan):
    source, state_directory, target = _paths(conn, Path(plan.source), Path(plan.state_directory))
    if (str(target) != plan.target or _file(source) != plan.source_file or _file(target) != plan.target_file):
        raise CutoverError('准备后数据库位置或文件身份发生变化，须重新准备')
    return source, state_directory, target


def _ready(conn, plan, target):
    if _row(conn) != plan.mirror_row:
        raise CutoverError('末次镜像指针已变化，须重新准备')
    activation = _policy(conn, plan.account_scope)
    if (activation['id'], activation['identity']) != (plan.activation_id, plan.policy_identity):
        raise CutoverError('准备后生效政策指针已变化')
    verified = readiness.validate(conn, target.parent, plan.account_id, plan.account_scope, plan.mirror_row)
    if verified['state_sha256'] != plan.opening_sha256:
        raise CutoverError('准备后账户或分析状态已变化，须重新准备')
    return verified


def _state(receipt, plan):
    if receipt['state_sha256'] != plan.state_sha256:
        raise CutoverError('准备后旧提醒状态已变化，须重新准备')


def _binding(plan):
    return dict(schema_version=1, account_id=plan.account_id, account_scope=plan.account_scope,
                mirror_watermark=account_input._json(plan.mirror_row[3]))


def _matches_binding(row, plan):
    return row[0] == 'ledger' and row[1] == plan.mirror_row[1] and row[3] == gate.canonical(_binding(plan))


def fence(conn, principal, plan: Plan) -> dict:
    """Recheck the plan before sealing old files; any partial fence stays in place."""
    with db.transaction(conn):
        registry._authorize(conn, principal, owner=True)
        source, state_directory, target = _locations(conn, plan)
        _ready(conn, plan, target)
        preserved = tuple(Path(path) for path in plan.preserved_entries)
        _state(retirement.inspect(source, state_directory, preserve_entries=preserved), plan)
        receipt = retirement.fence(source, state_directory, plan.mirror_row[1], preserve_entries=preserved)
        _state(receipt, plan)
        return receipt


def commit(conn, principal, plan: Plan) -> dict:
    """One atomic source binding; matching retries preserve all later journal writes."""
    with db.transaction(conn):
        registry._authorize(conn, principal, owner=True)
        source, state_directory, target = _locations(conn, plan)
        _state(retirement.verify(source, state_directory, plan.mirror_row[1],
                                preserve_entries=tuple(Path(path) for path in plan.preserved_entries)), plan)
        binding = _binding(plan)
        row = _row(conn)
        if row[0] == 'ledger':
            if not _matches_binding(row, plan):
                raise CutoverError('已有不同账户绑定，不能替换')
            return dict(status='already_bound', source_sha256=row[1], updated_at=row[2], binding=binding)
        verified = _ready(conn, plan, target)
        stamp = db.now()
        changed = conn.execute('UPDATE account_source SET mode=\'ledger\',updated_at=?,watermark=? '
            'WHERE id=1 AND mode=? AND source_sha256=? AND updated_at=? AND watermark=?',
            (stamp, gate.canonical(verified['watermark']), *plan.mirror_row)).rowcount
        if changed != 1:
            raise CutoverError('镜像指针比较更新失败')
        return dict(status='bound', source_sha256=row[1], updated_at=stamp, binding=verified['watermark'])


def transfer_stop_state(conn, principal, plan: Plan, destination: Path) -> dict:
    """Import sealed legacy dedup state before enabling Hermes; never reset an existing client.

    The plan's null hash proves a checked absence at the explicit old state path.
    Only then is an empty state initialized. Advisor/monthly/scoring stay in place.
    """
    with db.transaction(conn):
        registry._authorize(conn, principal, owner=True)
        source, state_directory, target = _locations(conn, plan)
        _state(retirement.verify(source, state_directory, plan.mirror_row[1],
                                preserve_entries=tuple(Path(path) for path in plan.preserved_entries)), plan)
        if not _matches_binding(_row(conn), plan):
            raise CutoverError('须先完成相同账户的正式来源绑定，再交接通知状态')
        destination = backup._safe(destination)
        if (destination == target or any(destination.is_relative_to(folder)
                                        for folder in (source.parent, state_directory))):
            raise CutoverError('新通知状态须位于旧封锁目录之外，不能覆盖数据库')
        old_state = state_directory / retirement.STATE
        data = old_state.read_bytes() if old_state.exists() else None
        if (hashlib.sha256(data).hexdigest() if data is not None else None) != plan.state_sha256:
            raise CutoverError('旧通知状态与准备哈希不同，未导入')
        content = data if data is not None else b'{}\n'
        try:
            stop_state.validate_state(account_input._json(content.decode('utf-8')))
        except (ValueError, TypeError, UnicodeError):
            raise CutoverError('旧通知状态格式无效，未导入') from None
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = destination.parent.stat()
        if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
            raise CutoverError('新通知状态目录须为当前属主私有目录')
        lock_path = backup._safe(destination.with_suffix('.lock'))
        if lock_path == destination:
            raise CutoverError('通知状态文件不能与客户端锁文件同名')
        if lock_path.exists():
            info = lock_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise CutoverError('客户端锁须为私有普通文件')
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(descriptor, 'r+b') as lock:
            info = os.fstat(lock.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise CutoverError('客户端锁须为私有普通文件')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CutoverError('新止损客户端仍在运行，须先排空') from None
            if destination.exists():
                info = destination.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or info.st_mode & 0o077):
                    raise CutoverError('现有通知状态须为私有普通文件')
                if destination.read_bytes() != content:
                    raise CutoverError('新通知状态已存在且不同；保持现有历史，不回退或清除待查请求')
                status = 'already_imported'
            else:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Atomic create-if-absent; even a non-cooperating writer is not overwritten.
                    os.link(temporary, destination, follow_symlinks=False)
                    status = 'imported'
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            return dict(status=status, destination=str(destination), source_absent=data is None,
                        source_sha256=plan.state_sha256, state_sha256=hashlib.sha256(content).hexdigest())


def _save(plan, path):
    path = backup._safe(path)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CutoverError('切换计划须保存在仅属主可访问的私有目录')
    if any(path.is_relative_to(Path(folder)) for folder in (str(Path(plan.source).parent), plan.state_directory)):
        raise CutoverError('切换计划不能保存在将被封锁的目录内')
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as stream:
        stream.write(plan.model_dump_json(indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True, help='existing target database; never migrates')
    parser.add_argument('--plan', type=Path, required=True, help='private preparation receipt outside old directories')
    actions = parser.add_subparsers(dest='action', required=True)
    preparation = actions.add_parser('prepare')
    preparation.add_argument('--source', type=Path, required=True)
    preparation.add_argument('--state-directory', type=Path, required=True, help='actual old stop-alert state directory')
    preparation.add_argument('--account-id', required=True)
    preparation.add_argument('--account-scope', required=True)
    preparation.add_argument('--preserve-entry', type=Path, action='append', default=[],
                             help='explicit adjacent archive/log/runtime entry to leave untouched; repeat per entry')
    actions.add_parser('fence')
    actions.add_parser('commit')
    transfer = actions.add_parser('transfer-stop-state', help='after binding, before enabling new Hermes tasks')
    transfer.add_argument('--destination', type=Path, required=True, help='new client state.json; existing history is never reset')
    args = parser.parse_args()
    conn = None
    try:
        path = backup._safe(args.database)
        _file(path)
        conn = sqlite3.connect(path.as_uri() + '?mode=' + ('ro' if args.action == 'prepare' else 'rw'),
                               uri=True, isolation_level=None, timeout=30)
        conn.execute('PRAGMA foreign_keys=ON')
        principal = api_keys.authenticate(conn, os.environ.get('ASHARE_OWNER_TOKEN', ''))
        if principal is None:
            raise CutoverError('请提供有效的 ASHARE_OWNER_TOKEN')
        if args.action == 'prepare':
            plan = prepare(conn, principal, args.source, args.state_directory, args.account_id, args.account_scope,
                           preserve_entries=tuple(args.preserve_entry))
            _save(plan, args.plan)
            result = dict(status='prepared', plan=str(backup._safe(args.plan)))
        else:
            plan_path = backup._safe(args.plan)
            if plan_path.samefile(path):
                raise CutoverError('数据库不能作为切换计划文件')
            with backup._regular(plan_path) as stream:
                info = os.fstat(stream.fileno())
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise CutoverError('切换计划须为属主私有文件')
                plan = Plan.model_validate_json(stream.read())
            if args.action == 'transfer-stop-state':
                result = transfer_stop_state(conn, principal, plan, args.destination)
            else:
                result = (fence if args.action == 'fence' else commit)(conn, principal, plan)
        print(gate.canonical(result))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'{type(exc).__name__}: {exc}\n')
    finally:
        if conn is not None:
            conn.close()


if __name__ == '__main__':
    main()
