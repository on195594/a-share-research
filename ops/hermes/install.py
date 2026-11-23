#!/usr/bin/env python3
"""Check or explicitly install the committed Hermes scripts; never invoke Hermes."""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import sys

SOURCE = Path(__file__).resolve().parent
OBSOLETE = (
    "advisor_budget.py",
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


def check_legacy_pending(path: Path):
    """Cutover must not strand a request submitted by the previously installed client."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    with os.fdopen(fd) as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'{path} 旧状态须为常规文件。')
        if path.name == 'scan.json' and (info.st_mode & 0o077 or info.st_uid != os.getuid()
                                         or info.st_nlink != 1 or info.st_size > 32768):
            raise ValueError(f'{path} 旧扫描状态权限或大小无效。')
        state = json.load(stream)
    if isinstance(state, list) and path.name in ('opportunities.json', 'weekly-opportunities.json'):
        return  # Old list of delivered keys.
    if not isinstance(state, dict):
        raise ValueError(f'{path} 旧状态格式无效，请核对后再安装。')
    if state.get('pending') is not None:
        if path.name == 'scan.json':
            if state.get('schema_version') == 1:
                raise ValueError('须用旧 research key 与旧脚本查回扫描后再安装。')
            raise ValueError('扫描仍有待查请求，请用原身份查回后再安装。')
        raise ValueError(f'{path} 仍有待查政策请求，请用原 assistant 身份与旧脚本查回后再安装。')


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
    sys.path.insert(0, str(SOURCE))
    from ashare_schedule import advisor_directory, lock_path, policy_state_path, task_lock
    advisor = advisor_directory()
    legacy = [policy_state_path(job) for job in ('stop_alert', 'daily_brief', 'benchmark')]
    legacy.extend(advisor / filename for filename in ('scan.json', 'opportunities.json', 'weekly-opportunities.json'))
    with ExitStack() as locks:
        for path in [advisor / 'weekly.lock', lock_path('monthly_submit'), lock_path('monthly_status'),
                     *(state.with_suffix('.lock') for state in legacy)]:
            if not locks.enter_context(task_lock(path)):
                raise ValueError(f'{path} 的任务正在运行，请等任务完成后再安装。')
        for state in legacy:
            check_legacy_pending(state)
        copy_scripts(target)


def copy_scripts(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    modules = [SOURCE / name for name in ("ashare_tools.py", "ashare_schedule.py")]
    for source in modules + [path for path in scripts() if path not in modules]:
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
