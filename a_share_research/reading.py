"""Read-only discovery and explicit report comparisons; never submits research."""
import hashlib
import json
import re
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from threading import Lock

from a_share_research import charts, db, service

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

    def period_conflict(*records):
        # Old T.* rows inherit the generator's cumulative/consolidated contract.
        # Explicit contrary (or unrecognised) metadata must never be overridden by a date.
        for record in records:
            for metadata in (record, mapping(record, 'source')):
                kind, report_type = metadata.get('period_type'), metadata.get('report_type')
                if kind is not None and kind != 'cumulative':
                    return True
                if report_type is not None and (type(report_type) not in (str, int) or str(report_type) != '1'):
                    return True
        return False

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
        if period_conflict(row):
            return result | {'reason': '显式期间或报表类型与累计合并口径不一致或无法识别'}
        unavailable = mapping(row, 'unavailable')
        if period in unavailable:
            reason = unavailable[period]
            return result | {'reason': reason if isinstance(reason, str) and reason.strip()
                             else '该期间已标记不可用，原因未记录'}
        value = mapping(row, 'values').get(period)
        if number(value) is None:
            return result | {'reason': '该共同期间未记录有效数值'}
        period_note = ('原行明确记录累计期间类型；本页未重新核对原始资料。'
                       if row.get('period_type') == 'cumulative' or source.get('period_type') == 'cumulative'
                       else '累计口径沿用生成时契约，原行未保存独立期间类型。')
        return result | {'value': value, 'reason': '', 'source': source, 'label': row_label, 'period_note': period_note}

    def ratio(cells, period):
        result = {'value': None, 'reason': '未记录可确切映射的同期间程序计算'}
        profit, cash = cells
        if profit['value'] is not None and number(profit['value']) <= 0:
            return result | {'reason': '归母净利润为零，比率不适用' if number(profit['value']) == 0
                             else '归母净利润为负，比率不适用；保留原金额供核对'}
        if any(c['value'] is None for c in cells):
            return result | {'reason': '计算所需的同口径原值不完整'}
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
                   or v.get('unit') != '亿元' or v.get('value_field', 'values') not in ('values', 'exact_values')
                   for v, c in zip(operands, (cash, profit))):
                continue
            expected_inputs = [number(mapping(indexed[c['ref']][0], v.get('value_field', 'values')).get(period))
                               for v, c in zip(operands, (cash, profit))]
            if any(expected is None or number(v.get('value')) != expected
                   for v, expected in zip(operands, expected_inputs)):
                continue
            if period_conflict(row, calculation, *operands):
                continue
            normalized = calculation.get('normalized_inputs')
            if normalized is not None and (not isinstance(normalized, list) or len(normalized) != 2 or
                    any(not isinstance(v, dict) or v.get('unit') != '亿元' or number(v.get('value')) != expected
                        for v, expected in zip(normalized, expected_inputs))):
                continue
            if normalized is not None and period_conflict(*normalized):
                continue
            value = mapping(row, 'values').get('result')
            try:
                expected = (expected_inputs[0] / expected_inputs[1]).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
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
        entry = item | {'period': period, 'rows': rows, 'ratio_missing': not any(c['value'] is not None for c in calculations),
                        'ratio_reasons': calculations}
        snapshots.append(entry | {'chart': charts.peer_bars(entry)})
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
        " AND u.replay_of IS NULL"
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


def evidence_ledger(content: dict, rows: dict, evidence: dict) -> dict:
    """Counts of what a report shows: its items, distinct quoted passages and program calculations."""
    quotes, calculations = set(), set()
    for key in ('answer', 'risks', 'business', 'financial', 'checkpoints', 'methods'):
        for item in content.get(key) or []:
            for cite in item.get('cites') or [] if isinstance(item, dict) else []:
                if cite.get('ref'):
                    if (rows.get(cite['ref']) or {}).get('calculation'):
                        calculations.add(cite['ref'])
                elif cite.get('quote'):
                    quotes.add((cite.get('source') or 'D0', cite.get('page'), cite['quote']))
    return {'risks': len(content.get('risks') or []), 'checkpoints': len(content.get('checkpoints') or []),
            'quotes': len(quotes), 'calculations': len(calculations), 'gaps': len(evidence.get('gaps') or [])}


def latest_reports(conn, *, query: str = "", codes: list[str] | None = None,
                   passed: bool = False, since: str | None = None, code_prefix: str | None = None,
                   limit: int = -1, offset: int = 0, include_checkpoint: bool = True) -> list:
    """Newest non-replay report per company among eligible reports, with bounded summaries.

    Search matches any historical name/code literally; source counts exclude replays.
    Filters apply before selecting the newest report, including passed/date constraints.
    """
    where, params = ["u.replay_of IS NULL"], []
    if codes is not None:
        if not codes:
            return []
        where.append(f"p.code IN ({','.join('?' for _ in codes)})")
        params.extend(codes)
    if passed:
        where.append("p.passed = 1")
    if since is not None:
        where.append("p.created_at >= ?")
        params.append(since)
    if code_prefix is not None:
        where.append("p.code LIKE ?")
        params.append(code_prefix[:6] + ".%")
    checkpoint = "json_extract(r.body, '$.checkpoints[0].text')" if include_checkpoint else "NULL"
    return conn.execute(
        "WITH latest AS (SELECT p.code, MAX(p.id) AS id,"
        " SUM(COALESCE(u.request_id, '') NOT LIKE 'batch-%') AS own"
        " FROM reports p JOIN runs u ON u.id = p.run_id WHERE " + " AND ".join(where) +
        " GROUP BY p.code HAVING MAX(instr(lower(COALESCE(p.name, '')), lower(?)) > 0"
        " OR instr(lower(p.code), lower(?)) > 0)),"
        " selected AS (SELECT * FROM latest ORDER BY id DESC LIMIT ? OFFSET ?)"
        " SELECT r.id, r.code, r.name, r.passed, r.verdict, r.created_at, s.own,"
        f" {checkpoint} AS checkpoint,"
        " COALESCE(u.request_id, '') LIKE 'batch-%' AS from_batch, r.coverage"
        " FROM selected s JOIN reports r ON r.id = s.id"
        " JOIN runs u ON u.id = r.run_id ORDER BY r.id DESC",
        (*params, query, query, limit, offset),
    ).fetchall()


def search_reports(conn, query: str, page: int = 1, *, page_size: int = PAGE_SIZE) -> tuple[list, bool]:
    rows = latest_reports(conn, query=query, limit=page_size + 1, offset=(page - 1) * page_size)
    return rows[:page_size], len(rows) > page_size


def recent_report(conn, code: str):
    rows = latest_reports(conn, code_prefix=code, passed=True,
                          since=datetime.now(db.SHANGHAI).date().isoformat(), limit=1)
    return report(conn, rows[0]["id"]) if rows else None


def report_bodies(conn, ids: list[int]) -> dict:
    if not ids:
        return {}
    return {r["id"]: json.loads(r["body"]) for r in conn.execute(
        f"SELECT id, body FROM reports WHERE id IN ({','.join('?' for _ in ids)})", ids)}


_CARD_CACHE_SIZE = 256
_card_cache: OrderedDict[tuple, dict] = OrderedDict()
_card_cache_lock = Lock()


def report_cards(conn, reports: list, passed_before: dict) -> list[dict]:
    """Share compact cards across connections; body edits must also update body_sha256."""
    if not reports:
        return []
    cursor = conn.execute('PRAGMA database_list')
    db_path = next(row['file'] for row in cursor if row['name'] == 'main')
    # In-memory databases have no stable path; uncommitted changes stay out of the cache.
    cacheable = bool(db_path) and not cursor.connection.in_transaction
    ids = [report['id'] for report in reports]
    hashes = dict(conn.execute(
        f"SELECT id, body_sha256 FROM reports WHERE id IN ({','.join('?' for _ in ids)})", ids))
    keys, resolved = {}, {}
    for report in reports:
        metadata = tuple((k, report[k]) for k in
                         ('code', 'name', 'passed', 'verdict', 'created_at', 'coverage'))
        key = (db_path, report['id'], hashes[report['id']], metadata)
        keys[report['id']] = key
        if cacheable and key[2]:
            with _card_cache_lock:
                if key in _card_cache:
                    resolved[report['id']] = _card_cache[key]
                    _card_cache.move_to_end(key)

    missing = [report_id for report_id in ids if report_id not in resolved]
    if missing:
        rows = conn.execute(
            f"SELECT id, body FROM reports WHERE id IN ({','.join('?' for _ in missing)})", missing)
        for row in rows:
            key = keys[row['id']]
            item = card(dict(key[3]) | {'id': row['id']}, json.loads(row['body']))
            resolved[row['id']] = item
            # Never cache legacy bodies without hashes or a body changed between the two reads.
            if cacheable and key[2] and hashlib.sha256(row['body'].encode()).hexdigest() == key[2]:
                with _card_cache_lock:
                    _card_cache[key] = item
                    _card_cache.move_to_end(key)
                    while len(_card_cache) > _CARD_CACHE_SIZE:
                        _card_cache.popitem(last=False)

    cards = []
    for report in reports:
        item = deepcopy(resolved[report['id']])
        previous = passed_before.get(report['code']) if not report['passed'] else None
        item['last_passed'] = dict(previous) if previous is not None else None
        cards.append(item | {'from_batch': report['from_batch']})
    return cards


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
    diff = since_prior(old_table, new_table)
    # Empty market snapshots are missing observations, not comparable values.
    diff['valuation'] = [v for v in diff['valuation'] if v['before'] and v['after']]
    missing = sorted({r['label'] for r in old_table if r['id'] not in new_map or
                      set(r['values']) - set(new_map[r['id']]['values'])})
    return result | {'diff': diff, 'excluded': excluded, 'missing': missing}


# Card check-up: saved financial-table rows grouped by what they tell a reader. No scores, no
# derived numbers: each metric is the saved latest value beside the saved value a year earlier.
CARD_DIMENSIONS = (
    ('成长', (('T.revenue', '营业总收入'), ('T.revenue_yoy', '营收同比'), ('T.core_profit_yoy', '扣非同比'))),
    ('盈利', (('T.core_profit', '扣非净利润'), ('T.gross_margin', '毛利率'), ('T.roe', '加权ROE'))),
    ('现金', (('T.net_profit', '归母净利润'), ('T.op_cash', '经营现金流'), ('T.fcf', '经营现金流减购建长期资产'))),
    ('健康', (('T.debt_ratio', '资产负债率'), ('T.cash', '货币资金'), ('T.interest_debt', '四项债务'))),
)
# Saved closing-day valuation, never refreshed.
CARD_MARKET = (('M.pe_ttm', '市盈率'), ('M.pb', '市净率'), ('M.dv_ttm', '股息率'))
CARD_KEYS = ('T.revenue_yoy', 'T.core_profit_yoy', 'M.pe_ttm', 'M.dv_ttm')
CARD_ORDER = {'值得继续研究': 0, '需要谨慎': 1, '暂不值得': 2}


def card(report, body: dict, last_passed=None) -> dict:
    """One company's investment card, read from its latest saved report; nothing is fetched or recomputed.

    A report that failed review shows no verdict, summary, risks, checkpoints or figures. The last
    passed report, when the caller supplies one, is named with its own date rather than standing in.
    """
    report = dict(report)  # sqlite3.Row has no .get
    passed = bool(report['passed'])
    verdict = report['verdict'] if passed and report['verdict'] in charts.VERDICTS else None
    summary = (body.get('summary') or '').strip() if passed else ''
    # Summaries often open with the verdict the card already shows on its scale.
    if verdict and summary[len(verdict):len(verdict) + 1] in ('：', ':', '，') and summary.startswith(verdict):
        summary = summary[len(verdict) + 1:].lstrip()

    def texts(key):
        return [i['text'] for i in body.get(key) or []
                if isinstance(i, dict) and isinstance(i.get('text'), str) and i['text'].strip()] if passed else []

    table = {r['id']: r for r in body.get('table') or [] if isinstance(r, dict) and isinstance(r.get('id'), str)}
    periods = {p for k, r in table.items() if k.startswith('T.') for p in r.get('values') or {}}
    latest = max(periods) if periods else None
    prior = (f"{int(latest[:4]) - 1}{latest[4:]}"
             if latest and len(latest) >= 4 and latest[:4].isdigit() else None)
    day = body.get('market_date')
    metrics = {}
    for key, label in [m for _, group in CARD_DIMENSIONS for m in group] + list(CARD_MARKET):
        row = table.get(key)
        at, before = (day, None) if key.startswith('M.') else (latest, prior)
        if not (passed and row and at):
            continue
        values = row.get('values') or {}
        metrics[key] = {'label': label, 'unit': row.get('unit') or '', 'value': values.get(at), 'signed': key.endswith('_yoy'),
                        'unavailable': at in (row.get('unavailable') or {}),
                        'prior': values.get(before) if before else None}
    dims = [{'label': label, 'metrics': [metrics[k] for k, _ in group if k in metrics]}
            for label, group in CARD_DIMENSIONS]
    dims.append({'label': '估值', 'metrics': [metrics[k] for k, _ in CARD_MARKET if k in metrics]})
    dims = [d for d in dims if any(m['value'] is not None or m['unavailable'] for m in d['metrics'])]
    bucket = verdict or '复核未通过'  # the same buckets as the home verdict strip
    created_at = report.get('created_at') or ''
    coverage = report.get('coverage') or (latest or report.get('period'))
    period_label = (f"{latest[2:4]}年{charts.CUMULATIVE.get(latest[5:], latest[5:])}"
                    if latest and len(latest) >= 7 and latest[:4].isdigit() else (latest or None))
    return {'id': report['id'], 'code': report['code'], 'name': report['name'], 'passed': passed,
            'verdict': verdict, 'bucket': bucket, 'rank': CARD_ORDER.get(bucket, 3), 'summary': summary,
            'risks': texts('risks'), 'checkpoints': texts('checkpoints'), 'dims': dims,
            'figures': [metrics[k] for k in CARD_KEYS if k in metrics],
            'period': period_label, 'created_at': created_at, 'coverage': coverage,
            'market_date': day, 'last_passed': dict(last_passed) if last_passed else None}


VALUATION = ("M.pe_ttm", "M.pb", "M.dv_ttm")


def since_prior(old: list[dict], new: list[dict]) -> dict:
    """What changed in the program tables between two reports; shown to people, never cited by the model.

    Values are compared only within the same row and period: cumulative figures of different periods are
    not subtracted, and a row the old report never collected is listed rather than treated as zero.
    """
    before = {r["id"]: r["values"] for r in old}
    rows = [r for r in new if r["id"].startswith("T.")]
    seen = {p for r in old if r["id"].startswith("T.") for p in r["values"]}
    added = sorted({p for r in rows for p in r["values"]} - seen)
    newest = max(seen, default="")
    return {
        "disclosed": [p for p in added if p > newest],
        "backfilled": [p for p in added if p <= newest],  # comparison periods, not new disclosures
        "revised": [{"label": r["label"], "unit": r["unit"], "period": p, "before": before[r["id"]][p], "after": v}
                    for r in rows if r["id"] in before for p, v in r["values"].items()
                    if p in before[r["id"]] and before[r["id"]][p] != v],
        # Rows usually stay in the table with an empty value, so absence is per row and period.
        "uncollected": [r["label"] for r in new
                        if (r["id"].startswith("T.") and any(p in seen and p not in before.get(r["id"], {}) for p in r["values"]))
                        or (r["id"] in VALUATION and r["values"] and r["id"] not in before)],
        "valuation": [{"label": r["label"], "unit": r["unit"], "before": next(iter(before[r["id"]].items()), None),
                       "after": next(iter(r["values"].items()), None), "unchanged": before[r["id"]] == r["values"]}
                      for r in new if r["id"] in VALUATION and r["id"] in before],
    }


def research_state(conn, q20: list[dict]) -> tuple[bool, list[dict]]:
    """(all finished, rows with current status/report) for the linked runs."""
    done = True
    for row in q20:
        run = conn.execute("SELECT u.status, u.error, r.id AS report_id, r.passed, r.verdict, r.body"
                           " FROM runs u LEFT JOIN reports r ON r.run_id = u.id WHERE u.id = ?",
                           (row["run_id"],)).fetchone()
        row["research_status"] = run["status"]
        row["report_id"] = run["report_id"]
        row["verdict"] = run["verdict"] if run["passed"] else None
        row["research_error"] = run["error"]
        if run["status"] in ("queued", "running"):
            done = False
    return done, q20


def active_runs(conn) -> list:
    return conn.execute(
        "SELECT id, code, name, status, step FROM runs WHERE status IN ('queued', 'running') ORDER BY id"
    ).fetchall()


def latest_price(conn, code: str) -> dict | None:
    row = conn.execute("SELECT body FROM prices WHERE code = ? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    if row is None:
        return None
    body = json.loads(row["body"])
    # Levels are only shown while valid; afterwards the reference is history, not an action line.
    body["expired"] = datetime.now(db.SHANGHAI).date().isoformat() > body["expires"]
    body["ladder"] = charts.price_ladder(body["levels"])
    return body


def latest_batch(conn):
    return conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1").fetchone()


def run(conn, run_id: int):
    return conn.execute(
        "SELECT id, kind, code, name, status, step, error, started_at, finished_at, request, replay_of FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()


def report_for_run(conn, run_id: int):
    return conn.execute("SELECT id FROM reports WHERE run_id = ?", (run_id,)).fetchone()


def queue_ahead(conn, run_id: int):
    return conn.execute(
        "SELECT COUNT(*) FROM runs u, (SELECT request_id LIKE 'batch-%' AS batch FROM runs WHERE id = ?) me"
        " WHERE u.request_id IS NOT NULL AND u.id != ? AND (u.status = 'running' OR (u.status = 'queued' AND"
        " ((u.request_id LIKE 'batch-%') < me.batch OR ((u.request_id LIKE 'batch-%') = me.batch AND u.id < ?))))",
        (run_id, run_id, run_id)).fetchone()


def report(conn, report_id: int):
    return conn.execute("SELECT r.* FROM reports r WHERE id = ?", (report_id,)).fetchone()


def report_run(conn, run_id: int):
    return conn.execute("SELECT kind, status, request, identity, replay_of, material FROM runs WHERE id = ?", (run_id,)).fetchone()


def followups(conn, code: str, report_id: int):
    return conn.execute(
        "SELECT u.id, u.status, COALESCE(NULLIF(json_extract(u.request, '$.question'), ''),"
        " json_extract(f.body, '$.question')) AS question,"
        " json_extract(u.request, '$.feedback.text') AS feedback_text,"
        " json_extract(f.body, '$.prior.report_id') AS prior_report_id"
        " FROM runs u LEFT JOIN reports f ON f.run_id = u.id"
        " WHERE u.code = ? AND (json_extract(u.request, '$.parent_report_id') = ? OR"
        " (f.code = ? AND (json_extract(f.body, '$.parent_report_id') = ? OR"
        " json_extract(f.body, '$.prior.report_id') = ?))) ORDER BY u.id DESC",
        (code, report_id, code, report_id, report_id)).fetchall()


def request_run(conn, request_id: str):
    return conn.execute("SELECT id FROM runs WHERE request_id = ?", (request_id,)).fetchone()


def api_run(conn, run_id: int):
    return conn.execute("SELECT u.id, u.code, u.name, u.status, u.step, u.error, u.created_at, u.finished_at,"
        " r.id AS report_id FROM runs u LEFT JOIN reports r ON r.run_id = u.id WHERE u.id = ?", (run_id,)).fetchone()


def api_reports(conn, code: str):
    return conn.execute("SELECT r.id, r.run_id, r.code, r.name, r.created_at, r.period, r.passed, u.status"
        " FROM reports r JOIN runs u ON u.id = r.run_id WHERE substr(r.code, 1, 6) = ?"
        " ORDER BY r.id DESC LIMIT 100", (code,)).fetchall()


def stock_reports(conn, code: str):
    return conn.execute(
        "SELECT r.*, u.replay_of AS replay_of,"
        " COALESCE(NULLIF(json_extract(u.request, '$.question'), ''), json_extract(r.body, '$.question')) AS question,"
        " COALESCE(json_extract(u.request, '$.parent_report_id'), json_extract(r.body, '$.parent_report_id')) AS parent_report_id,"
        " json_extract(u.request, '$.feedback.text') AS feedback_text,"
        " json_extract(r.body, '$.prior.report_id') AS prior_report_id FROM reports r"
        " JOIN runs u ON u.id = r.run_id WHERE r.code = ? ORDER BY r.id DESC", (code,),
    ).fetchall()


def batch_list(conn):
    return conn.execute("SELECT id, status, error, created_at, as_of, l5, frozen_at FROM batches ORDER BY id DESC").fetchall()


def batch(conn, batch_id: int):
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
