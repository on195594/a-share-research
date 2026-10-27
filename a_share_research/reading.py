"""Read-only discovery and explicit report comparisons; never submits research."""
import json

from a_share_research import db, research, service

PAGE_SIZE = 20


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
        return {'reason': '关联原报告记录不一致，无法可靠对照。'}
    parent = next(iter(parents), None) or (body.get('prior') or {}).get('report_id')
    if not isinstance(parent, int) or parent == report['id']:
        return {'reason': '未记录明确关联的原报告，不能按研究时间自动配对。'}
    old = conn.execute('SELECT r.*, u.request, u.status FROM reports r JOIN runs u ON u.id = r.run_id'
                       ' WHERE r.id = ? AND r.code = ?', (parent, report['code'])).fetchone()
    if old is None:
        return {'reason': '关联原报告不存在或不属于同一家公司，无法对照。'}
    before = json.loads(old['body'])
    previous_request = json.loads(old['request'] or '{}') or {}
    result = {'old': dict(old), 'new': dict(report),
              'old_question': previous_request.get('question') or before.get('question'),
              'new_question': request.get('question') or body.get('question'),
              'old_document': before.get('document'), 'new_document': body.get('document'),
              'old_ready': service.ready(old['status'], old['passed']),
              'new_ready': bool(run and service.ready(run['status'], report['passed']))}
    if not run or not service.ready(run['status'], report['passed']) or not service.ready(old['status'], old['passed']):
        return result | {'reason': '至少一份报告尚未就绪，暂不能对照。'}
    old_rows, new_rows = before.get('table'), body.get('table')
    if not old_rows or not new_rows:
        return result | {'reason': '至少一份报告未记录财务表，无法计算指标变化。'}
    # K2's deterministic comparison assumes identical row semantics. Exclude mismatched
    # units/labels and incomplete legacy rows instead of comparing unlike quantities.
    def valid(row):
        return all(row.get(k) for k in ('id', 'label', 'unit')) and isinstance(row.get('values'), dict)
    old_map = {r['id']: r for r in old_rows if valid(r)}
    new_map = {r['id']: r for r in new_rows if valid(r)}
    incompatible = {key for key in old_map.keys() & new_map.keys()
                    if any(old_map[key][k] != new_map[key][k] for k in ('unit', 'label'))}
    excluded = sorted({r.get('label') or r.get('id', '未知指标') for r in old_rows + new_rows
                       if not valid(r) or r.get('id') in incompatible})
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
