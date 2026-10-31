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
from a_share_research.evidence import material_hash


def check(database: Path, pdf_dir: Path) -> tuple[list[str], dict]:
    """Return (problems, counts); a missing directory or database file is itself a problem."""
    if not database.is_file():
        return [f"数据库不存在：{database}"], {}
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        # One read transaction: a report published mid-check must not be compared with an older run row.
        conn.execute("BEGIN")
        problems, counts = _check_rows(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    pdfs = sorted(pdf_dir.glob("*.pdf")) if pdf_dir.is_dir() else []
    for pdf in pdfs:
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != pdf.stem:
            problems.append(f"PDF {pdf.name}：内容与文件名哈希不符（下次取用时会重新下载覆盖）")
    counts["pdfs"] = len(pdfs)
    return problems, counts


def _object(text: str | None) -> dict | None:
    """The JSON object stored in a column, or None when it is unreadable or not an object."""
    try:
        value = json.loads(text) if text is not None else {}
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _check_rows(conn: sqlite3.Connection) -> tuple[list[str], dict]:
    problems = [f"SQLite integrity_check：{row[0]}" for row in conn.execute("PRAGMA integrity_check")
                if row[0] != "ok"]
    materials: dict[int, str] = {}
    for run in conn.execute("SELECT id, identity, material FROM runs WHERE material IS NOT NULL ORDER BY id"):
        snapshot = _object(run["material"])
        if snapshot is None or not isinstance(snapshot.get("data"), dict) or not isinstance(snapshot.get("sha256"), str):
            problems.append(f"任务 {run['id']}：材料快照无法解析")
            continue
        sha = snapshot["sha256"]
        materials[run["id"]] = sha
        if material_hash(snapshot["data"]) != sha:
            problems.append(f"任务 {run['id']}：材料快照内容与其哈希不符")
        identity = _object(run["identity"])
        if identity is None:
            problems.append(f"任务 {run['id']}：运行身份无法解析")
        elif identity.get("material_sha256") not in (None, sha):
            problems.append(f"任务 {run['id']}：运行身份记录的材料哈希与快照不符")
    if any(row["name"] == "replay_of" for row in conn.execute("PRAGMA table_info(runs)")):
        for run in conn.execute(
            "SELECT id FROM runs WHERE CASE WHEN identity IS NULL OR json_valid(identity)"
            " THEN replay_of IS NOT json_extract(identity, '$.replay_of') END ORDER BY id"
        ):
            problems.append(f"任务 {run['id']}：replay_of 与运行身份 JSON 不一致")
    # A database not yet opened by this version lacks the column; its reports are all unhashed.
    hashed = any(row["name"] == "body_sha256" for row in conn.execute("PRAGMA table_info(reports)"))
    covered = any(row["name"] == "coverage" for row in conn.execute("PRAGMA table_info(reports)"))
    reports = unhashed = 0
    for report in conn.execute(f"SELECT id, run_id, body, {'body_sha256' if hashed else 'NULL'} AS body_sha256"
                               " FROM reports ORDER BY id"):
        reports += 1
        if report["body_sha256"] is None:
            unhashed += 1
        elif hashlib.sha256(report["body"].encode()).hexdigest() != report["body_sha256"]:
            problems.append(f"报告 {report['id']}：正文与写入时的哈希不符")
        body = _object(report["body"])
        if body is None:
            problems.append(f"报告 {report['id']}：正文不是 JSON 对象")
        elif body.get("material_sha256") is not None and materials.get(report["run_id"]) != body["material_sha256"]:
            problems.append(f"报告 {report['id']}：引用的材料快照与任务 {report['run_id']} 记录不符")
    if covered:
        for report in conn.execute(f"SELECT id FROM reports r WHERE coverage IS NOT {db.COVERAGE} ORDER BY id"):
            problems.append(f"报告 {report['id']}：coverage 与正文重算不一致")
    for price in conn.execute("SELECT id, body FROM prices ORDER BY id"):
        if _object(price["body"]) is None:
            problems.append(f"价格参考 {price['id']}：正文不是 JSON 对象")
    # A database not yet opened by this version has no batches table.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batches'").fetchone():
        for batch in conn.execute("SELECT * FROM batches ORDER BY id"):
            for column in ("rule", "universe", "q20", "q5", "selection", "l5", "entry", "marks"):
                try:
                    if batch[column] is not None:
                        json.loads(batch[column])
                except ValueError:
                    problems.append(f"批次 {batch['id']}：{column} 不是合法 JSON")
    return problems, {"runs_with_material": len(materials), "reports": reports, "reports_without_hash": unhashed}


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
