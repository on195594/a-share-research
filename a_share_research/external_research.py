"""Owned, immutable external submissions; review lifecycle lives in external_review."""
from __future__ import annotations

import json
import sqlite3

from a_share_research import api_keys, db, gate, stock_codes


class SubmissionError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS external_research (
        id INTEGER PRIMARY KEY, owner_key_id INTEGER NOT NULL, request_id TEXT NOT NULL,
        company_key TEXT NOT NULL, source_run_id INTEGER NOT NULL REFERENCES runs(id),
        account_aware INTEGER NOT NULL CHECK(account_aware IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('collecting','submitted')),
        version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        material_sha256 TEXT NOT NULL, material TEXT NOT NULL,
        UNIQUE(owner_key_id, request_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS external_research_by_company ON external_research(company_key, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS external_submissions (
        id INTEGER PRIMARY KEY, external_id INTEGER NOT NULL REFERENCES external_research(id),
        version INTEGER NOT NULL, created_at TEXT NOT NULL,
        accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
        draft_sha256 TEXT NOT NULL, material_sha256 TEXT NOT NULL,
        draft TEXT NOT NULL, problems TEXT NOT NULL,
        UNIQUE(external_id, version), UNIQUE(external_id, draft_sha256))""")
    for event in ('UPDATE', 'DELETE'):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS external_submissions_no_{event.lower()} BEFORE {event}"
                     " ON external_submissions BEGIN SELECT RAISE(ABORT, 'submission is immutable'); END")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS external_material_immutable
        BEFORE UPDATE OF owner_key_id, request_id, company_key, source_run_id, account_aware, material_sha256, material
        ON external_research BEGIN SELECT RAISE(ABORT, 'submission identity is immutable'); END""")


def _authorize(conn, principal):
    row = conn.execute('SELECT role FROM api_keys WHERE id=?', (principal.key_id,)).fetchone()
    if row is None or row[0] != principal.role or principal.role not in ('research', 'assistant'):
        raise SubmissionError('凭据已撤销或无外部研究权限', 403)


def _owned(conn, principal, external_id):
    _authorize(conn, principal)
    row = conn.execute('SELECT * FROM external_research WHERE id=? AND owner_key_id=?',
                       (external_id, principal.key_id)).fetchone()
    if row is None:
        raise SubmissionError('外部研究不存在或不属于此凭据', 404)
    return row


def _receipt(row, conn) -> dict:
    from a_share_research.external_review import view
    review = view(conn, row['id'])
    return dict(submission_id=row['id'], version=row['version'], accepted=bool(row['accepted']),
                draft_sha256=row['draft_sha256'], material_sha256=row['material_sha256'],
                material_revision=row['material_revision'],
                problems=json.loads(row['problems']), review_status=review['status'], review=review)


def start(conn: sqlite3.Connection, principal: api_keys.Principal, request_id: str, source_run_id: int) -> dict:
    """Reuse a trusted worker material snapshot; clients cannot upload replacement evidence."""
    if not isinstance(request_id, str) or not 1 <= len(request_id.strip()) <= 128:
        raise SubmissionError('request_id 须为1–128字符', 422)
    with db.transaction(conn):
        _authorize(conn, principal)
        if conn.execute('SELECT 1 FROM external_operations WHERE owner_key_id=? AND request_id=?',
                        (principal.key_id, request_id)).fetchone():
            raise SubmissionError('请求编号已绑定取证操作')
        previous = conn.execute('SELECT id, source_run_id FROM external_research WHERE owner_key_id=? AND request_id=?',
                                (principal.key_id, request_id)).fetchone()
        if previous:
            if previous['source_run_id'] != source_run_id:
                raise SubmissionError('请求编号已绑定其他材料')
            external_id = previous['id']
        else:
            source = conn.execute("SELECT material, company_key FROM runs WHERE id=? AND kind='research'"
                                  " AND status='done' AND material IS NOT NULL", (source_run_id,)).fetchone()
            if source is None:
                raise SubmissionError('未找到已完成研究的服务端材料', 404)
            from a_share_research.material import Material
            snapshot = json.loads(source['material'])
            material = Material.from_snapshot(snapshot)
            if (stock_codes.normalize_key(material.code) != source['company_key']
                    or material.doc.code != material.code):
                raise SubmissionError('材料与公司不一致', 422)
            external_id = conn.execute(
                "INSERT INTO external_research(owner_key_id,request_id,company_key,source_run_id,account_aware,"
                "status,created_at,material_sha256,material) VALUES (?,?,?,?,?,'collecting',?,?,?)",
                (principal.key_id, request_id, source['company_key'], source_run_id, int(principal.role == 'assistant'),
                 db.now(), snapshot['sha256'], source['material'])).lastrowid
            conn.execute('INSERT INTO external_materials(external_id,revision,created_at,material_sha256,material)'
                         ' SELECT id,0,created_at,material_sha256,material FROM external_research WHERE id=?', (external_id,))
    return status(conn, principal, external_id)


def status(conn: sqlite3.Connection, principal: api_keys.Principal, external_id: int) -> dict:
    from a_share_research import external_operations as operations
    row = _owned(conn, principal, external_id)
    receipts = [_receipt(r, conn) for r in conn.execute(
        'SELECT * FROM external_submissions WHERE external_id=? ORDER BY version', (external_id,))]
    current = row['status']
    if (receipts and receipts[-1]['material_revision'] == row['material_revision']
            and receipts[-1]['review_status'] in ('reviewing', 'done', 'failed')):
        current = receipts[-1]['review_status']
    pending = operations.busy(conn, external_id)
    material = operations.revision(conn, external_id, row['material_revision'])
    return dict(id=row['id'], company_key=row['company_key'], source_run_id=row['source_run_id'],
                account_aware=bool(row['account_aware']), status=current, version=row['version'],
                material_sha256=material['material_sha256'], material_revision=row['material_revision'],
                operation_id=pending['id'] if pending else None, receipts=receipts)


def material_snapshot(conn, principal, external_id, material_revision=None) -> dict:
    """Only the creating credential can retrieve the evidence frozen for its draft."""
    from a_share_research import external_operations as operations
    row = _owned(conn, principal, external_id)
    number = row['material_revision'] if material_revision is None else material_revision
    return json.loads(operations.revision(conn, external_id, number)['material'])


def submit(conn: sqlite3.Connection, principal: api_keys.Principal, external_id: int,
           expected_version: int, draft: dict, material_revision: int = 0) -> dict:
    """Atomically freeze a checked revision; duplicate content returns its original receipt."""
    from a_share_research import external_operations as operations
    if type(material_revision) is not int or material_revision < 0:
        raise SubmissionError('材料版本须为非负整数', 422)
    if not isinstance(draft, dict):
        raise SubmissionError('草稿须为JSON对象', 422)
    try:
        text, sha = gate.canonical(draft), gate.digest(draft)
    except (ValueError, TypeError):
        raise SubmissionError('草稿须为有效JSON', 422) from None
    with db.transaction(conn):
        row = _owned(conn, principal, external_id)
        material = operations.revision(conn, external_id, material_revision)
        previous = conn.execute('SELECT * FROM external_submissions WHERE external_id=? AND draft_sha256=? AND material_sha256=?',
                                (external_id, sha, material['material_sha256'])).fetchone()
        if previous:
            return _receipt(previous, conn)
        if operations.busy(conn, external_id):
            raise SubmissionError('存在未完成取证操作，请先查询操作结果')
        if row['version'] != expected_version:
            raise SubmissionError(f'版本冲突：当前版本为 {row["version"]}')
        problems = gate.check_draft(draft, json.loads(material['material']))
        version = row['version'] + 1
        submission_id = conn.execute(
            'INSERT INTO external_submissions(external_id,version,created_at,accepted,draft_sha256,'
            'material_sha256,draft,problems,material_revision) VALUES (?,?,?,?,?,?,?,?,?)',
            (external_id, version, db.now(), int(not problems), sha, material['material_sha256'], text,
             json.dumps(problems, ensure_ascii=False), material_revision)).lastrowid
        conn.execute('UPDATE external_research SET version=?,status=? WHERE id=?',
                     (version, 'submitted' if not problems and material_revision == row['material_revision'] else 'collecting', external_id))
        receipt = conn.execute('SELECT * FROM external_submissions WHERE id=?', (submission_id,)).fetchone()
        return _receipt(receipt, conn)
