"""Switch prebuilt local images after explicit production authorization.

This is the existing release procedure, without tests, builds or browser control.
"""

import argparse
import fcntl
import functools
import stat
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, ProxyHandler

from a_share_research import backup as recovery, db, initial_release, rollback

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("web", "worker")
TABLES = ("runs", "calls", "reports", "prices", "batches", "sessions", "api_keys")
OPTIONAL_TABLES = (*db.ACCOUNT_TABLES, 'account_source', 'policy_activations',
                   'external_research', 'external_submissions', 'external_reviews', 'external_operations', 'external_materials',
                   'account_books', 'account_batches', 'account_journal', 'account_voids', 'account_observations',
                   'account_analysis_history', 'account_position_books',
                   'account_projection_ids', 'policy_v0_profiles', 'policy_v0_account_openings',
                   'policy_versions', 'policy_evaluations')
log = logging.getLogger(__name__)


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def idle(database: Path) -> None:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        active = conn.execute("SELECT id FROM runs WHERE status IN ('queued','running')").fetchall()
        busy = conn.execute("SELECT id FROM batches WHERE status NOT IN ('failed','done','closed')"
                            " AND (status != 'tracking' OR step IS NOT NULL)").fetchall()
        scans = conn.execute("SELECT id FROM scans WHERE status IN ('queued','running')").fetchall()
        operations = (conn.execute("SELECT id FROM external_operations WHERE status IN ('queued','running')").fetchall()
                      if conn.execute("SELECT 1 FROM sqlite_master WHERE name='external_operations'").fetchone() else [])
    if active or busy or scans or operations:
        raise RuntimeError(f"发布中止：研究任务 {active}、批次 {busy}、扫描 {scans} 或取证 {operations} 尚未空闲")


KEEP_BACKUPS = 3


def prune_backups(data: Path, keep: int = KEEP_BACKUPS) -> list[str]:
    """Keep the newest release backups (owner's standing rule, 10-31); never touches app.db."""
    backups = sorted(data.glob("backup-pre-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    removed = []
    for old in backups[keep:]:
        bundle = old.with_suffix('.bundle')
        if bundle.exists() or bundle.is_symlink():
            try:
                checked = recovery.verify(bundle)
                if recovery._copy(old) != checked['manifest']['files']['app.db']:
                    raise recovery.BackupError('独立数据库与备份包不匹配')
            except (recovery.BackupError, OSError) as exc:
                log.warning('保留无法核对归属的发布备份 %s：%s',old,exc)
                continue
            shutil.rmtree(bundle)
        for path in (old, old.with_name(old.name + "-shm"), old.with_name(old.name + "-wal")):
            path.unlink(missing_ok=True)
        removed.append(old.name)
    return removed


def create_release_backup(database: Path, destination: Path) -> dict:
    """Keep the existing .db recovery entry alongside its verified raw-evidence bundle."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    manifest = recovery.create(database,destination.with_suffix('.bundle'))
    # A standalone copy preserves the documented .db entry and cannot mutate bundle bytes.
    if recovery._copy(destination.with_suffix('.bundle')/'app.db',destination) != manifest['files']['app.db']:
        raise recovery.BackupError('发布备份数据库复制不一致')
    recovery._sync_directory(destination.parent)
    return dict(bundle=str(destination.with_suffix('.bundle')),files=len(manifest['files']),
                pdfs=sum(name.startswith('pdf/') for name in manifest['files']),
                account_evidence=sum(name.startswith('account-evidence/') for name in manifest['files']))


KEEP_ROLLBACK_IMAGES = 2


def _created(value: str) -> datetime:
    """Docker's RFC 3339 creation time; nanoseconds are cut to what datetime holds."""
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))


def images_to_prune(images: dict[str, dict], current: str, keep: int = KEEP_ROLLBACK_IMAGES) -> list[str]:
    """Tags of one repository's images older than the current image and its newest `keep` rollback targets.

    `images` maps image id to {"created", "tags"}. A rollback target is an image carrying a
    `rollback-*` tag. Images newer than the oldest kept one (built but not released) are never chosen.
    """
    created = {image: _created(info["created"]) for image, info in images.items()}
    rollbacks = sorted((image for image, info in images.items()
                        if image != current and any(tag.startswith("rollback-") for tag in info["tags"])),
                       key=created.__getitem__, reverse=True)[:keep]
    kept = {current, *rollbacks}
    cutoff = min(created[image] for image in kept)
    return sorted(tag for image, info in images.items() if image not in kept and created[image] < cutoff
                  for tag in info["tags"] if tag != "<none>")


def prune_images(current: dict[str, str], keep: int = KEEP_ROLLBACK_IMAGES) -> dict:
    """Untag old release images after a verified release (owner's standing rule, 11-08).

    Docker deletes an image once its last tag is gone and no container uses it. A tag Docker
    refuses to remove is reported, not retried: the release itself has already been verified.
    """
    result = {}
    for service in SERVICES:
        repository = f"a-share-research-{service}"
        removed, failed = [], {}
        try:
            images = {}
            for line in run("docker", "image", "ls", "--no-trunc", "--format", "{{.ID}} {{.Tag}}", repository).splitlines():
                image, tag = line.split()
                images.setdefault(image, {"tags": []})["tags"].append(tag)
            for image in images:
                images[image]["created"] = run("docker", "image", "inspect", "--format", "{{.Created}}", image)
            tags = images_to_prune(images, current[service], keep)
        except subprocess.CalledProcessError as exc:
            log.warning("%s 镜像列表读取失败，本次不清理：%s", repository, exc)
            result[service] = {"removed": [], "failed": {"list": str(exc)}}
            continue
        for tag in tags:
            try:
                run("docker", "image", "rm", f"{repository}:{tag}")
                removed.append(tag)
            except subprocess.CalledProcessError as exc:
                log.warning("%s:%s 未能删除：%s", repository, tag, exc)
                failed[tag] = str(exc)
        result[service] = {"removed": removed, "failed": failed}
    return result


def fingerprints(database: Path) -> dict:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = (*TABLES, *(name for name in OPTIONAL_TABLES if name in existing))
        return {table: hashlib.sha256(json.dumps(conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(),
                                                 ensure_ascii=False).encode()).hexdigest() for table in tables}


def _source_version(database: Path) -> int:
    with closing(sqlite3.connect(database.absolute().as_uri()+'?mode=ro', uri=True)) as conn:
        return conn.execute('PRAGMA user_version').fetchone()[0]


# Injected, bounded stdin protocol uses only APIs already present in policy-capable
# images. It cannot import a new release helper from the host into an old image.
POLICY_PROBE = '''
import json, sqlite3, sys
from pathlib import Path
from datetime import datetime
from a_share_research import db, api_keys
p = json.load(sys.stdin)
if p['action'] == 'candidate':
    from a_share_research.policy import identity
    print(json.dumps(identity.build(p['parameters'])))
else:
    database = Path('/data/app.db')
    conn = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('BEGIN')
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {'policy_activations', 'policy_versions'}
    if not required & tables:
        print(json.dumps(dict(applicable=False)))
    else:
        if not required <= tables: raise ValueError('incomplete policy schema')
        from a_share_research.policy import registry
        from a_share_research.policy.evaluation import validate_parameters
        state = registry._read_state(conn)
        manifest = state['manifest']
        if manifest is None:
            print(json.dumps(dict(applicable=False)))
            sys.exit(0)
        parameters = manifest['parameters']
        validate_parameters(parameters)
        now = datetime.fromisoformat(db.now())
        valid = parameters.get('source') != 'not_applied_account_scope_required'
        for key in ('effective_from', 'confirmed_at'):
            if key in parameters and datetime.fromisoformat(parameters[key]) > now: valid = False
        if parameters.get('effective_to') is not None and now > datetime.fromisoformat(parameters['effective_to']): valid = False
        source = conn.execute('SELECT mode,watermark FROM account_source WHERE id=1').fetchone()
        if source is None: valid = False
        elif source[0] == 'ledger':
            valid = valid and parameters.get('account_scope') == json.loads(source[1])['account_scope']
        elif source[0] != 'mirror': valid = False
        result = dict(applicable=True, activation=state['activation'], manifest=manifest,
                      identity=manifest['identity'], installed=state['installed'], valid=bool(valid))
        if p.get('owner_token'):
            principal = api_keys.authenticate(conn, p['owner_token'])
            registry._authorize(conn, principal, owner=True)
            result['owner_key_id'] = principal.key_id
        print(json.dumps(result))
'''

POLICY_ACTIVATE = '''
import json, sqlite3, sys
from a_share_research import api_keys
from a_share_research.policy import registry
p = json.load(sys.stdin)
conn = sqlite3.connect('/data/app.db', isolation_level=None, timeout=5)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA foreign_keys=ON')
principal = api_keys.authenticate(conn, p.pop('owner_token'))
manifest = registry.register(conn, principal, p.pop('parameters'))
if manifest['identity'] != p.pop('identity'): raise ValueError('unapproved identity')
print(json.dumps(registry.activate(conn, principal, manifest['identity'], **p)))
'''


@contextmanager
def release_lock():
    """One host lock for release and explicit completion, never a stale lockfile lease."""
    with (ROOT / '.git' / 'ashare-deploy.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有发布或完成入口正在运行') from None
        yield


def serialized(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        with release_lock():
            return function(*args, **kwargs)
    return locked


def private_json(path, *, limit=65536):
    """Read approval, not shell code; credentials never enter argv or evidence."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError('批准输入须为私有常规文件')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError('批准输入须为属主的 0600 常规文件')
        raw = stream.read(limit + 1)
    try:
        if len(raw) > limit: raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError
                result[key] = value
            return result
        return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeError('批准输入无效') from None


def policy_command(script, payload, *, image=None):
    args = (['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', 'python', image]
            if image else ['docker', 'exec', '-i', 'a-share-research-worker', 'python'])
    try:
        result = subprocess.run([*args, '-c', script], input=json.dumps(payload), text=True,
                                capture_output=True, timeout=40, cwd=ROOT)
        if result.returncode or len(result.stdout) > 2*1024*1024:
            raise ValueError
        value = json.loads(result.stdout)
        if not isinstance(value, dict): raise ValueError
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Child stderr can contain credentials or database details; never include it.
        raise RuntimeError('镜像内政策操作失败、输出无效或超时；保持 fail-closed') from None


def policy_probe(*, owner_token=None):
    value = policy_command(POLICY_PROBE, dict(action='probe', owner_token=owner_token))
    if not isinstance(value, dict):
        raise RuntimeError('镜像内政策状态未知')
    if value.get('applicable') is False and set(value) == {'applicable'}:
        return value
    try:
        if (value['applicable'] is not True or type(value['installed']) is not bool
                or type(value['valid']) is not bool or not isinstance(value['manifest'], dict)
                or type(value['activation']['id']) is not int or value['activation']['id'] < 1
                or value['activation']['identity'] != value['identity']
                or value['manifest']['identity'] != value['identity']):
            raise ValueError
        from a_share_research.policy.identity import verify
        verify(value['manifest'])  # Self-consistency only, never host check_installed.
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('镜像内政策状态未知或不完整') from None
    return value


def save_evidence(directory, name, value):
    """Durable, atomic evidence; a killed process leaves the last held checkpoint."""
    temporary = directory / (name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / name)
    recovery._sync_directory(directory)


def policy_summary(state):
    return {k: state.get(k) for k in ('applicable', 'activation', 'identity', 'installed', 'valid')}


def observe_held_policy(state: dict) -> None:
    """Best-effort read-only evidence, never an admission gate or inferred approval."""
    try:
        observed = policy_summary(policy_probe()) | dict(observation='observed', at=db.now())
    except Exception:
        # A lost activation receipt can mean a committed append. Never present the
        # earlier observation as current when the actual container cannot be read.
        observed = dict(applicable=None, activation=None, identity=None, installed=None, valid=None,
                        observation='unknown', at=db.now(), reason='最终只读政策探针失败；状态未确认')
    state['actual_policy'] = observed


def identity_diff(old, new):
    """Only hashes/paths and parameter equality; never approval credentials."""
    def changed(a, b):
        return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return dict(identity=[old['identity'], new['identity']],
                parameters_changed=old['parameters'] != new['parameters'],
                code=changed(old['files']['code'], new['files']['code']),
                rules=[old.get('rules_hash'), new.get('rules_hash')],
                guides=changed(old['files']['guides'], new['files']['guides']))


def approval_for(approval, manifest, predecessor, *, rollback=False):
    selected = approval.get('rollback') if rollback and approval else approval
    try:
        if not isinstance(selected, dict): raise ValueError
        allowed = {'identity', 'expected_id', 'request_id', 'reason'}
        if not rollback: allowed |= {'owner_token', 'rollback'}
        if (set(selected) - allowed or selected['identity'] != manifest['identity']
                or type(selected['expected_id']) is not int or selected['expected_id'] != predecessor
                or not isinstance(approval['owner_token'], str) or not approval['owner_token']):
            raise ValueError
        from a_share_research.policy.registry import _text
        for key, limit in (('request_id', 128), ('reason', 1000)):
            _text(selected[key], key, limit)
        return {k: selected[k] for k in ('identity', 'expected_id', 'request_id', 'reason')}
    except (KeyError, ValueError, TypeError):
        raise RuntimeError('缺少本次精确 identity/expected_id/request_id/reason 的明确批准') from None


def freeze_policy(images, fallback, *, approval, not_applicable, preview=False):
    current = policy_probe(owner_token=approval.get('owner_token') if approval else None)
    if current.get('applicable') is False:
        if not not_applicable or approval:
            raise RuntimeError('required 政策缺失；不得发布')
        # The explicit opt-out is only for a policy-free old research instance.
        return dict(required=False)
    if not current['valid']:
        raise RuntimeError('原政策参数无效、过期或不适用账户')
    old = current['manifest']
    candidate = policy_command(POLICY_PROBE, dict(action='candidate', parameters=old['parameters']), image=images['worker'])
    from a_share_research.policy.identity import verify
    verify(candidate)
    if candidate['parameters'] != old['parameters']:
        raise RuntimeError('候选政策更改了原参数')
    intent = None
    if candidate['identity'] != current['identity']:
        if not preview:
            intent = approval_for(approval, candidate, current['activation']['id'])
    restore = None
    # Rollback approval freezes the actual fallback implementation, not host sources.
    if approval and approval.get('rollback') is not None:
        previous = policy_command(POLICY_PROBE, dict(action='candidate', parameters=old['parameters']), image=fallback['worker'])
        verify(previous)
        if previous['identity'] != old['identity'] or previous['parameters'] != old['parameters']:
            raise RuntimeError('批准恢复须为冻结的前驱 identity；恢复镜像不匹配')
        # The restored activation's expected_id is not known until the new append.
        item = approval['rollback']
        if set(item) != {'identity', 'request_id', 'reason'}:
            raise RuntimeError('恢复批准仅接受冻结 identity、稳定 request_id 和 reason')
        restore = approval_for(dict(owner_token=approval['owner_token'], rollback=item | {'expected_id': 0}),
                               old, 0, rollback=True)
        restore.pop('expected_id')
    return dict(required=True, predecessor=current['activation'], parameters=old['parameters'],
                target_identity=candidate['identity'], target_manifest=candidate, old_identity=old['identity'],
                intent=intent, restore=restore, owner_key_id=current.get('owner_key_id'),
                diff=identity_diff(old, candidate))


def policy_gate(images, frozen, *, approval=None, restoring=False, save=lambda *_: None):
    """Every admission path uses actual-container state and rereads latest after idempotency."""
    for service, image in images.items():
        actual = describe(service)
        if not actual['running'] or actual['image'] != image:
            raise RuntimeError('政策门禁：实际镜像不匹配')
    current = policy_probe()
    save('policy-observed.json', policy_summary(current) | dict(at=db.now()))
    if not frozen['required']:
        if current.get('applicable') is not False:
            raise RuntimeError('政策不适用声明与实际状态不符')
        save('policy-gate.json', policy_summary(current))
        return current
    if not current.get('applicable') or not current['valid'] or current['manifest']['parameters'] != frozen['parameters']:
        raise RuntimeError('政策门禁：状态未知、参数无效或账户不适用')
    predecessor = frozen['predecessor']
    expected_identity = frozen['old_identity'] if restoring else frozen['target_identity']
    intent = frozen['intent']
    expected = predecessor
    if restoring:
        if current['activation'] != predecessor:
            restore = frozen['restore']
            committed = frozen.get('committed_activation')
            resumed_restore = (restore is not None and committed is not None
                               and _our_activation(current['activation'], restore | dict(expected_id=committed['id']), frozen))
            # Only our exact new activation can be compensated, never another owner's switch.
            if not resumed_restore:
                if not intent or not _our_activation(current['activation'], intent, frozen):
                    raise RuntimeError('恢复前驱不是本次激活；保持暂停')
                committed = current['activation']
                frozen['committed_activation'] = committed
                save('policy-committed.json', committed)
            if restore is None:
                raise RuntimeError('新激活已提交，但本次未明确批准恢复旧 identity')
            intent = restore | dict(expected_id=committed['id'])
        else:
            intent = None
    if intent is not None:
        expected = None
        if not _our_activation(current['activation'], intent, frozen):
            if current['activation']['id'] != intent['expected_id']:
                raise RuntimeError('政策门禁 CAS 冲突；不改 expected_id')
        else:
            expected = current['activation']
        if expected is None:
            if approval is None:
                raise RuntimeError('需要重新提供本次临时 owner 批准输入')
            # Compare the supplied authorization against the persisted, secret-free intent.
            if restoring:
                supplied = approval.get('rollback')
                if supplied != {k: intent[k] for k in ('identity', 'request_id', 'reason')}:
                    raise RuntimeError('恢复批准与冻结目标不符')
            elif approval_for(approval, frozen['target_manifest'], predecessor['id']) != intent:
                raise RuntimeError('激活批准与冻结目标不符')
            owner_state = policy_probe(owner_token=approval.get('owner_token'))
            if owner_state.get('owner_key_id') != frozen['owner_key_id']:
                raise RuntimeError('临时 owner 与冻结批准不符；未写激活')
            payload = intent | dict(parameters=frozen['parameters'], owner_token=approval.get('owner_token'))
            # Lost receipt: retry the SAME request once. Even an old successful receipt
            # is not proof of latest; the following read and comparison are mandatory.
            for attempt in range(2):
                try:
                    expected = policy_command(POLICY_ACTIVATE, payload)
                    break
                except RuntimeError:
                    if attempt: raise
            current = policy_probe()
        if not isinstance(expected, dict) or not _our_activation(expected, intent, frozen):
            raise RuntimeError('激活回执与本次冻结批准不符')
        # Also reread if an earlier process committed and lost its reply.
        current = policy_probe()
    if (current['identity'] != expected_identity or current['installed'] is not True or current['valid'] is not True
            or current['manifest']['parameters'] != frozen['parameters'] or current['activation'] != expected):
        raise RuntimeError('政策门禁未通过：latest activation/identity/installed 不匹配')
    if not restoring and frozen['intent'] is not None:
        frozen['committed_activation'] = current['activation']
        save('policy-committed.json', current['activation'])
    save('policy-gate.json', policy_summary(current) | dict(at=db.now()))
    return current


def _our_activation(activation, intent, frozen):
    return (all(activation.get(k) == intent[k] for k in ('identity', 'expected_id', 'request_id', 'reason'))
            and activation.get('activated_by') == f"key:{frozen['owner_key_id']}")


def describe(service: str) -> dict:
    container = json.loads(run("docker", "inspect", f"a-share-research-{service}"))[0]
    mounts = {m['Destination']: ['tmpfs' if m.get('Type') == 'tmpfs' else m['Source'], m['RW']]
              for m in container['Mounts']}
    # Docker can list --tmpfs only in HostConfig, omitting it from Mounts even while running.
    for target, options in (container['HostConfig'].get('Tmpfs') or {}).items():
        mounts.setdefault(target, ['tmpfs', 'ro' not in options.split(',')])
    return {"image": container["Image"], "running": container["State"]["Running"],
            "config_hash": container["Config"]["Labels"].get("com.docker.compose.config-hash"),
            "cpus": container["HostConfig"]["NanoCpus"], "ports": container["HostConfig"]["PortBindings"],
            "mounts": mounts}


def declared_mounts(service: str) -> dict:
    """Mounts the committed Compose file declares for a service, in `describe` shape."""
    configured = json.loads(run('docker', 'compose', '--profile', 'tools', 'config', '--format', 'json'))
    spec = configured['services'][service]
    mounts = {v['target']: [v['source'], not v.get('read_only', False)] for v in spec.get('volumes') or []}
    for entry in spec.get('tmpfs') or []:
        target, _, options = entry.partition(':')
        mounts[target] = ['tmpfs', 'ro' not in options.split(',')]
    return mounts


def verify_images(code: str) -> dict:
    archive = subprocess.check_output(["git", "archive", code, "a_share_research"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        expected = {m.name: hashlib.sha256(source.extractfile(m).read()).hexdigest() for m in source if m.isfile()}
    reader = ("import hashlib,json; from pathlib import Path; root=Path('/app'); "
              "print(json.dumps({p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() "
              "for p in (root/'a_share_research').rglob('*') if p.is_file() and '__pycache__' not in p.parts}))")
    images = {}
    for service in SERVICES:
        image = run("docker", "image", "inspect", f"a-share-research-{service}:{code}", "--format", "{{.Id}}")
        actual = json.loads(run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image, "-c", reader))
        if actual != expected:
            raise RuntimeError(f"{service} 镜像源码或读取权限与提交不一致")
        images[service] = image
    return images


def wait_ready() -> None:
    deadline = time.monotonic() + 30
    request = Request("http://127.0.0.1:8620/login", headers={"Host": "stock.keyi.win"})
    while True:
        try:
            with urlopen(request, timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError(f"本机健康检查返回 {response.status}")
                return
        except HTTPError:
            raise  # An application error is not startup connection refusal.
        except (URLError, TimeoutError, ConnectionError) as exc:  # a starting server may refuse or reset
            if time.monotonic() >= deadline:
                raise RuntimeError("本机健康检查未就绪") from exc
            log.debug("等待本机服务启动：%s", exc)
            time.sleep(0.5)


def tools_control(action: str) -> dict:
    result = json.loads(run('docker','exec','a-share-research-tools','python','-m',
                           'a_share_research.tools.control',action,'--timeout','30'))
    if (set(result) != {'paused','in_flight'} or type(result['paused']) is not bool
            or type(result['in_flight']) is not int or result['in_flight'] < 0):
        raise RuntimeError('tools 控制回执无效')
    return result


def wait_tools_ready(*, paused=False) -> dict:
    deadline = time.monotonic()+30
    opener = build_opener(ProxyHandler({}))
    while True:
        try:
            try:
                response = opener.open('http://127.0.0.1:8621/ready',timeout=2)
            except HTTPError as exc:
                if exc.code != 503:
                    raise
                response = exc
            with response:
                result = json.load(response)
            if (result.get('database_ready') is True and result.get('schema_version') == len(db.MIGRATIONS)
                    and result.get('paused') is paused and (not paused or result.get('in_flight') == 0)):
                return result
        except HTTPError:
            raise
        except (URLError,TimeoutError,ConnectionError,ValueError):
            pass  # Startup readiness may lag worker migration; the same endpoint is bounded below.
        if time.monotonic() >= deadline:
            raise RuntimeError('tools 数据库或暂停状态未就绪')
        time.sleep(.5)


def _tools_config():
    configured = json.loads(run('docker','compose','--profile','tools','config','--format','json'))['services']['tools']
    allowed = {'DATA_DIR','TUSHARE_TOKEN','ASHARE_TOOLS_EXTERNAL_ENABLED',
               'ASHARE_TOOLS_EXTERNAL_REVIEW_ENABLED','ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED'}
    if set(configured.get('environment',{})) - allowed:
        raise RuntimeError('tools 环境含非许可配置；仅允许行情凭据、数据目录和显式功能开关')
    return configured


def _verify_release(images, before, services, *, allow_config, save, tools_paused=True):
    """Verify a target or compatible recovery before reopening tools admission."""
    wait_ready()
    if 'tools' in services:
        save('tools-ready-paused.json', wait_tools_ready(paused=tools_paused))
    runtime = json.loads(run('docker', 'exec', 'a-share-research-worker', 'python', '-c',
                            'import json; from a_share_research.model import preflight; print(json.dumps(preflight()))'))
    save('worker-preflight.json', runtime)
    after = {service: describe(service) for service in services}
    for service in before:
        expected = before[service] | {'image': images[service]}
        if allow_config:
            # An authorized config change may add or drop mounts; the committed Compose file is the reference.
            expected['config_hash'] = after[service]['config_hash']
            expected['mounts'] = declared_mounts(service)
        if after[service] != expected:
            raise RuntimeError(f'{service} 镜像、运行状态或部署配置不一致')
    if 'tools' in services:
        tool = after['tools']
        if (not tool['running'] or tool['image'] != images['worker']
                or tool['mounts'].get('/data') != before['web']['mounts']['/data']
                or set(tool['mounts']) != {'/data', '/run/ashare-tools'}
                or tool['mounts'].get('/run/ashare-tools') != ['tmpfs', True]
                or tool['cpus'] != 400_000_000
                or tool['ports'] != {'8621/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8621'}]}):
            raise RuntimeError('tools 镜像、资源、挂载或回环端口与约定不符')
    check = json.loads(run('docker', 'exec', 'a-share-research-worker', 'python', '-m', 'a_share_research.integrity'))
    save('integrity-after.json', check)
    if not check['ok']:
        raise RuntimeError('发布后完整性检查失败')
    return after


@serialized
def deploy(code: str, evidence: Path, allow_config: bool = False, with_tools: bool = False,
           initial_recovery: str | None = None, approval_path: Path | None = None,
           policy_not_applicable: bool = False, plan_policy: bool = False) -> None:
    approval = private_json(approval_path) if approval_path else None
    revision = run("git", "rev-parse", f"{code}^{{commit}}")
    if revision != run("git", "rev-parse", "HEAD") or revision != run("git", "rev-parse", "origin/main"):
        raise RuntimeError("只能发布已提交并已推送到 origin/main 的 HEAD")
    if run("git", "status", "--porcelain", "--", "a_share_research", "Dockerfile", "compose.yaml", "pyproject.toml", "uv.lock"):
        raise RuntimeError("应用或部署文件有未提交修改；无关文档改动不阻断发布")
    has_tools = bool(run('docker','ps','-a','--filter','name=^/a-share-research-tools$','--format','{{.ID}}'))
    if initial_recovery is not None and (has_tools or not with_tools or not allow_config):
        raise RuntimeError('首次迁移恢复入口仅用于尚无 tools 的部署，且须同时指定 --with-tools --allow-config')
    include_tools = with_tools or has_tools
    services = (*SERVICES,'tools') if include_tools else SERVICES
    existing = (*SERVICES,'tools') if has_tools else SERVICES
    if with_tools and not has_tools and not allow_config:
        raise RuntimeError('首次纳入 tools 须使用已授权配置变更入口 --allow-config')
    if include_tools:
        _tools_config()
    images = verify_images(code)
    recovery_images = verify_images(initial_recovery) if initial_recovery is not None else None
    if include_tools:
        images['tools'] = images['worker']
    before = {service: describe(service) for service in existing}
    if not all(item["running"] for item in before.values()):
        raise RuntimeError("现有服务未同时运行，请先查明状态")
    for service in existing:
        if not allow_config and run("docker", "compose", *(('--profile','tools') if include_tools else ()),
                                    "config", "--hash", service).split() != [service, before[service]["config_hash"]]:
            raise RuntimeError(f"{service} 的 Compose 或环境配置发生变化；须另行准备配置回退")
    if has_tools:
        if before['tools']['image'] != before['worker']['image']:
            raise RuntimeError('现有 tools 与 worker 不同镜像，须先核对部分发布状态')
        if tools_control('status')['paused']:
            raise RuntimeError('tools 已被暂停；本次发布不接管已有暂停状态')
    data = Path(before["web"]["mounts"]["/data"][0])
    if before["web"]["mounts"]["/data"] != before["worker"]["mounts"]["/data"]:
        raise RuntimeError("web/worker 数据目录不一致")
    database = data / "app.db"
    if initial_recovery is None and _source_version(database) == 9:
        raise RuntimeError('v9→当前版本 首次发布须指定 --initial-recovery；不能使用日常回退路径')
    if has_tools and before['tools']['mounts'].get('/data') != before['web']['mounts']['/data']:
        raise RuntimeError('tools 数据目录与 web/worker 不一致')
    marker = data/'.tools-paused'
    if include_tools and not has_tools and (marker.exists() or marker.is_symlink()):
        raise RuntimeError('首次启动前存在 tools 暂停标记，须先核对未完成发布')
    fallback_images = recovery_images or {service: before[service]['image'] for service in existing}
    frozen = freeze_policy(images, fallback_images, approval=approval, not_applicable=policy_not_applicable,
                           preview=plan_policy)
    idle(database)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    if plan_policy:
        save_evidence(evidence, 'policy-plan.json', dict(status='planned', code=revision, before=before,
                                                       images=images, policy=frozen, at=db.now()))
        log.info('只读政策计划已保存；未暂停、切换或激活')
        return
    backup = data / f"backup-pre-{code}-{datetime.now(db.SHANGHAI):%Y%m%d-%H%M%S}.db"

    def save(name, value):
        save_evidence(evidence, name, value)
        if name.endswith(('policy-observed.json', 'policy-gate.json')):
            state['actual_policy'] = value
        if name.endswith(('policy-committed.json', 'policy-observed.json', 'policy-gate.json')):
            save_evidence(evidence, 'release-state.json', state)

    state = dict(format='ashare-release-v1', code=revision, status='held', phase='prepared',
                 before=before, images=images, services=list(services), allow_config=allow_config,
                 policy=frozen, restoring=False, at=db.now())
    def record(phase, status='held', reason=None):
        state.update(phase=phase, status=status, reason=reason, at=db.now())
        try:
            state['actual'] = {s: describe(s) for s in state['services']}
        except Exception:
            state['actual'] = None
        save('release-state.json', state)
    record('prepared')
    if set(existing) == set(services) and all(before[s]['image'] == images[s] for s in existing):
        # No activation append without pause, even when the image need not change.
        if frozen.get('intent') is None:
            _verify_release(images, before, services, allow_config=allow_config, save=save,
                            tools_paused=False)
            policy_gate(images, frozen, approval=approval, save=save)
            record('same-image', 'succeeded')
            log.info('目标镜像和政策均已就绪，无需重复切换')
            return

    if recovery_images is not None:
        save('initial-preparation.json', initial_release.prepare(database, images, recovery_images,
                                                                evidence/'initial-preparation'))
        idle(database)  # Real work may have arrived while the private copy was checked.
    for service in SERVICES:
        fallback = recovery_images[service] if recovery_images is not None else before[service]['image']
        run("docker", "tag", fallback, f"a-share-research-{service}:rollback-{code}")
    save("manifest.json", {"code": revision, "before": before, "new": images, "backup": str(backup),
                           "backup_bundle": str(backup.with_suffix('.bundle')),
                           "initial_recovery": recovery_images})
    paused = switched = tools_paused = new_tools_started = False
    migration_verified = False
    def compose(*args):
        return run('docker','compose',*(('--profile','tools') if include_tools else ()),*args)
    try:
        record('pausing')
        if include_tools:
            tools_paused = True
            if has_tools:
                drained = tools_control('pause')
                if drained != {'paused':True,'in_flight':0}:
                    raise RuntimeError('tools 尚未排空，不得停服')
            else:
                from a_share_research.tools.control import pause_offline
                pause_offline(marker)
        record('drained')
        paused = True
        compose("stop", "-t", "30", "web")
        idle(database)
        compose("stop", "-t", "30", "worker")
        if has_tools:
            compose('stop','-t','60','tools')
        idle(database)
        original = fingerprints(database)
        expected = original
        save("tables-before.json", original)
        if backup.exists():
            raise FileExistsError(backup)
        counts = create_release_backup(database,backup)
        record('switching')
        switched = True  # Restore both tags even if only the first replacement succeeds.
        if recovery_images is not None:
            # No application writer runs until the actual stopped database passes conservation.
            initial_release.migrate(database, images['worker'], evidence/'initial-migration')
            migration_verified = True
            expected = fingerprints(database)
            save('tables-migrated.json', expected)
        for service in SERVICES:
            run("docker", "tag", images[service], f"a-share-research-{service}:latest")
        final = fingerprints(database)
        save("tables-after.json", final)
        if final != expected:
            migration_verified = False
            raise RuntimeError("停服窗口内业务表发生变化；保留数据供核对")
        new_tools_started = include_tools and not has_tools
        compose("up", "-d", "--no-build", "--no-deps", *services)
        after = _verify_release(images, before, services, allow_config=allow_config, save=save)
        record('policy-gate')
        policy_state = policy_gate(images, frozen, approval=approval, save=save)
        state['actual_policy'] = policy_summary(policy_state)
        record('resuming')
        if include_tools:
            tools_paused = False  # Even a lost resume reply may have admitted requests; rollback must drain again.
            if tools_control('resume')['paused']:
                raise RuntimeError('tools 未恢复受理')
            save('tools-ready.json',wait_tools_ready())
        record('ready', 'succeeded')
        # The first migration retains all preparation/recovery evidence and images.
        pruned = prune_backups(data) if recovery_images is None else []
        pruned_images = prune_images(images) if recovery_images is None else {}
        save("manifest.json", {"code": revision, "before": before, "after": after, "new": images,
                               "backup": str(backup), "backup_bundle": str(backup.with_suffix('.bundle')),
                               "backup_counts": counts, "pruned_backups": pruned,
                               "pruned_images": pruned_images, "initial_recovery": recovery_images,
                               "policy": policy_summary(policy_state), "at": db.now()})
        log.info("切换完成，备份 %s；继续用真实浏览器验收受影响页面与一个正常案例", backup)
    except BaseException:
        state.update(status='held', phase='release-failed', reason='发布失败；须核对兼容性和政策后恢复')
        # Safety cannot depend on an evidence filesystem that may itself have failed.
        if include_tools and switched and not tools_paused:
            # Never replace a process that may now be handling a newly admitted financial call.
            drained = tools_control('pause')
            if drained != {'paused':True,'in_flight':0}:
                raise RuntimeError('tools 回退前排空失败；保持当前镜像和暂停状态')
            tools_paused = True
        record('release-failed', reason=state['reason'])
        log.error('发布失败，核对兼容性和政策后回退；保留数据、备份与证据')
        if recovery_images is not None and switched and not migration_verified:
            raise RuntimeError('首次迁移未通过守恒核对；保持停服和暂停标记，不自动恢复任何镜像')
        # Default rollback requires the old image; an initial release pins a separate compatible baseline.
        if switched:
            # Pin the post-failure state, including writes admitted after a lost resume reply.
            compose('stop', '-t', '30', 'web')
            idle(database)
            compose('stop', '-t', '30', 'worker')
            if include_tools:
                compose('stop', '-t', '60', 'tools')
            idle(database)
            fallback = recovery_images or {service: before[service]['image'] for service in SERVICES}
            compatibility = rollback.assess(fallback,
                                            database, evidence / 'rollback-probe')
            save('rollback-compatibility.json', compatibility)
            if compatibility['ok'] is not True:
                label = '指定恢复镜像' if recovery_images is not None else '旧镜像'
                raise RuntimeError(f'{label}读取兼容检查未通过；保持当前镜像、停服和 tools 暂停状态，'
                                   '请核对 rollback-compatibility.json')
        if switched and recovery_images is not None:
            # Keep the new tools service and all post-migration writes. This is
            # compatible recovery, never a return to the old v9 application.
            recovery_images = recovery_images | {'tools': recovery_images['worker']}
            def recovery_save(name, value):
                save('recovery-'+name, value)
            try:
                for service in SERVICES:
                    run('docker', 'tag', recovery_images[service], f'a-share-research-{service}:latest')
                compose('up', '-d', '--no-build', '--no-deps', *services)
                recovered = _verify_release(recovery_images, before, services, allow_config=allow_config,
                                            save=recovery_save)
                state.update(images=recovery_images, services=list(services), restoring=True)
                record('recovery-policy-gate')
                policy_state = policy_gate(recovery_images, frozen, approval=approval, restoring=True, save=recovery_save)
                state['actual_policy'] = policy_summary(policy_state)
                record('recovery-resuming')
                tools_paused = False
                if tools_control('resume')['paused']:
                    raise RuntimeError('兼容恢复后 tools 未恢复受理')
                recovery_save('tools-ready.json', wait_tools_ready())
                recovery_save('result.json', dict(status='recovered', images=recovery_images, after=recovered))
                record('recovery-ready', 'recovered')
            except BaseException:
                state.update(status='held', phase='recovery-held', reason='兼容恢复未完成')
                if not tools_paused:
                    if tools_control('pause') != {'paused': True, 'in_flight': 0}:
                        raise RuntimeError('兼容恢复失败且未确认排空；不替换当前服务')
                    tools_paused = True
                compose('stop', '-t', '30', 'web')
                idle(database)
                compose('stop', '-t', '30', 'worker')
                compose('stop', '-t', '60', 'tools')
                recovery_save('result.json', dict(status='held', images=recovery_images))
                record('recovery-held', reason='兼容恢复未通过政策门禁或就绪')
                raise
            raise  # The requested release still failed, even when compatible recovery succeeded.
        if new_tools_started:
            compose('rm','-f','-s','tools')
        if switched:
            for service in SERVICES:
                run("docker", "tag", before[service]["image"], f"a-share-research-{service}:latest")
            compose("up", "-d", "--no-build", "--no-deps", *existing)
        elif paused:
            compose("start", *existing)
        # Ordinary rollback and pre-switch restart use the same full gate as release.
        old_images = {s: before[s]['image'] for s in existing}
        state.update(images=old_images, services=list(existing), restoring=True)
        record('rollback-policy-gate')
        _verify_release(old_images, before, existing, allow_config=False, save=save)
        policy_state = policy_gate(old_images, frozen, approval=approval, restoring=True, save=save)
        state['actual_policy'] = policy_summary(policy_state)
        record('rollback-resuming')
        try:
            if tools_paused:
                if has_tools:
                    tools_paused = False
                    if tools_control('resume')['paused']:
                        raise RuntimeError('回退后 tools 未恢复受理')
                    save('rollback-tools-ready.json', wait_tools_ready())
                else:
                    from a_share_research.tools.control import Gate
                    Gate(marker=marker).resume()
            record('rollback-ready', 'recovered')
        except BaseException:
            state.update(status='held', phase='rollback-held', reason='回退 resume/ready 或证据未完成')
            if has_tools and not tools_paused:
                if tools_control('pause') != {'paused': True, 'in_flight': 0}:
                    raise RuntimeError('回退后二次排空失败；不替换当前进程')
                tools_paused = True
            raise
        raise
    finally:
        if state['status'] == 'held':
            # Reobserve possible committed activation/compensation after lost receipts.
            observe_held_policy(state)
            try:
                record(state['phase'], reason=state.get('reason') or '核验未完成；保持暂停，不报成功')
            except OSError:
                log.error('最终 held 证据写入失败；旧证据可能过时，不能据此宣称发布成功')


@serialized
def complete_held(evidence: Path, *, approval_path: Path | None = None):
    """Explicit operator consent to finish one tool-recorded held target, never switch."""
    state = private_json(evidence / 'release-state.json', limit=4*1024*1024)
    if (not isinstance(state, dict) or state.get('format') != 'ashare-release-v1'
            or state.get('status') != 'held' or state.get('phase') == 'prepared'):
        raise RuntimeError('只接受本工具记录的 held 发布证据')
    images, services, before = state['images'], state['services'], state['before']
    if set(images) != set(services) or not set(services) <= {'web', 'worker', 'tools'} or not set(SERVICES) <= set(services):
        raise RuntimeError('冻结服务/镜像无效')
    data = Path(before['web']['mounts']['/data'][0])
    marker = data / '.tools-paused'
    if not marker.is_file() or marker.is_symlink():
        raise RuntimeError('缺少持久暂停；不接管未知状态')
    # The marker has no release_id and proves no ownership. Explicit held selection
    # plus unchanged actual target/configuration and live drained status are required.
    if {s: describe(s) for s in services} != state.get('actual'):
        raise RuntimeError('实际服务已偏离 held 证据；不得自动接管')
    if 'tools' not in services or tools_control('status') != {'paused': True, 'in_flight': 0}:
        raise RuntimeError('完成入口要求 tools paused=true 且 in_flight=0')
    _tools_config()
    for service in services:
        if run('docker', 'compose', '--profile', 'tools', 'config', '--hash', service).split() != [
                service, state['actual'][service]['config_hash']]:
            raise RuntimeError('完成入口的 Compose/环境配置偏离 held 目标')
    def save(name, value):
        save_evidence(evidence, 'completion-' + name, value)
        if name in ('policy-observed.json', 'policy-gate.json'):
            state['actual_policy'] = value
        if name in ('policy-committed.json', 'policy-observed.json', 'policy-gate.json'):
            save_evidence(evidence, 'release-state.json', state)
    approval = private_json(approval_path) if approval_path else None
    idle(data / 'app.db')
    _verify_release(images, before, services, allow_config=state['allow_config'], save=save)
    resume_attempted = False
    try:
        current = policy_gate(images, state['policy'], approval=approval, restoring=state['restoring'], save=save)
        state.update(actual_policy=policy_summary(current), phase='completion-resuming', at=db.now())
        save_evidence(evidence, 'release-state.json', state)
        resume_attempted = True
        if tools_control('resume')['paused']:
            raise RuntimeError('完成入口未恢复受理')
        save('tools-ready.json', wait_tools_ready())
        state.update(status='recovered' if state['restoring'] else 'succeeded', phase='completion-ready', at=db.now())
        save_evidence(evidence, 'release-state.json', state)
    except BaseException:
        state.update(status='held', phase='completion-held', at=db.now(), reason='政策、resume/ready 或证据未完成')
        # Never let an evidence error prevent redrain; never switch processes here.
        if resume_attempted and tools_control('pause') != {'paused': True, 'in_flight': 0}:
            raise RuntimeError('完成入口二次排空失败；不替换进程')
        raise
    finally:
        if state['status'] == 'held':
            observe_held_policy(state)
            try:
                save_evidence(evidence, 'release-state.json', state)
            except OSError:
                log.error('完成入口 held 证据写入失败；旧证据可能过时，不能据此宣称成功')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('code', nargs='?', help='short commit used as the prebuilt image tag')
    parser.add_argument('evidence', nargs='?', type=Path, help='new private directory for release evidence')
    parser.add_argument('--approval', type=Path, help='0600 one-release explicit approval and temporary owner credential')
    parser.add_argument('--policy-not-applicable', action='store_true', help='explicit policy-free research instance only')
    parser.add_argument('--plan-policy', action='store_true', help='read-only exact candidate and diff; no pause/switch/activation')
    parser.add_argument('--complete-held', type=Path, help='explicitly finish this recorded held target without switching images')
    parser.add_argument("--allow-config", action="store_true",
                        help="allow authorized Compose or environment configuration changes")
    parser.add_argument('--with-tools',action='store_true',help='include the tools service on its first authorized rollout')
    parser.add_argument('--initial-recovery', metavar='CODE',
                        help='first v9→当前版本 release only: verify this compatible recovery build before stopping services')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.complete_held:
        if args.code or args.evidence or args.allow_config or args.with_tools or args.initial_recovery or args.policy_not_applicable or args.plan_policy:
            parser.error('--complete-held 仅接受 --approval，不接受新的切换目标或配置')
        complete_held(args.complete_held, approval_path=args.approval)
    else:
        if not args.code or not args.evidence:
            parser.error('发布须指定 code 和 evidence')
        deploy(args.code, args.evidence, allow_config=args.allow_config, with_tools=args.with_tools,
               initial_recovery=args.initial_recovery, approval_path=args.approval,
               policy_not_applicable=args.policy_not_applicable, plan_policy=args.plan_policy)


if __name__ == "__main__":
    main()
