"""Switch prebuilt local images after explicit production authorization.

This is the existing release procedure, without tests, builds or browser control.
"""

import argparse
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
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, ProxyHandler

from a_share_research import backup as recovery, db, initial_release, rollback

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("web", "worker")
TABLES = ("runs", "calls", "reports", "prices", "batches", "sessions", "api_keys")
OPTIONAL_TABLES = (*db.ACCOUNT_TABLES, 'account_source', 'policy_activations',
                   'external_research', 'external_submissions', 'external_reviews',
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


def fingerprints(database: Path) -> dict:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = (*TABLES, *(name for name in OPTIONAL_TABLES if name in existing))
        return {table: hashlib.sha256(json.dumps(conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(),
                                                 ensure_ascii=False).encode()).hexdigest() for table in tables}


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


def _verify_release(images, before, services, *, allow_config, save):
    """Verify a target or compatible recovery before reopening tools admission."""
    wait_ready()
    if 'tools' in services:
        save('tools-ready-paused.json', wait_tools_ready(paused=True))
    runtime = json.loads(run('docker', 'exec', 'a-share-research-worker', 'python', '-c',
                            'import json; from a_share_research.model import preflight; print(json.dumps(preflight()))'))
    save('worker-preflight.json', runtime)
    after = {service: describe(service) for service in services}
    for service in before:
        expected = before[service] | {'image': images[service]}
        if allow_config:
            expected['config_hash'] = after[service]['config_hash']
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


def deploy(code: str, evidence: Path, allow_config: bool = False, with_tools: bool = False,
           initial_recovery: str | None = None) -> None:
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
    if set(existing) == set(services) and all(before[service]["image"] == images[service] for service in existing):
        if has_tools:
            wait_tools_ready()
        log.info("目标镜像已运行，无需重复切换")
        return
    data = Path(before["web"]["mounts"]["/data"][0])
    if before["web"]["mounts"]["/data"] != before["worker"]["mounts"]["/data"]:
        raise RuntimeError("web/worker 数据目录不一致")
    database = data / "app.db"
    if has_tools and before['tools']['mounts'].get('/data') != before['web']['mounts']['/data']:
        raise RuntimeError('tools 数据目录与 web/worker 不一致')
    marker = data/'.tools-paused'
    if include_tools and not has_tools and (marker.exists() or marker.is_symlink()):
        raise RuntimeError('首次启动前存在 tools 暂停标记，须先核对未完成发布')
    idle(database)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup = data / f"backup-pre-{code}-{datetime.now(db.SHANGHAI):%Y%m%d-%H%M%S}.db"

    def save(name, value):
        (evidence / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))

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
        if include_tools:
            tools_paused = True
            if has_tools:
                drained = tools_control('pause')
                if drained != {'paused':True,'in_flight':0}:
                    raise RuntimeError('tools 尚未排空，不得停服')
            else:
                from a_share_research.tools.control import pause_offline
                pause_offline(marker)
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
        if include_tools:
            tools_paused = False  # Even a lost resume reply may have admitted requests; rollback must drain again.
            if tools_control('resume')['paused']:
                raise RuntimeError('tools 未恢复受理')
            save('tools-ready.json',wait_tools_ready())
        # The first migration retains all preparation/recovery evidence and images.
        pruned = prune_backups(data) if recovery_images is None else []
        pruned_images = prune_images(images) if recovery_images is None else {}
        save("manifest.json", {"code": revision, "before": before, "after": after, "new": images,
                               "backup": str(backup), "backup_bundle": str(backup.with_suffix('.bundle')),
                               "backup_counts": counts, "pruned_backups": pruned,
                               "pruned_images": pruned_images, "initial_recovery": recovery_images, "at": db.now()})
        log.info("切换完成，备份 %s；继续用真实浏览器验收受影响页面与一个正常案例", backup)
    except BaseException:
        log.exception("发布失败，核对兼容性后回退应用；保留数据、备份与证据")
        if recovery_images is not None and switched and not migration_verified:
            raise RuntimeError('首次迁移未通过守恒核对；保持停服和暂停标记，不自动恢复任何镜像')
        # Default rollback requires the old image; an initial release pins a separate compatible baseline.
        if include_tools and switched and not tools_paused:
            # Never replace a process that may now be handling a newly admitted financial call.
            drained = tools_control('pause')
            if drained != {'paused':True,'in_flight':0}:
                raise RuntimeError('tools 回退前排空失败；保持当前镜像和暂停状态')
            tools_paused = True
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
                tools_paused = False
                if tools_control('resume')['paused']:
                    raise RuntimeError('兼容恢复后 tools 未恢复受理')
                recovery_save('tools-ready.json', wait_tools_ready())
                recovery_save('result.json', dict(status='recovered', images=recovery_images, after=recovered))
            except BaseException:
                if not tools_paused:
                    if tools_control('pause') != {'paused': True, 'in_flight': 0}:
                        raise RuntimeError('兼容恢复失败且未确认排空；不替换当前服务')
                    tools_paused = True
                compose('stop', '-t', '30', 'web')
                idle(database)
                compose('stop', '-t', '30', 'worker')
                compose('stop', '-t', '60', 'tools')
                recovery_save('result.json', dict(status='held', images=recovery_images))
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
        if tools_paused:
            if has_tools:
                if switched or paused:
                    wait_tools_ready(paused=True)
                tools_control('resume')
            else:
                from a_share_research.tools.control import Gate
                Gate(marker=marker).resume()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code", help="short commit used as the prebuilt image tag")
    parser.add_argument("evidence", type=Path, help="new private directory for backup and release evidence")
    parser.add_argument("--allow-config", action="store_true",
                        help="allow authorized Compose or environment configuration changes")
    parser.add_argument('--with-tools',action='store_true',help='include the tools service on its first authorized rollout')
    parser.add_argument('--initial-recovery', metavar='CODE',
                        help='first v9→v21 release only: verify this compatible recovery build before stopping services')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    deploy(args.code, args.evidence, allow_config=args.allow_config, with_tools=args.with_tools,
           initial_recovery=args.initial_recovery)


if __name__ == "__main__":
    main()
