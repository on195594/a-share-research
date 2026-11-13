#!/usr/bin/env python3
"""Weekly milestone progress: read the milestone table, check delivery against production, print the message."""

import re
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path.home() / "a-share-research"
DATABASE = Path.home() / "a-share-research-data" / "app.db"
STATES = {"未开始", "进行中", "已交付"}


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)


def parse_milestones(text: str) -> list[dict]:
    """Rows of the fixed milestone table; a malformed row fails loudly rather than vanishing from the push."""
    section = re.split(r"\n#{1,6}\s", text.split("### 固定里程碑", 1)[1], maxsplit=1)[0]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.startswith("|") or cells[0] == "步骤" or set(cells[0]) <= {"-"}:
            continue
        if len(cells) != 5 or cells[2] not in STATES:
            raise ValueError(f"里程碑行格式不对：{line}")
        commit = re.search(r"`([0-9a-f]{7,40})`", cells[3])
        rows.append({"name": cells[0], "due": date.fromisoformat(cells[1]), "state": cells[2],
                     "commit": commit[1] if commit else None, "note": cells[4]})
    if not rows:
        raise ValueError("实施计划里没有固定里程碑行")
    return rows


def render(rows: list[dict], today: date, *, production: str | None, released,
           counts: dict) -> str:
    overdue, soon, done, unreleased = [], [], [], []
    for row in rows:
        if row["state"] == "已交付":
            (done if row["commit"] and released(row["commit"]) else unreleased).append(row)
        elif row["due"] < today:
            overdue.append(row)
        elif row["due"] <= today + timedelta(days=14):
            soon.append(row)
    lines = [f"里程碑进度 {today.isoformat()}", ""]
    if overdue:
        lines.append(f"逾期 {len(overdue)} 项（须交付或改方案）")
        lines += [f"- {r['name']}：到期 {r['due']}，已逾期 {(today - r['due']).days} 天，{r['state']}" for r in overdue]
    else:
        lines.append("逾期 0 项")
    if unreleased:
        lines.append("标已交付但未发布或缺提交号")
        lines += [f"- {r['name']}：{r['commit'] or '无提交号'}" for r in unreleased]
    if soon:
        lines.append("14 天内到期")
        lines += [f"- {r['name']}：{r['due']}，{r['state']}（{r['note']}）" for r in soon]
    lines.append(f"已交付并上线 {len(done)}/{len(rows)}" + (f"：{'、'.join(r['name'] for r in done)}" if done else ""))
    lines.append(f"过去 7 天研究失败 {counts['failed']} 个，研究总数 {counts['research']} 个")
    lines.append(f"生产版本 {production or '无法确认'}")
    return "\n".join(lines)


def production_commit() -> str | None:
    """The running web container's commit tag; a stopped, missing, or untagged image reads as unconfirmed."""
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{if .State.Running}}{{.Image}}{{end}}", "a-share-research-web"],
        capture_output=True, text=True,
    )
    running_id = inspected.stdout.strip()
    if inspected.returncode or not running_id:
        return None
    out = subprocess.run(
        ["docker", "image", "ls", "a-share-research-web", "--no-trunc", "--format", "{{.Tag}} {{.ID}}"],
        capture_output=True, text=True, check=True,
    ).stdout
    tags = [
        tag for tag, image in (line.split() for line in out.splitlines() if line.strip())
        if image == running_id and re.fullmatch(r"[0-9a-f]{7,40}", tag)
    ]
    return tags[0] if len(tags) == 1 else None


def activity(since: date) -> dict:
    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT SUM(kind='research' AND status='failed'), SUM(kind='research')"
                           " FROM runs WHERE created_at >= ?", (since.isoformat(),)).fetchone()
    return dict(zip(("failed", "research"), (value or 0 for value in row)))


def main() -> None:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    plan = git("show", "HEAD:docs/implementation-plan.md")
    if plan.returncode:
        raise SystemExit(plan.stderr)
    production = production_commit()
    since = today - timedelta(days=7)
    print(render(parse_milestones(plan.stdout), today, production=production, counts=activity(since),
                 released=lambda commit: bool(production) and git("merge-base", "--is-ancestor", commit, production).returncode == 0))


if __name__ == "__main__":
    main()
