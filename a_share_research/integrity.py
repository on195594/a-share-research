"""Read-only integrity check of the database and PDF store; lists problems, never repairs.

Guards against accidental corruption and half-written files, not against someone who can
write the data directory (they could rewrite a row and its hash alike). Run it before a
backup, after a restore, or after an incident; it makes no network or model calls.
"""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from a_share_research import db
from a_share_research.research import _material_hash


def check(database: Path, pdf_dir: Path) -> tuple[list[str], dict]:
    """Return (problems, counts); a missing directory or database file is itself a problem."""
    if not database.is_file():
        return [f"数据库不存在：{database}"], {}
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        problems = [f"SQLite integrity_check：{row[0]}" for row in conn.execute("PRAGMA integrity_check")
                    if row[0] != "ok"]
        materials: dict[int, str] = {}
        for run in conn.execute("SELECT id, identity, material FROM runs WHERE material IS NOT NULL ORDER BY id"):
            try:
                snapshot = json.loads(run["material"])
                sha = snapshot["sha256"]
                actual = _material_hash(snapshot["data"])
            except (ValueError, KeyError, TypeError):
                problems.append(f"任务 {run['id']}：材料快照无法解析")
                continue
            materials[run["id"]] = sha
            if actual != sha:
                problems.append(f"任务 {run['id']}：材料快照内容与其哈希不符")
            try:
                recorded = json.loads(run["identity"] or "{}").get("material_sha256")
            except ValueError:
                problems.append(f"任务 {run['id']}：运行身份无法解析")
                continue
            if recorded is not None and recorded != sha:
                problems.append(f"任务 {run['id']}：运行身份记录的材料哈希与快照不符")
        unhashed = 0
        # A database not yet opened by this version lacks the column; its reports are all unhashed.
        hashed = any(row["name"] == "body_sha256" for row in conn.execute("PRAGMA table_info(reports)"))
        reports = conn.execute(f"SELECT id, run_id, body, {'body_sha256' if hashed else 'NULL'} AS body_sha256"
                               " FROM reports ORDER BY id").fetchall()
        for report in reports:
            if report["body_sha256"] is None:
                unhashed += 1
            elif hashlib.sha256(report["body"].encode()).hexdigest() != report["body_sha256"]:
                problems.append(f"报告 {report['id']}：正文与写入时的哈希不符")
            try:
                expected = json.loads(report["body"]).get("material_sha256")
            except ValueError:
                problems.append(f"报告 {report['id']}：正文不是合法 JSON")
                continue
            if expected is not None and materials.get(report["run_id"]) != expected:
                problems.append(f"报告 {report['id']}：引用的材料快照与任务 {report['run_id']} 记录不符")
        for price in conn.execute("SELECT id, body FROM prices ORDER BY id"):
            try:
                json.loads(price["body"])
            except ValueError:
                problems.append(f"价格参考 {price['id']}：正文不是合法 JSON")
        counts = {"runs_with_material": len(materials), "reports": len(reports), "reports_without_hash": unhashed}
    finally:
        conn.close()
    pdfs = sorted(pdf_dir.glob("*.pdf")) if pdf_dir.is_dir() else []
    for pdf in pdfs:
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != pdf.stem:
            problems.append(f"PDF {pdf.name}：内容与文件名哈希不符（下次取用时会重新下载覆盖）")
    counts["pdfs"] = len(pdfs)
    return problems, counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=db.DATA / "app.db")
    parser.add_argument("--pdf-dir", type=Path, default=db.DATA / "pdf")
    args = parser.parse_args()
    problems, counts = check(args.db, args.pdf_dir)
    print(json.dumps({"ok": not problems, "counts": counts, "problems": problems}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
