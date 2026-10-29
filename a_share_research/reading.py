"""Read-only discovery and explicit report comparisons; never submits research."""
import json
from datetime import date

from a_share_research import db, research, service

PAGE_SIZE = 20


def pending_reviews(conn, limit: int = PAGE_SIZE) -> tuple[list, bool]:
    """Saved feedback requests only; no inferred events, due dates or read status.

    The latest identical request supersedes retries. A passed result closes only
    that exact parent/location/text, never every question about the same report.
    SQL returns bounded summaries rather than report bodies.
    """
    rows = conn.execute(
        "WITH requests AS (SELECT u.id, u.status, p.id AS parent_id, p.code, p.name,"
        " json_extract(u.request, '$.feedback.text') AS question,"
        " json_extract(u.request, '$.feedback.location') AS location,"
        " f.id AS result_id, f.passed,"
        " ROW_NUMBER() OVER (PARTITION BY p.id, json_extract(u.request, '$.feedback.location'),"
        " json_extract(u.request, '$.feedback.text') ORDER BY u.id DESC) AS attempt"
        " FROM runs u JOIN reports p ON p.id = json_extract(u.request, '$.parent_report_id')"
        " JOIN runs original ON original.id = p.run_id"
        " LEFT JOIN reports f ON f.run_id = u.id AND f.code = p.code"
        " WHERE u.kind = 'research' AND substr(u.code, 1, 6) = substr(p.code, 1, 6)"
        " AND json_extract(u.identity, '$.replay_of') IS NULL"
        " AND json_type(u.request, '$.feedback') = 'object'"
        " AND length(trim(json_extract(u.request, '$.feedback.text'))) > 0"
        " AND length(trim(json_extract(u.request, '$.feedback.location'))) > 0"
        " AND ((original.status = 'done' AND p.passed = 1)"
        " OR (original.status = 'failed' AND p.passed = 0)))"
        " SELECT * FROM requests WHERE attempt = 1"
        " AND NOT (status = 'done' AND COALESCE(passed, 0) = 1)"
        " ORDER BY id DESC LIMIT ?", (limit + 1,),
    ).fetchall()
    labels = {'queued': '核查排队中', 'running': '核查进行中', 'failed': '核查未完成'}
    anchors = {'risks': '#risks', 'answer': '#answer', 'business': '#business',
               'financial': '#financial', 'checkpoints': '#checkpoints'}
    return [dict(r) | {'label': ('核查未通过' if r['status'] == 'failed' and r['passed'] == 0
                               else labels.get(r['status'], '核查结果未就绪')),
                      'anchor': anchors.get(r['location'].split('[')[0], '')}
            for r in rows[:limit]], len(rows) > limit


def evidence_context(body: dict, coverage: str | None) -> dict:
    """Keep report dates separate. A partial source set is never an evidence cutoff."""
    documents = [body.get('document')] + [s.get('document') for s in (body.get('sources') or {}).values()]
    dates = set()
    missing = 0
    for document in documents:
        raw = document.get('announced') if isinstance(document, dict) else None
        try:
            parsed = date.fromisoformat(raw) if isinstance(raw, str) and len(raw) == 10 else None
        except ValueError:
            parsed = None
        if parsed:
            dates.add(parsed.isoformat())
        else:
            missing += 1
    return {'coverage': coverage, 'announced': sorted(dates), 'missing_dates': missing,
            'gaps': list(dict.fromkeys(body.get('gaps') or []))}


def search_reports(conn, query: str, page: int = 1, *, page_size: int = PAGE_SIZE) -> tuple[list, bool]:
    # Match literal text, including %, _ and quotes. Keep bodies out of the result;
    # SQLite derives coverage for only the selected page, as on the K1 home page.
    rows = conn.execute(
        "WITH latest AS (SELECT p.code, MAX(p.id) AS id,"
        " SUM(COALESCE(u.request_id, '') NOT LIKE 'batch-%') AS own"
        " FROM reports p JOIN runs u ON u.id = p.run_id"
        " WHERE json_extract(u.identity, '$.replay_of') IS NULL"
        " GROUP BY p.code HAVING MAX(instr(lower(COALESCE(p.name, '')), lower(?)) > 0"
        " OR instr(lower(p.code), lower(?)) > 0)),"
        " selected AS (SELECT * FROM latest ORDER BY id DESC LIMIT ? OFFSET ?)"
        " SELECT r.id, r.code, r.name, r.passed, r.verdict, r.created_at, s.own,"
        " COALESCE(u.request_id, '') LIKE 'batch-%' AS from_batch,"
        f" {db.COVERAGE} AS coverage FROM selected s JOIN reports r ON r.id = s.id"
        " JOIN runs u ON u.id = r.run_id ORDER BY r.id DESC",
        (query, query, page_size + 1, (page - 1) * page_size),
    ).fetchall()
    return rows[:page_size], len(rows) > page_size


def comparison(conn, report) -> dict:
    """Resolve only saved parent/prior identities, never chronological neighbours."""
    body = json.loads(report['body'])
    run = conn.execute('SELECT request, status FROM runs WHERE id = ?', (report['run_id'],)).fetchone()
    request = (json.loads(run['request'] or '{}') or {}) if run else {}
    parents = {x for x in (request.get('parent_report_id'), body.get('parent_report_id')) if isinstance(x, int)}
    if len(parents) > 1:
        return {'parent_report_id': None, 'baseline_status': 'conflict',
                'reason': '关联原报告记录不一致，无法可靠对照。'}
    parent = next(iter(parents), None) or (body.get('prior') or {}).get('report_id')
    if not isinstance(parent, int) or parent == report['id']:
        return {'parent_report_id': None, 'baseline_status': 'not_linked',
                'reason': '未记录明确关联的原报告，不能按研究时间自动配对。'}
    old = conn.execute('SELECT r.*, u.request, u.status FROM reports r JOIN runs u ON u.id = r.run_id'
                       ' WHERE r.id = ?', (parent,)).fetchone()
    if old is None or old['code'] != report['code']:
        return {'parent_report_id': parent, 'baseline_status': 'missing' if old is None else 'not_same_code',
                'reason': '关联原报告不存在或不属于同一家公司，无法对照。'}
    before = json.loads(old['body'])
    previous_request = json.loads(old['request'] or '{}') or {}
    baseline_status = ('ready' if old['status'] == 'done' and old['passed']
                       else 'failed' if old['status'] == 'failed' and not old['passed'] else 'not_ready')
    result = {'parent_report_id': parent, 'baseline_status': baseline_status,
              'old': dict(old), 'new': dict(report),
              'old_question': previous_request.get('question') or before.get('question'),
              'new_question': request.get('question') or body.get('question'),
              'old_document': before.get('document'), 'new_document': body.get('document'),
              'old_ready': service.ready(old['status'], old['passed']),
              'new_ready': bool(run and service.ready(run['status'], report['passed']))}
    if not run or not service.ready(run['status'], report['passed']) or not service.ready(old['status'], old['passed']):
        return result | {'reason': '至少一份报告尚未就绪，暂不能对照。'}
    old_rows, new_rows = before.get('table'), body.get('table')
    if not isinstance(old_rows, list) or not isinstance(new_rows, list) or not old_rows or not new_rows:
        return result | {'reason': '至少一份报告未记录财务表，无法计算指标变化。'}
    # K2's deterministic comparison assumes identical row semantics. Exclude mismatched
    # units/labels and incomplete legacy rows instead of comparing unlike quantities.
    def valid(row):
        return (isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k] for k in ('id', 'label', 'unit'))
                and isinstance(row.get('values'), dict))
    old_map = {r['id']: r for r in old_rows if valid(r)}
    new_map = {r['id']: r for r in new_rows if valid(r)}
    incompatible = {key for key in old_map.keys() & new_map.keys()
                    if any(old_map[key][k] != new_map[key][k] for k in ('unit', 'label'))}
    def label(row):
        return next((row[k] for k in ('label', 'id') if isinstance(row, dict)
                     and isinstance(row.get(k), str) and row[k]), '未知指标')
    excluded = sorted({label(r) for r in old_rows + new_rows if not valid(r) or r.get('id') in incompatible})
    old_table = [r for key, r in old_map.items() if key not in incompatible]
    new_table = [r for key, r in new_map.items() if key not in incompatible]
    if not old_table or not new_table:
        return result | {'reason': '缺少口径完整且可比较的指标，不能计算变化。', 'excluded': excluded}
    diff = research.since_prior(old_table, new_table)
    # Empty market snapshots are missing observations, not comparable values.
    diff['valuation'] = [v for v in diff['valuation'] if v['before'] and v['after']]
    missing = sorted({r['label'] for r in old_table if r['id'] not in new_map or
                      set(r['values']) - set(new_map[r['id']]['values'])})
    return result | {'diff': diff, 'excluded': excluded, 'missing': missing}
