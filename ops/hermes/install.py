#!/usr/bin/env python3
"""Check or explicitly install the committed Hermes scripts; never invoke Hermes."""

import argparse
import hashlib
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


def apply(target: Path) -> None:
    repo = SOURCE.parent.parent
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "ops/hermes"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    if result.stdout:
        raise ValueError("拒绝安装：ops/hermes/ 有未提交改动，请先提交后再 --apply。")
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
    parser = argparse.ArgumentParser(description=__doc__)
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
