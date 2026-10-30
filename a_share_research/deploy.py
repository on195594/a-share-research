"""Switch prebuilt local images after explicit production authorization.

This is the existing release procedure, without tests, builds or browser control.
"""

import argparse
import hashlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import tarfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from a_share_research import db, integrity

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("web", "worker")
TABLES = ("runs", "calls", "reports", "prices", "batches", "sessions", "api_keys")
log = logging.getLogger(__name__)


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def idle(database: Path) -> None:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        active = conn.execute("SELECT id FROM runs WHERE status IN ('queued','running')").fetchall()
        busy = conn.execute("SELECT id FROM batches WHERE status NOT IN ('failed','done')"
                            " AND (status != 'tracking' OR step IS NOT NULL)").fetchall()
    if active or busy:
        raise RuntimeError(f"发布中止：研究任务 {active} 或批次 {busy} 尚未空闲")


def fingerprints(database: Path) -> dict:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        return {table: hashlib.sha256(json.dumps(conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(),
                                                 ensure_ascii=False).encode()).hexdigest() for table in TABLES}


def describe(service: str) -> dict:
    container = json.loads(run("docker", "inspect", f"a-share-research-{service}"))[0]
    return {"image": container["Image"], "running": container["State"]["Running"],
            "config_hash": container["Config"]["Labels"].get("com.docker.compose.config-hash"),
            "cpus": container["HostConfig"]["NanoCpus"], "ports": container["HostConfig"]["PortBindings"],
            "mounts": {m["Destination"]: [m["Source"], m["RW"]] for m in container["Mounts"]}}


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


def deploy(code: str, evidence: Path) -> None:
    revision = run("git", "rev-parse", f"{code}^{{commit}}")
    if revision != run("git", "rev-parse", "HEAD") or revision != run("git", "rev-parse", "origin/main"):
        raise RuntimeError("只能发布已提交并已推送到 origin/main 的 HEAD")
    if run("git", "status", "--porcelain", "--", "a_share_research", "Dockerfile", "compose.yaml", "pyproject.toml", "uv.lock"):
        raise RuntimeError("应用或部署文件有未提交修改；无关文档改动不阻断发布")
    images = verify_images(code)
    before = {service: describe(service) for service in SERVICES}
    if not all(item["running"] for item in before.values()):
        raise RuntimeError("现有 web/worker 未同时运行，请先查明状态")
    for service in SERVICES:
        if run("docker", "compose", "config", "--hash", service).split() != [service, before[service]["config_hash"]]:
            raise RuntimeError(f"{service} 的 Compose 或环境配置发生变化；须另行准备配置回退")
    if all(before[service]["image"] == images[service] for service in SERVICES):
        log.info("目标镜像已运行，无需重复切换")
        return
    data = Path(before["web"]["mounts"]["/data"][0])
    if before["web"]["mounts"]["/data"] != before["worker"]["mounts"]["/data"]:
        raise RuntimeError("web/worker 数据目录不一致")
    database = data / "app.db"
    idle(database)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup = data / f"backup-pre-{code}-{datetime.now(db.SHANGHAI):%Y%m%d-%H%M%S}.db"

    def save(name, value):
        (evidence / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))

    for service in SERVICES:
        run("docker", "tag", before[service]["image"], f"a-share-research-{service}:rollback-{code}")
    save("manifest.json", {"code": revision, "before": before, "new": images, "backup": str(backup)})
    paused = switched = False
    try:
        paused = True
        run("docker", "compose", "stop", "-t", "30", "web")
        idle(database)
        run("docker", "compose", "stop", "-t", "30", "worker")
        idle(database)
        original = fingerprints(database)
        save("tables-before.json", original)
        if backup.exists():
            raise FileExistsError(backup)
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        backup.chmod(0o600)
        problems, counts = integrity.check(backup, data / "pdf")
        if problems:
            raise RuntimeError(f"备份完整性检查失败：{problems}")
        switched = True  # Restore both tags even if only the first replacement succeeds.
        for service in SERVICES:
            run("docker", "tag", images[service], f"a-share-research-{service}:latest")
        final = fingerprints(database)
        save("tables-after.json", final)
        if final != original:
            raise RuntimeError("停服窗口内业务表发生变化；保留数据供核对")
        run("docker", "compose", "up", "-d", "--no-build", "--no-deps", *SERVICES)
        wait_ready()
        runtime = json.loads(run("docker", "exec", "a-share-research-worker", "python", "-c",
                                 "import json; from a_share_research.model import preflight; print(json.dumps(preflight()))"))
        save("worker-preflight.json", runtime)
        after = {service: describe(service) for service in SERVICES}
        for service in SERVICES:
            if after[service] != before[service] | {"image": images[service]}:
                raise RuntimeError(f"{service} 镜像、运行状态或部署配置不一致")
        check = json.loads(run("docker", "exec", "a-share-research-worker", "python", "-m", "a_share_research.integrity"))
        save("integrity-after.json", check)
        if not check["ok"]:
            raise RuntimeError("发布后完整性检查失败")
        save("manifest.json", {"code": revision, "before": before, "after": after, "new": images,
                               "backup": str(backup), "backup_counts": counts, "at": db.now()})
        log.info("切换完成，备份 %s；继续用真实浏览器验收受影响页面与一个正常案例", backup)
    except BaseException:
        log.exception("发布失败，恢复原应用镜像；保留备份与证据")
        # shortcut: application-only rollback; schema changes need a separate migration procedure.
        if switched:
            for service in SERVICES:
                run("docker", "tag", before[service]["image"], f"a-share-research-{service}:latest")
            run("docker", "compose", "up", "-d", "--no-build", "--no-deps", *SERVICES)
        elif paused:
            run("docker", "compose", "start", *SERVICES)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("code", help="short commit used as the prebuilt image tag")
    parser.add_argument("evidence", type=Path, help="new private directory for backup and release evidence")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    deploy(args.code, args.evidence)


if __name__ == "__main__":
    main()
