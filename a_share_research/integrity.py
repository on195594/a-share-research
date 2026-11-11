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

from a_share_research import db, gate
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
    # Calls may store only appended rounds and cut out shared blocks; the rebuilt prompt must hash as when sent.
    calls = 0
    for call in conn.execute("SELECT id, identity FROM calls ORDER BY id"):
        calls += 1
        identity = _object(call["identity"])
        sha = identity.get("prompt_sha256") if identity else None
        try:
            prompt = db.call_prompt(conn, call["id"])
        except (ValueError, KeyError, TypeError):
            problems.append(f"模型调用 {call['id']}：提示无法还原（前一轮或共享块缺失）")
            continue
        if sha is not None and hashlib.sha256(prompt.encode()).hexdigest() != sha:
            problems.append(f"模型调用 {call['id']}：提示与调用时记录的哈希不符")
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
    from a_share_research.account.audit import inspect as inspect_account
    problems.extend(inspect_account(conn))
    from a_share_research.account.position_store import inspect as inspect_positions
    problems.extend(inspect_positions(conn))
    from a_share_research.account.projection_ids import inspect as inspect_projection_ids
    problems.extend(inspect_projection_ids(conn))
    from a_share_research.policy.v0.account_store import inspect as inspect_account_policy
    problems.extend(inspect_account_policy(conn))
    from a_share_research.policy.registry import inspect as inspect_policy
    problems.extend(inspect_policy(conn))
    from a_share_research.account.analysis import inspect as inspect_analysis
    problems.extend(inspect_analysis(conn))
    return problems, {"runs_with_material": len(materials), "calls": calls, "reports": reports,
                      "reports_without_hash": unhashed, **_check_relations(conn, problems), **_check_external(conn, problems)}


def _check_relations(conn: sqlite3.Connection, problems: list[str]) -> dict:
    """Links no foreign key can enforce. Release backups taken before v9 have none of these columns."""
    if "company_key" not in {row["name"] for row in conn.execute("PRAGMA table_xinfo(reports)")}:
        return {}
    for row in conn.execute("SELECT r.id, r.run_id FROM reports r JOIN runs u ON u.id = r.run_id"
                            " WHERE r.company_key != u.company_key ORDER BY r.id"):
        problems.append(f"报告 {row['id']}：公司与任务 {row['run_id']} 不一致")
    for row in conn.execute("SELECT u.id, u.parent_report_id FROM runs u JOIN reports p ON p.id = u.parent_report_id"
                            " WHERE p.company_key != u.company_key ORDER BY u.id"):
        problems.append(f"任务 {row['id']}：公司与父报告 {row['parent_report_id']} 不一致")
    # Reported, not failed: deleting a report by hand legitimately leaves its continuations without a parent.
    dangling = conn.execute("SELECT u.id FROM runs u LEFT JOIN reports p ON p.id = u.parent_report_id"
                            " WHERE u.parent_report_id IS NOT NULL AND p.id IS NULL ORDER BY u.id").fetchall()
    orphaned = conn.execute("SELECT u.id FROM runs u LEFT JOIN batches b ON b.id = u.batch_id"
                            " WHERE u.batch_id IS NOT NULL AND b.id IS NULL ORDER BY u.id").fetchall()
    return {"dangling_parent_reports": [row["id"] for row in dangling],
            "orphan_batch_runs": [row["id"] for row in orphaned]}


def _check_external(conn: sqlite3.Connection, problems: list[str]) -> dict:
    """Frozen external evidence and lifecycle links; revoked keys remain valid historical producers."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'external_research' not in tables:
        return {}
    sessions = {r['id']: r for r in conn.execute('SELECT * FROM external_research')}
    submissions = {r['id']: r for r in conn.execute('SELECT * FROM external_submissions')}
    revisions = ({(r['external_id'], r['revision']): r for r in conn.execute('SELECT * FROM external_materials')}
                 if 'external_materials' in tables else {(eid, 0): row for eid, row in sessions.items()})
    if 'external_materials' in tables:
        from a_share_research.external_operations import inspect
        problems.extend(inspect(conn))
    for eid, row in sessions.items():
        snapshot = _object(row['material'])
        try:
            valid = (snapshot and isinstance(snapshot.get('data'), dict)
                     and snapshot.get('sha256') == row['material_sha256']
                     and material_hash(snapshot['data']) == row['material_sha256'])
        except (TypeError, ValueError):
            valid = False
        if not valid:
            problems.append(f'外部研究 {eid}：材料快照哈希不符')
    for sid, row in submissions.items():
        parent = sessions.get(row['external_id'])
        draft = _object(row['draft'])
        try:
            valid = draft is not None and gate.digest(draft) == row['draft_sha256']
        except (TypeError, ValueError):
            valid = False
        if not valid:
            problems.append(f'外部提交 {sid}：草稿哈希不符')
        bound = revisions.get((row['external_id'], row['material_revision'] if 'material_revision' in row.keys() else 0))
        if parent is None or bound is None or bound['material_sha256'] != row['material_sha256']:
            problems.append(f'外部提交 {sid}：材料与所属研究不一致')
    reviews = []
    if 'external_reviews' in tables:
        reviews = conn.execute('SELECT * FROM external_reviews').fetchall()
        for row in reviews:
            sid = row['submission_id']
            submission = submissions.get(sid)
            parent = sessions.get(submission['external_id']) if submission else None
            run = conn.execute('SELECT * FROM runs WHERE id=?', (row['run_id'],)).fetchone()
            expected_status = {'submitted': 'queued', 'reviewing': 'running', 'done': 'done', 'failed': 'failed'}
            if (not submission or not submission['accepted'] or not parent or not run
                    or run['kind'] != 'external' or run['company_key'] != parent['company_key']
                    or run['status'] != expected_status.get(row['status'])):
                problems.append(f'外部复核 {sid}：提交、任务类型、公司或状态关联不一致')
                continue
            bound = revisions.get((submission['external_id'],
                                   submission['material_revision'] if 'material_revision' in submission.keys() else 0))
            if bound is None or run['material'] != bound['material']:
                problems.append(f'外部复核 {sid}：任务材料与冻结材料不一致')
            producer = dict(kind='external', key_id=parent['owner_key_id'],
                            account_aware=bool(parent['account_aware']), submission_id=sid)
            identity = _object(run['identity'])
            if identity and identity.get('producer') != producer:
                problems.append(f'外部复核 {sid}：任务生产者身份不一致')
            report = conn.execute('SELECT * FROM reports WHERE run_id=?', (run['id'],)).fetchone()
            if (row['report_id'] != (report['id'] if report else None)
                    or (row['status'] == 'done' and not report)):
                problems.append(f'外部复核 {sid}：发布报告关联不一致')
            if report:
                body = _object(report['body'])
                if (not body or body.get('producer') != producer
                        or report['company_key'] != parent['company_key']
                        or bool(report['passed']) != (row['status'] == 'done')
                        or row['status'] not in ('done', 'failed')):
                    problems.append(f'外部复核 {sid}：报告生产者、公司或通过状态不一致')
        linked = {r['run_id'] for r in reviews}
        for row in conn.execute("SELECT id FROM runs WHERE kind='external'"):
            if row['id'] not in linked:
                problems.append(f"外部任务 {row['id']}：缺少复核映射")
    if not sessions and not submissions and not reviews:
        return {}  # Preserve the existing count shape before any external work exists.
    return {'external_research': len(sessions), 'external_submissions': len(submissions),
            'external_reviews': len(reviews)}


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
