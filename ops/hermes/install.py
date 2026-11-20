#!/usr/bin/env python3
"""Check or explicitly install the committed Hermes scripts; never invoke Hermes."""

import argparse
from contextlib import ExitStack
import fcntl
from functools import partial
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parent
OBSOLETE = (
    "a-share-k7-progress.py",
    "a-share-k7-progress.sh",
    "a-share-shadow-diff.sh",
    "a-share-remind-release.sh",
    "a-share-remind-cutover.sh",
    "a-share-remind-closeout.sh",
)


def scripts() -> list[Path]:
    return sorted(p for p in SOURCE.iterdir()
                  if p.is_file() and p.suffix in {".py", ".sh"} and p.name != "install.py")


def fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o111


def check(target: Path) -> int:
    different = False
    for source in scripts():
        dest = target / source.name
        if dest.is_symlink():
            status = "不同"
        elif not dest.exists():
            status = "缺失"
        elif not dest.is_file() or fingerprint(source) != fingerprint(dest):
            status = "不同"
        else:
            status = "一致"
        print(f"{status} {source.name}")
        different |= status != "一致"
    for name in OBSOLETE:
        dest = target / name
        if dest.exists() or dest.is_symlink():
            print(f"待移除 {name}")
            different = True
    return int(different)


def script_module(name: str):
    spec = spec_from_file_location(name, SOURCE / f"{name}.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply(target: Path) -> None:
    repo = SOURCE.parent.parent
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "ops/hermes"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    if result.stdout:
        raise ValueError("拒绝安装：ops/hermes/ 有未提交改动，请先提交后再 --apply。")
    ignored = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--", "ops/hermes"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    if any(Path(line).suffix in {".py", ".sh"} for line in ignored.stdout.splitlines() if line.strip()):
        raise ValueError("拒绝安装：ops/hermes/ 存在被忽略的脚本文件，请清理后再 --apply。")
    advisor = script_module("a-share-advisor")
    with ExitStack() as locks:
        if not locks.enter_context(advisor.task_lock()):
            raise ValueError("扫描正在运行，请等原扫描完成后再安装。")
        if advisor.read_scan_state()["pending"] is not None:
            raise ValueError("扫描仍有待查请求，请用原身份查回后再安装。")
        policy_states = []
        for name in ("a-share-stop-alert", "a-share-daily-brief", "a-share-benchmark"):
            script = script_module(name)
            policy_states.append((script.STATE, script.load_state))
        for path in (advisor.OPPORTUNITY_STATE, advisor.WEEKLY_OPPORTUNITY_STATE):
            policy_states.append((path, partial(advisor.read_opportunity_state, path)))
        for path, load in policy_states:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = locks.enter_context(path.with_suffix(".lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(f"{path} 的政策任务正在运行，请等任务完成后再安装。") from None
            if load()["pending"] is not None:
                raise ValueError(f"{path} 仍有待查政策请求，请用原 assistant 身份与旧脚本查回后再安装。")
        copy_scripts(target)


def copy_scripts(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for source in scripts():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target, prefix=f".{source.name}.", delete=False) as stream:
                temporary = Path(stream.name)
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, stream)
                os.fchmod(stream.fileno(), source.stat().st_mode & 0o777)
            os.replace(temporary, target / source.name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    for name in OBSOLETE:
        (target / name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "MCP 切换前用原身份完成待查请求，确认扫描和政策任务的 pending 均为空。"
        "Hermes 通过 ashare 使用 ~/.config/ashare/hermes-assistant.json。"
    ))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只读核对（默认）")
    mode.add_argument("--apply", action="store_true", help="安装已提交的脚本并核对")
    parser.add_argument("--target", type=Path, default=Path.home() / ".hermes/scripts/cron")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            apply(args.target)
        return check(args.target)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
