"""Read-only discovery and explicit report comparisons; never submits research."""
import json
import re
from datetime import date
from decimal import Decimal, DecimalException, ROUND_HALF_UP

from a_share_research import db, research, service

PAGE_SIZE = 20


def peer_snapshots(body: dict) -> list[dict]:
    """Read saved cumulative snapshots only; never infer peers or produce values.

    The two known T.* definitions are deliberately narrower than financial_table.
    A C.* ratio must map to those exact saved operands, not a model-written label.
    """
    metrics = (('net_profit', '归母净利润'), ('op_cash', '经营活动现金流净额'))
    table = [r for r in (body.get('table') or []) if isinstance(r, dict)]
    indexed = {}
    for row in table:
        if isinstance(row.get('id'), str):
            indexed.setdefault(row['id'], []).append(row)

    def code(value):
        match = re.fullmatch(r'(\d{6})(?:\.(?:SH|SZ|BJ))?', str(value))
        return match[1] if match else None

    def number(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = Decimal(value)
            return parsed if parsed.is_finite() else None
        except DecimalException:
            return None

    def mapping(row, key):
        return row[key] if isinstance(row.get(key), dict) else {}

    def periods(prefix):
        result = set()
        for key, _ in metrics:
            for row in indexed.get(prefix + 'T.' + key, []):
                for period in mapping(row, 'values').keys() | mapping(row, 'unavailable').keys():
                    if isinstance(period, str) and len(period) == 10 and period[5:] in ('03-31', '06-30', '09-30', '12-31'):
                        try:
                            result.add(date.fromisoformat(period).isoformat())
                        except ValueError:
                            pass  # An unknown period cannot become a comparison date.
        return result

    def cell(prefix, company, key, label, period):
        ref = prefix + 'T.' + key
        candidates = indexed.get(ref, [])
        result = {'ref': ref, 'value': None, 'reason': '未记录该指标'}
        if len(candidates) != 1:
            return result | {'reason': '记录标识重复，无法确定口径' if candidates else result['reason']}
        row = candidates[0]
        row_label = row.get('label', '')
        known_label = (isinstance(row_label, str) and
                       (row_label == label if not prefix else row_label.endswith('·' + label) and row_label != '·' + label))
        if not known_label or row.get('unit') != '亿元':
            return result | {'reason': '指标定义或单位不一致'}
        source = row.get('source')
        if not isinstance(source, dict) or source.get('provider') != 'TuShare' or code(source.get('code')) != company:
            return result | {'reason': '来源或公司标识不完整/不一致'}
        if mapping(row, 'unavailable').get(period):
            return result | {'reason': row['unavailable'][period]}
        value = mapping(row, 'values').get(period)
        if number(value) is None:
            return result | {'reason': '该共同期间未记录有效数值'}
        return result | {'value': value, 'reason': '', 'source': source, 'label': row_label}

    def ratio(cells, period):
        result = {'value': None, 'reason': '未记录可确切映射的同期间程序计算'}
        if any(c['value'] is None for c in cells):
            return result | {'reason': '计算所需的同口径原值不完整'}
        profit, cash = cells
        if number(profit['value']) == 0:
            return result | {'reason': '归母净利润为零，比率不适用'}
        matches = []
        for row in table:
            calculation = row.get('calculation')
            if (not isinstance(row.get('id'), str) or not row['id'].startswith('C.')
                    or len(indexed[row['id']]) != 1 or not isinstance(calculation, dict)):
                continue
            operands = calculation.get('inputs')
            if (calculation.get('operation') != 'ratio' or calculation.get('formula') != 'left / right'
                    or row.get('unit') != '倍' or not isinstance(operands, list) or len(operands) != 2):
                continue
            if any(not isinstance(v, dict) or v.get('ref') != c['ref'] or v.get('period') != period
                   or v.get('unit') != '亿元' or number(v.get('value')) != number(c['value'])
                   for v, c in zip(operands, (cash, profit))):
                continue
            normalized = calculation.get('normalized_inputs')
            if normalized is not None and (not isinstance(normalized, list) or len(normalized) != 2 or
                    any(not isinstance(v, dict) or v.get('unit') != '亿元' or number(v.get('value')) != number(c['value'])
                        for v, c in zip(normalized, (cash, profit)))):
                continue
            value = mapping(row, 'values').get('result')
            try:
                expected = (number(cash['value']) / number(profit['value'])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            except DecimalException:
                continue
            if number(value) == expected:
                matches.append(result | {'value': value, 'reason': '', 'ref': row['id'],
                                         'calculation': calculation})
        return matches[0] if len(matches) == 1 else result | {
            'reason': '存在多条匹配计算，未自动选择' if matches else result['reason']}

    subject = code(body.get('code'))
    if not subject:
        return []
    peers = list(dict.fromkeys(m[1] for ref in indexed if (m := re.fullmatch(r'P(\d{6})\.T\..+', ref)) and m[1] != subject))
    snapshots = []
    for peer in peers:
        prefix = f'P{peer}.'
        names = {r['label'].rsplit('·', 1)[0] for key, label in metrics
                 for r in indexed.get(prefix + 'T.' + key, [])
                 if isinstance(r.get('label'), str) and r['label'].endswith('·' + label)}
        name = next(iter(names)) if len(names) == 1 else peer
        item = {'name': name, 'code': peer, 'subject_name': body.get('name') or subject, 'subject_code': subject}
        common = periods('') & periods(prefix)
        if not common:
            snapshots.append(item | {'reason': '未记录可对齐的共同累计期间。', 'rows': []})
            continue
        period = max(common)
        rows = [{'label': label, 'unit': '亿元', 'cells': [cell('', subject, key, label, period),
                                                        cell(prefix, peer, key, label, period)]}
                for key, label in metrics]
        comparable = any(all(c['value'] is not None for c in row['cells']) for row in rows)
        # A lone company value is not a comparison. Keep the reasons, not an empty numeric table.
        if not comparable:
            reasons = list(dict.fromkeys(c['reason'] for row in rows for c in row['cells'] if c['reason']))
            snapshots.append(item | {'period': period, 'reason': '；'.join(reasons), 'rows': []})
            continue
        calculations = [ratio([r['cells'][i] for r in rows], period) for i in (0, 1)]
        if any(c['value'] is not None for c in calculations):
            rows.append({'label': '合并经营现金流 / 归母净利润', 'unit': '倍', 'calculated': True, 'cells': calculations})
        snapshots.append(item | {'period': period, 'rows': rows, 'ratio_missing': not any(c['value'] is not None for c in calculations)})
    return snapshots


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
