"""Release prebuilt local images for a commit on origin/main."""

import argparse
import fcntl
import stat
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, ProxyHandler

from a_share_research import backup as recovery, db, gate

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("web", "worker")
log = logging.getLogger(__name__)


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def idle(database: Path) -> None:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        active = conn.execute("SELECT id FROM runs WHERE status IN ('queued','running')").fetchall()
        busy = conn.execute("SELECT id FROM batches WHERE status NOT IN ('failed','done','closed')"
                            " AND (status != 'tracking' OR step IS NOT NULL)").fetchall()
        scans = conn.execute("SELECT id FROM scans WHERE status IN ('queued','running')").fetchall()
    if active or busy or scans:
        raise RuntimeError(f"发布中止：研究任务 {active}、批次 {busy} 或扫描 {scans} 尚未空闲")


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
    """Serialize releases on this host."""
    with (ROOT / '.git' / 'ashare-deploy.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有发布正在运行') from None
        yield


def private_json(path):
    """Read approval, not shell code; credentials never enter argv or evidence."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError('批准输入须为私有常规文件')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError('批准输入须为属主的 0600 常规文件')
        limit = 65536
        raw = stream.read(limit + 1)
    try:
        if len(raw) > limit: raise ValueError
        return gate.loads_strict(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeError('批准输入无效') from None


# Owner's standing rule (11-19, personal use): releases activate the new policy identity without a
# per-release approval file; the tool signs with this 0600 owner credential ({"owner_token": "..."}).
OWNER_CREDENTIAL = Path.home() / '.config/a-share-research/owner.json'


def owner_token():
    """Read only once an activation is known to be needed; never logged or saved."""
    try:
        return private_json(OWNER_CREDENTIAL).get('owner_token')
    except FileNotFoundError:
        raise RuntimeError(f'需要激活新政策身份，但缺少常设 owner 凭据 {OWNER_CREDENTIAL}'
                           '（0600 JSON，含 owner_token）；或用 --approval 指定') from None


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
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('镜像内政策状态未知或不完整') from None
    return value


def save_evidence(directory, name, value):
    """Write release evidence atomically."""
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


def identity_diff(old, new):
    """Only hashes/paths and parameter equality; never approval credentials."""
    def changed(a, b):
        return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return dict(identity=[old['identity'], new['identity']],
                parameters_changed=old['parameters'] != new['parameters'],
                rules=[old.get('rules_hash'), new.get('rules_hash')],
                guides=changed(old['files']['guides'], new['files']['guides']))


def approval_for(approval, manifest, predecessor):
    try:
        allowed = {'identity', 'expected_id', 'request_id', 'reason', 'owner_token'}
        if (set(approval) - allowed or approval['identity'] != manifest['identity']
                or type(approval['expected_id']) is not int or approval['expected_id'] != predecessor
                or not isinstance(approval['owner_token'], str) or not approval['owner_token']):
            raise ValueError
        from a_share_research.policy.registry import _text
        for key, limit in (('request_id', 128), ('reason', 1000)):
            _text(approval[key], key, limit)
        return {k: approval[k] for k in ('identity', 'expected_id', 'request_id', 'reason')}
    except (KeyError, ValueError, TypeError):
        raise RuntimeError('缺少本次精确 identity/expected_id/request_id/reason 的明确批准') from None


def freeze_policy(images, *, approval, not_applicable, preview=False, standing=None):
    """``standing`` (release code) fills the empty ``approval`` dict from this plan with the owner credential."""
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
    if candidate['identity'] != current['identity'] and not preview:
        if standing is not None and approval == {}:
            # Authenticate before any pause; the CAS id comes from this authenticated read.
            approval['owner_token'] = owner_token()
            authenticated = policy_probe(owner_token=approval['owner_token'])
            if authenticated.get('identity') != current['identity']:
                raise RuntimeError('冻结期间生效政策已变化；请重新发布')
            current, pred = authenticated, authenticated['activation']['id']
            approval.update(identity=candidate['identity'], expected_id=pred, request_id=f'release-{standing}-after-{pred}',
                            reason=f'所有者常设授权（2026-11-19）：发布 {standing} 时激活新政策身份，原参数不变')
        elif not approval:
            raise RuntimeError(f'需要激活新政策身份：缺少常设 owner 凭据 {OWNER_CREDENTIAL} 或 --approval 批准')
        intent = approval_for(approval, candidate, current['activation']['id'])
    return dict(required=True, predecessor=current['activation'], parameters=old['parameters'],
                target_identity=candidate['identity'], target_manifest=candidate,
                intent=intent, owner_key_id=current.get('owner_key_id'),
                diff=identity_diff(old, candidate))


def policy_gate(images, frozen, *, approval=None, save=lambda *_: None):
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
    intent = frozen['intent']
    expected = predecessor
    if intent is not None:
        expected = None
        if not _our_activation(current['activation'], intent, frozen):
            if current['activation']['id'] != intent['expected_id']:
                raise RuntimeError('政策门禁 CAS 冲突；不改 expected_id')
        else:
            expected = current['activation']
        if expected is None:
            if not approval:
                raise RuntimeError('需要重新提供本次临时 owner 批准输入')
            if approval_for(approval, frozen['target_manifest'], predecessor['id']) != intent:
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
    if (current['identity'] != frozen['target_identity'] or current['installed'] is not True or current['valid'] is not True
            or current['manifest']['parameters'] != frozen['parameters'] or current['activation'] != expected):
        raise RuntimeError('政策门禁未通过：latest activation/identity/installed 不匹配')
    save('policy-gate.json', policy_summary(current) | dict(at=db.now()))
    return current


def _our_activation(activation, intent, frozen):
    return (all(activation.get(k) == intent[k] for k in ('identity', 'expected_id', 'request_id', 'reason'))
            and activation.get('activated_by') == f"key:{frozen['owner_key_id']}")


def describe(service: str) -> dict:
    container = json.loads(run("docker", "inspect", f"a-share-research-{service}"))[0]
    return {"image": container["Image"], "running": container["State"]["Running"],
            "config_hash": container["Config"]["Labels"].get("com.docker.compose.config-hash")}


def release_images(code: str) -> dict:
    return {service: run("docker", "image", "inspect", f"a-share-research-{service}:{code}",
                         "--format", "{{.Id}}") for service in SERVICES}


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
            # tools itself reports ready only when the database matches its own image's migrations,
            # so a pre-migration restart of the previous image passes without pinning this tool's version.
            if (result.get('database_ready') is True
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
    configured = json.loads(run('docker', 'compose', '--profile', 'tools', 'config', '--format', 'json'))['services']['tools']
    environment = configured.get('environment') or {}
    allowed = {'DATA_DIR', 'METHOD_CARDS_DIR', 'TUSHARE_TOKEN', 'ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED',
               'ASHARE_PUBLIC_MCP_URL'}
    if environment.keys() - allowed:
        raise RuntimeError('tools 环境含非许可配置；仅允许行情凭据、数据目录、方法卡目录、功能开关和公网 MCP 地址')
    if environment.get('METHOD_CARDS_DIR') != '/method-cards':
        raise RuntimeError('tools 的 METHOD_CARDS_DIR 须指向 /method-cards')


def deploy(code: str, evidence: Path, allow_config: bool = False, approval_path: Path | None = None,
           policy_not_applicable: bool = False, plan_policy: bool = False) -> None:
    with release_lock():
        revision = run('git', 'rev-parse', f'{code}^{{commit}}')
        head = run('git', 'rev-parse', 'HEAD')
        if (head != run('git', 'rev-parse', 'origin/main') or not revision.startswith(code)
                or run('git', 'merge-base', revision, head) != revision):
            raise RuntimeError('须在 origin/main 的 HEAD 上按已推送的提交号发布预构建镜像')
        if run('git', 'status', '--porcelain', '--', 'a_share_research', 'agent-runner', 'Dockerfile', 'compose.yaml',
               'pyproject.toml', 'uv.lock'):
            raise RuntimeError('应用或部署文件有未提交修改')
        has_tools = bool(run('docker', 'ps', '-a', '--filter', 'name=^/a-share-research-tools$', '--format', '{{.ID}}'))
        services = (*SERVICES, 'tools') if has_tools else SERVICES
        images = release_images(code)
        if has_tools:
            _tools_config()
            images['tools'] = images['worker']
        before = {service: describe(service) for service in services}
        # A database restore leaves all services stopped. The previous worker must be
        # the first one to open that database, so the failed image cannot migrate it again.
        offline = not any(info['running'] for info in before.values())
        if not all(info['running'] for info in before.values()) and not (offline and revision != head and not plan_policy):
            raise RuntimeError('现有服务未同时运行；恢复发布前备份后，可在全部停服时重新发布上一个提交的镜像')

        def compose(*args):
            profile = ('--profile', 'tools') if has_tools else ()
            return run('docker', 'compose', *profile, *args)

        config_changed = any(compose('config', '--hash', s).split() != [s, before[s]['config_hash']] for s in services)
        if config_changed and not allow_config:
            raise RuntimeError('Compose 或环境配置发生变化；须指定 --allow-config')
        if has_tools and not offline and before['tools']['image'] != before['worker']['image']:
            raise RuntimeError('现有 tools 与 worker 不同镜像，须先核对部分发布状态')
        data = Path(run('docker', 'inspect', '--format',
                        '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}',
                        'a-share-research-web'))
        already_paused = False
        if has_tools:
            already_paused = (data / '.tools-paused').exists() if offline else tools_control('status')['paused']
        if already_paused and revision == head:
            raise RuntimeError('tools 已被暂停；本次发布不接管已有暂停状态')
        database = data / 'app.db'
        idle(database)
        approval = private_json(approval_path) if approval_path else None if plan_policy or policy_not_applicable else {}

        def freeze():
            return freeze_policy(images, approval=approval, not_applicable=policy_not_applicable,
                                 preview=plan_policy, standing=None if approval_path else code)

        frozen = None if offline else freeze()
        evidence.mkdir(mode=0o700, parents=True, exist_ok=False)

        def save(name, value):
            save_evidence(evidence, name, value)

        state = dict(code=revision, before=before, images=images, policy=frozen, at=db.now())
        if plan_policy:
            save('policy-plan.json', state | dict(status='planned'))
            return

        def check_ready(paused, *, candidate=False):
            wait_ready()
            if has_tools:
                save('tools-ready-paused.json' if paused else 'tools-ready.json', wait_tools_ready(paused=paused))
            # A rollback can target a commit from before the runner was introduced.
            if candidate and run('git', 'ls-tree', '--name-only', revision, 'a_share_research/runner_process.py'):
                result = run('docker', 'exec', 'a-share-research-worker', 'python', '-c',
                             'import json, sys; from a_share_research.runner_process import preflight; '
                             'json.dump(preflight(), sys.stdout)')
                save('worker-preflight.json', json.loads(result))

        same_images = all(before[s]['image'] == images[s] for s in services)
        if not offline and same_images and not config_changed and not frozen.get('intent'):
            check_ready(already_paused, candidate=True)
            current = policy_gate(images, frozen, approval=approval, save=save)
            save('release-state.json', state | dict(status='succeeded', tools_paused=already_paused, actual_policy=policy_summary(current)))
            return

        backup = data / f"backup-pre-{code}-{datetime.now(db.SHANGHAI):%Y%m%d-%H%M%S}.db"
        manifest = dict(code=revision, before=before, new=images, backup=str(backup),
                        backup_bundle=str(backup.with_suffix('.bundle')))
        save('manifest.json', manifest)
        save('release-state.json', state | dict(status='switching'))
        if has_tools and not offline and tools_control('pause') != {'paused': True, 'in_flight': 0}:
            raise RuntimeError('tools 尚未排空，不得停服')
        try:
            compose('stop', '-t', '30', 'web')
            idle(database)
            compose('stop', '-t', '30', 'worker')
            if has_tools:
                compose('stop', '-t', '60', 'tools')
            idle(database)
            manifest['backup_counts'] = create_release_backup(database, backup)
            for service in SERVICES:
                run('docker', 'tag', before[service]['image'], f'a-share-research-{service}:rollback-{code}')
            for service in SERVICES:
                run('docker', 'tag', images[service], f'a-share-research-{service}:latest')
        except BaseException:
            # No replacement container has started. Restart only the original containers.
            if not offline:
                compose('start', *services)
                check_ready(has_tools)
                if has_tools and not already_paused:
                    tools_control('resume')
            raise

        if offline:
            if has_tools:
                from a_share_research.tools.control import pause_offline
                pause_offline(data / '.tools-paused')
            compose('up', '-d', '--no-build', '--no-deps', 'worker')
            frozen = freeze()
            state['policy'] = frozen
            save('release-state.json', state | dict(status='switching'))
        compose('up', '-d', '--no-build', '--no-deps', *services)
        check_ready(has_tools, candidate=True)
        current = policy_gate(images, frozen, approval=approval, save=save)
        resume = has_tools and not already_paused
        try:
            if resume:
                if tools_control('resume')['paused']:
                    raise RuntimeError('tools 未恢复受理')
                save('tools-ready.json', wait_tools_ready())
            save('release-state.json', state | dict(status='succeeded', tools_paused=already_paused,
                                                    actual_policy=policy_summary(current)))
        except BaseException:
            if resume:
                tools_control('pause')
            raise
        manifest.update(pruned_backups=prune_backups(data), pruned_images=prune_images(images))
        save('manifest.json', manifest)
        log.info('切换完成，发布前备份 %s', backup)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('code', help='short commit used as the prebuilt image tag')
    parser.add_argument('evidence', type=Path, help='new private directory for release evidence')
    parser.add_argument('--approval', type=Path, help='override: 0600 one-release approval instead of the standing owner credential')
    parser.add_argument('--policy-not-applicable', action='store_true', help='explicit policy-free research instance only')
    parser.add_argument('--plan-policy', action='store_true', help='read-only exact candidate and diff; no pause/switch/activation')
    parser.add_argument("--allow-config", action="store_true",
                        help="allow authorized Compose or environment configuration changes")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    deploy(args.code, args.evidence, allow_config=args.allow_config, approval_path=args.approval,
           policy_not_applicable=args.policy_not_applicable, plan_policy=args.plan_policy)


if __name__ == "__main__":
    main()
