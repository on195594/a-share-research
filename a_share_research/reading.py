"""Read-only discovery and explicit report comparisons; never submits research."""
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, ROUND_HALF_UP

from a_share_research import report as report_body
from a_share_research import batches, charts, db, service, stance as stances
from a_share_research import disclosure_checks, stock_codes

PAGE_SIZE = 20
RECENT_FAILURE_DAYS = 30


def stance_decision(stance: dict | None, stance_calc: dict | None, valuation_calc: dict | None) -> dict | None:
    """Derive safe, deterministic display view for the stance and price barrier.

    Pure function: never throws, guarantees valid numeric operations, returns clean dict.
    """
    if not isinstance(stance, dict) or not stance.get("action"):
        return None
    action = stance["action"]
    out = {
        "action": action,
        "text": stance.get("text", ""),
        "cites": stance.get("cites") if isinstance(stance.get("cites"), list) else [],
        "has_prices": False,
        "close": None,
        "hurdle": None,
        "diff_pct": None,
        "valid_until": None,
        "annual_return": None,
        "zone": None,
        "add_trigger": stance.get("add_trigger") if action == "现在分批" and isinstance(stance.get("add_trigger"), dict) else None,
        "trigger": stance.get("trigger") if action == "等证据" and isinstance(stance.get("trigger"), dict) else None,
    }

    sc = stance_calc if isinstance(stance_calc, dict) else {}
    vc = valuation_calc if isinstance(valuation_calc, dict) else {}
    v2 = vc.get("v2") if isinstance(vc.get("v2"), dict) else {}

    close = vc.get("close")
    hurdle = sc.get("price")
    if hurdle is None:
        hurdle = v2.get("support_below")

    if isinstance(close, (int, float)) and close > 0 and isinstance(hurdle, (int, float)) and hurdle > 0:
        out["has_prices"] = True
        out["close"] = round(float(close), 2)
        out["hurdle"] = round(float(hurdle), 2)
        if action == "等价格" and close > hurdle:
            out["diff_pct"] = round((close - hurdle) / close * 100, 1)
        elif close <= hurdle:
            out["diff_pct"] = round((hurdle - close) / hurdle * 100, 1)

    if action == "等价格":
        out["valid_until"] = sc.get("valid_until")

    if isinstance(v2.get("annual_return"), (int, float)):
        out["annual_return"] = round(float(v2["annual_return"]) * 100, 1)

    out["zone"] = v2.get("zone") or vc.get("zone")
    out["boundary"] = stances.on_boundary(vc) is True
    out["flip_distance"] = stances.flip_distance(vc)
    return out



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
        match = stock_codes.CODE_RE.fullmatch(str(value))
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
        " FROM runs u JOIN reports p ON p.id = u.parent_report_id"
        " JOIN runs original ON original.id = p.run_id"
        " LEFT JOIN reports f ON f.run_id = u.id AND f.company_key = p.company_key"
        " WHERE u.kind = 'research' AND u.company_key = p.company_key"
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
               'financial': '#financial', 'checkpoints': '#checkpoints', 'prior_status': '#prior-status'}
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


def latest_reports(conn, *, query: str = "", codes: list[str] | None = None,
                   passed: bool = False, since: str | None = None, code_prefix: str | None = None,
                   limit: int = -1, offset: int = 0, include_checkpoint: bool = True) -> list:
    """Newest non-replay blind report per company among eligible reports, with bounded summaries.

    Search matches any historical name/code literally; source counts exclude replays.
    Filters apply before selecting the newest report, including passed/date constraints.
    """
    where, params = [db.RESEARCH_BLIND], []
    if codes is not None:
        if not codes:
            return []
        keys = []
        for code in codes:
            try:
                keys.append(stock_codes.normalize_key(code))
            except ValueError:
                continue
        if not keys:
            return []
        where.append(f"p.company_key IN ({','.join('?' for _ in keys)})")
        params.extend(keys)
    if passed:
        where.append("p.passed = 1")
    if since is not None:
        where.append("p.created_at >= ?")
        params.append(since)
    if code_prefix is not None:
        try:
            key = stock_codes.normalize_key(code_prefix)
        except ValueError:
            return []
        where.append("p.company_key = ?")
        params.append(key)
    checkpoint = "json_extract(r.body, '$.checkpoints[0].text')" if include_checkpoint else "NULL"
    return conn.execute(
        "WITH latest AS (SELECT p.code, MAX(p.id) AS id,"
        " SUM(u.batch_id IS NULL) AS own"
        " FROM reports p JOIN runs u ON u.id = p.run_id WHERE " + " AND ".join(where) +
        " GROUP BY p.company_key HAVING MAX(instr(lower(COALESCE(p.name, '')), lower(?)) > 0"
        " OR instr(lower(p.code), lower(?)) > 0)),"
        " selected AS (SELECT * FROM latest ORDER BY id DESC LIMIT ? OFFSET ?)"
        " SELECT r.id, r.code, r.name, r.passed, r.verdict, r.created_at, s.own,"
        f" {checkpoint} AS checkpoint,"
        " u.batch_id IS NOT NULL AS from_batch, r.coverage"
        " FROM selected s JOIN reports r ON r.id = s.id"
        " JOIN runs u ON u.id = r.run_id ORDER BY r.id DESC",
        (*params, query, query, limit, offset),
    ).fetchall()


def search_reports(conn, query: str, page: int = 1, *, page_size: int = PAGE_SIZE) -> tuple[list, bool]:
    rows = latest_reports(conn, query=query, limit=page_size + 1, offset=(page - 1) * page_size)
    return rows[:page_size], len(rows) > page_size


SEARCH_SECTIONS = {'risks': '风险与反证', 'checkpoints': '下期检查点', 'answer': '问题回答', 'business': '业务',
                   'financial': '财务', 'stance': '研究立场', 'prior_status': '旧议程状态'}
SEARCH_MAX_TERMS = 5
SEARCH_MAX_LENGTH = 100
SEARCH_REPORT_BATCH_SIZE = 8


def search_items(conn, query: str, *, code: str | None = None, section: str | None = None, history: bool = False,
                 page: int = 1, page_size: int = PAGE_SIZE) -> tuple[list[dict], bool]:
    """Literal AND search across report items, newest report first, scanning stored JSON.

    Each company's report is chosen first, by the company card's rule, and only then matched: a term found
    only in an older report does not surface unless `history` asks for every report.
    shortcut: no index; revisit if the measured production P95 exceeds one second.
    """
    query = query.strip()
    terms = [term.casefold() for term in query.split()]
    if not terms or len(query) > SEARCH_MAX_LENGTH or len(terms) > SEARCH_MAX_TERMS:
        raise service.RequestError(f"查询须为 1–{SEARCH_MAX_LENGTH} 字、最多 {SEARCH_MAX_TERMS} 个词")
    if code is not None:
        try:
            code = stock_codes.normalize_key(code)
        except ValueError as exc:
            raise service.RequestError("公司代码必须为六位数字") from exc
    if section is not None and section not in SEARCH_SECTIONS:
        raise service.RequestError("章节仅限：" + "、".join(SEARCH_SECTIONS))
    # Select lightweight metadata first: extracting JSON before sorting/fetchall would parse every
    # candidate even when the first few reports already fill the requested page.
    paths = ", ".join("'$.stance.text'" if name == "stance" else f"'$.{name}'" for name in SEARCH_SECTIONS)
    rows = conn.execute(
        "SELECT c.id, c.code, c.name, c.created_at, c.coverage FROM ("
        " SELECT r.id, r.code, r.name, r.created_at, r.coverage, ROW_NUMBER() OVER ("
        "  PARTITION BY r.company_key ORDER BY r.created_at DESC, r.id DESC) AS position"
        " FROM reports r JOIN runs u ON u.id = r.run_id"
        f" WHERE {db.BLIND_FORMAL_REPORT}"
        " AND (? IS NULL OR r.company_key = ?)) c"
        " WHERE (? OR c.position = 1) ORDER BY c.created_at DESC, c.id DESC",
        (code, code, int(history)),
    ).fetchall()
    wanted, matched = (page - 1) * page_size + page_size + 1, []
    for offset in range(0, len(rows), SEARCH_REPORT_BATCH_SIZE):
        batch = rows[offset:offset + SEARCH_REPORT_BATCH_SIZE]
        bodies = {row['id']: row['found'] for row in conn.execute(
            "SELECT id, json_extract(body, " + paths + ") AS found FROM reports WHERE id IN ("
            + ",".join("?" for _ in batch) + ")", [row['id'] for row in batch])}
        # IN does not guarantee order; retain the metadata query's stable report order.
        for row in batch:
            found = bodies.get(row['id'])
            if found is None:  # A maintenance deletion may occur between autocommit reads.
                continue
            for name, value in zip(SEARCH_SECTIONS, json.loads(found)):
                if not value or section not in (None, name):
                    continue
                entries = [(name, value)] if name == 'stance' else [(f"{name}[{i}]", item['text']) for i, item in enumerate(value)]
                for location, text in entries:
                    if all(term in text.casefold() for term in terms):
                        matched.append({'report_id': row['id'], 'code': row['code'], 'name': row['name'],
                                        'created_at': row['created_at'], 'period': row['coverage'],
                                        'location': location, 'text': text})
            if len(matched) >= wanted:
                break
        if len(matched) >= wanted:
            break
    start = (page - 1) * page_size
    return matched[start:start + page_size], len(matched) > start + page_size


def item_link(report_id: int, location: str) -> str:
    """Where a search hit sits on the report page; ?claim= opens its fold and its sources."""
    match = re.fullmatch(r"([a-z_]+)\[(\d+)\]", location)
    if not match:
        return f"/reports/{report_id}#{location}"
    return f"/reports/{report_id}?claim={match[1]}:{match[2]}#claim-{match[1]}-{match[2]}"


def search_scope(conn, query: str) -> tuple | None:
    """The one company a multi-word query names, with the words left to look for inside its reports.

    Single characters and partial numbers never name a company: "铜 现金流" and "2025 下降" are content
    queries even when some company's name or code happens to contain them.
    """
    terms = query.split()
    if len(query) > SEARCH_MAX_LENGTH or not 2 <= len(terms) <= SEARCH_MAX_TERMS:
        return None
    named, rest = {}, []
    for term in terms:
        rows = [] if len(term) < 2 or (term.isdigit() and len(term) != 6) else latest_reports(
            conn, query=term, include_checkpoint=False, limit=2)
        if len(rows) == 1:
            named[stock_codes.normalize_key(rows[0]["code"])] = rows[0]
        else:
            rest.append(term)
    return (next(iter(named.values())), rest) if len(named) == 1 and rest else None


def library_results(conn, query: str, *, history: bool = False, whole_library: bool = False, page: int = 1) -> dict:
    """One research-library query: the companies it names and the report items containing it, paged together."""
    companies, more = search_reports(conn, query, page)
    results = {"companies": companies, "hits": [], "more": more, "scope_company": None, "error": ""}
    if not query:
        return results
    scope = None if companies or whole_library else search_scope(conn, query)
    if scope:
        results["scope_company"] = scope[0]
        results["companies"] = [scope[0]] if page == 1 else []
    try:
        hits, hits_more = search_items(conn, " ".join(scope[1]) if scope else query,
                                       code=stock_codes.normalize_key(scope[0]["code"]) if scope else None, history=history, page=page)
    except service.RequestError as exc:
        return results | {"error": str(exc)}
    if hits:
        codes = sorted({stock_codes.normalize_key(hit["code"]) for hit in hits})
        latest = {stock_codes.normalize_key(row["code"]): dict(row)
                  for row in latest_reports(conn, codes=codes, include_checkpoint=False)}
        results["hits"] = [hit | {"latest": latest.get(stock_codes.normalize_key(hit["code"])), "label": SEARCH_SECTIONS[hit["location"].split("[")[0]],
                                  "url": item_link(hit["report_id"], hit["location"])} for hit in hits]
    return results | {"more": more or hits_more}


def prior_agenda(conn, body: dict, previous: dict | None = None) -> list[dict]:
    """prior_status with each old item's wording, resolved against the immutable prior report."""
    entries = body.get('prior_status') or []
    if not entries:
        return []
    report_id = (body.get('prior') or {}).get('report_id')
    if previous is None:
        row = conn.execute("SELECT json_extract(body, '$.risks') AS risks, json_extract(body, '$.checkpoints') AS checkpoints"
                           " FROM reports WHERE id = ?", (report_id,)).fetchone()
        previous = {name: json.loads(row[name] or '[]') if row else [] for name in ('risks', 'checkpoints')}
    resolved = []
    for entry in entries:
        name, index = entry['ref'][:-1].split('[')
        items = previous.get(name) or []
        found = items[int(index)] if int(index) < len(items) else None
        resolved.append(entry | {'previous': found['text'] if found else None,
                                 'previous_label': f"{'旧风险' if name == 'risks' else '旧检查点'}第 {int(index) + 1} 条",
                                 'previous_url': item_link(report_id, entry['ref'])})
    return resolved


def recent_report(conn, code: str):
    rows = latest_reports(conn, code_prefix=code, passed=True,
                          since=datetime.now(db.SHANGHAI).date().isoformat(), limit=1)
    return report(conn, rows[0]["id"]) if rows else None


CARD_BODY_FIELDS = (
    'summary', 'risks', 'checkpoints', 'table', 'market_date', 'valuation_calc',
    'open_questions', 'stance', 'stance_calc', 'prior', 'prior_status',
)
_CARD_BODY_PATHS = ', '.join(f"'$.{field}'" for field in CARD_BODY_FIELDS)


def _card_bodies(conn, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    result = {}
    for row in conn.execute(
        f"SELECT id, json_extract(body, {_CARD_BODY_PATHS}) AS fields FROM reports WHERE id IN ({marks})",
        ids,
    ):
        values = json.loads(row['fields']) if row['fields'] is not None else []
        result[row['id']] = {k: v for k, v in zip(CARD_BODY_FIELDS, values) if v is not None}
    return result


def report_cards(conn, reports: list, passed_before: dict) -> list[dict]:
    """Compact home-page cards; a failed report also links the company's last passed one."""
    if not reports:
        return []
    from a_share_research import scans
    bodies = _card_bodies(conn, [report['id'] for report in reports])
    checks = scans.ScanHistory(conn, fields=('check_failures', 'checks_succeeded')).checks
    return [card(r, bodies[r['id']], None if r['passed'] else
                 (passed_before.get(r['code']) or passed_before.get(stock_codes.normalize_key(r['code']))))
            | {'from_batch': r['from_batch'],
               'check_failure': checks.get((stock_codes.normalize_key(r['code']), r['id']))}
            for r in reports]


def comparison(conn, report) -> dict:
    body = report_body.load(report['body'])
    run = conn.execute('SELECT request, status FROM runs WHERE id = ?', (report['run_id'],)).fetchone()
    return comparison_with_prior(conn, report, body, run)[0]


def comparison_with_prior(conn, report, body: dict, run) -> tuple[dict, dict | None]:
    """Compare the saved parent and retain its decoded body if it also supplies the prior agenda."""
    request = (json.loads(run['request'] or '{}') or {}) if run else {}
    parents = {x for x in (request.get('parent_report_id'), body.get('parent_report_id')) if isinstance(x, int)}
    if len(parents) > 1:
        return {'parent_report_id': None, 'baseline_status': 'conflict',
                'reason': '关联原报告记录不一致，无法可靠对照。'}, None
    prior_id = (body.get('prior') or {}).get('report_id')
    parent = next(iter(parents), None) or prior_id
    if not isinstance(parent, int) or parent == report['id']:
        return {'parent_report_id': None, 'baseline_status': 'not_linked',
                'reason': '未记录明确关联的原报告，不能按研究时间自动配对。'}, None
    old = conn.execute('SELECT r.id, r.run_id, r.code, r.name, r.period, r.passed, r.verdict, r.created_at,'
                       ' r.body_sha256, r.coverage, r.body, u.request, u.status'
                       ' FROM reports r JOIN runs u ON u.id = r.run_id'
                       ' WHERE r.id = ?', (parent,)).fetchone()
    if old is None or old['code'] != report['code']:
        return {'parent_report_id': parent, 'baseline_status': 'missing' if old is None else 'not_same_code',
                'reason': '关联原报告不存在或不属于同一家公司，无法对照。'}, None
    before = report_body.load(old['body'])
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
    old_rows, new_rows = before.get('table'), body.get('table')
    if not result['new_ready'] or not result['old_ready']:
        result['reason'] = '至少一份报告尚未就绪，暂不能对照。'
    elif not isinstance(old_rows, list) or not isinstance(new_rows, list) or not old_rows or not new_rows:
        result['reason'] = '至少一份报告未记录财务表，无法计算指标变化。'
    else:
        result.update(table_comparison(old_rows, new_rows))
    return result, before if parent == prior_id else None


def table_comparison(old_rows: list, new_rows: list) -> dict:
    """Deterministic diff of two saved financial tables, plus what could not be compared.

    Only rows that since_prior() compares count: T.* statement rows and valuation snapshots.
    C.* calculations are numbered per report and peer rows belong to other companies, so they
    are neither "missing" nor "excluded"; a malformed row of unknown identity is still reported
    as excluded. A T.* period the new table no longer keeps is listed
    as a period rather than as every indicator that shares it.
    """
    def compared(row):
        if not (isinstance(row, dict) and isinstance(row.get('id'), str)):
            return True  # unknown identity: surfaced as excluded, never silently dropped
        return row['id'].startswith('T.') or row['id'] in VALUATION
    old_rows = [r for r in old_rows if compared(r)]
    new_rows = [r for r in new_rows if compared(r)]
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
        return {'reason': '缺少口径完整且可比较的指标，不能计算变化。', 'excluded': excluded}
    diff = since_prior(old_table, new_table)
    # Empty market snapshots are missing observations, not comparable values.
    diff['valuation'] = [v for v in diff['valuation'] if v['before'] and v['after']]
    # Valuation rows are keyed by market date, so a later snapshot never "drops" the older date.
    missing = sorted({r['label'] for r in old_table if r['id'] not in new_map})
    dropped = sorted({p for r in old_table if r['id'].startswith('T.') and r['id'] in new_map
                      for p in set(r['values']) - set(new_map[r['id']]['values'])})
    return {'diff': diff, 'excluded': excluded, 'missing': missing, 'dropped': dropped}


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


def without_verdict(summary: str, verdict: str | None) -> str:
    """Summaries often open with the verdict that the page already shows on its scale or badge."""
    summary = summary.strip()
    if verdict and summary[len(verdict):len(verdict) + 1] in ('：', ':', '，') and summary.startswith(verdict):
        return summary[len(verdict) + 1:].lstrip()
    return summary


def card(report, body: dict, last_passed=None, *, checks: list[dict] | None = None) -> dict:
    """One company's investment card from saved data; no market fetch or valuation recomputation.

    A report that failed review shows no verdict, summary, risks, checkpoints or figures. The last
    passed report, when the caller supplies one, is named with its own date rather than standing in.
    """
    report = dict(report)  # sqlite3.Row has no .get
    passed = bool(report['passed'])
    verdict = report['verdict'] if passed and report['verdict'] in charts.VERDICTS else None
    summary = without_verdict(body.get('summary') or '', verdict) if passed else ''

    def texts(key):
        return [i['text'] for i in body.get(key) or []
                if isinstance(i, dict) and isinstance(i.get('text'), str) and i['text'].strip()] if passed else []

    table = {r['id']: r for r in body.get('table') or [] if isinstance(r, dict) and isinstance(r.get('id'), str)}
    items = {item['ref']: item for item in disclosure_checks.items(body)}
    check_views = {}
    for check in checks or []:
        if not passed or check['ref'] not in items:
            continue
        item = items[check['ref']]
        row = table.get(check['metric'], {})
        check_views[check['ref']] = check | {
            'unit': row.get('unit', ''), 'label': row.get('label', check['metric']),
            'condition': (f"{item['op'].replace('>=', '≥').replace('<=', '≤')} {item['value']:g}"
                          if item['kind'] != 'forecast' else None)}
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
    continued = body.get('prior')
    continued = continued if isinstance(continued, dict) else {}
    period_label = (f"{latest[2:4]}年{charts.CUMULATIVE.get(latest[5:], latest[5:])}"
                    if latest and len(latest) >= 7 and latest[:4].isdigit() else (latest or None))
    return {'id': report['id'], 'code': report['code'], 'name': report['name'], 'passed': passed,
            'verdict': verdict, 'bucket': bucket, 'rank': CARD_ORDER.get(bucket, 3), 'summary': summary,
            'consensus': (body.get('valuation_calc') or {}).get('consensus') if passed else None,
            'risks': texts('risks'), 'checkpoints': texts('checkpoints'), 'dims': dims,
            'checks': check_views,
            'open_questions': body.get('open_questions', []) if passed else [],
            'boundary': stances.on_boundary(body.get('valuation_calc')) if passed else None,
            'flip_distance': stances.flip_distance(body.get('valuation_calc')) if passed else None,
            'stance': stance_decision(body.get('stance'), body.get('stance_calc'), body.get('valuation_calc')) if passed else None,
            'stance_market_date': body.get('market_date') if passed else None,
            'figures': [metrics[k] for k in CARD_KEYS if k in metrics],
            'period': period_label, 'created_at': created_at, 'coverage': coverage,
            'market_date': day, 'last_passed': dict(last_passed) if last_passed else None,
            # A continuation restates the prior report's risks and checkpoints with sources:
            # item by item in prior_status, or at the end of the answer in reports before that field.
            'prior': continued if passed and isinstance(continued.get('report_id'), int) else None,
            'prior_status': bool(body.get('prior_status')) if passed else False}


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
    """Current linked-run state; candidates not linked yet remain pending."""
    def _run_id(value):
        if type(value) is int:
            return value
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
        return value

    run_ids = [_run_id(row["run_id"]) for row in q20 if row.get("run_id")]
    runs = {}
    if run_ids:
        marks = ",".join("?" for _ in run_ids)
        runs = {
            r["id"]: r for r in conn.execute(
                "SELECT u.id, u.status, u.error, r.id AS report_id, r.passed, r.verdict"
                f" FROM runs u LEFT JOIN reports r ON r.run_id = u.id WHERE u.id IN ({marks})",
                run_ids,
            )
        }
    done = True
    for row in q20:
        run_id = _run_id(row.get("run_id"))
        if not run_id:
            row["research_status"] = "pending"
            done = False
            continue
        run = runs[run_id]
        row["research_status"] = run["status"]
        row["report_id"] = run["report_id"]
        row["verdict"] = run["verdict"] if run["passed"] else None
        row["research_error"] = run["error"]
        if run["status"] in ("queued", "running"):
            done = False
    return done, q20


def existing_run_ids(conn, run_ids: list[int]) -> set[int]:
    marks = ",".join("?" * len(run_ids))
    return {r[0] for r in conn.execute(f"SELECT id FROM runs WHERE id IN ({marks})", run_ids)} if run_ids else set()


def active_runs(conn) -> list:
    """Queued and running tasks, plus recent failures nobody has retried or asked again."""
    since = (datetime.now(db.SHANGHAI) - timedelta(days=RECENT_FAILURE_DAYS)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT id, code, name, status, step FROM runs u WHERE kind IN ('research', 'price')"
        " AND (status IN ('queued', 'running') OR (status = 'failed' AND replay_of IS NULL AND created_at >= ?"
        " AND batch_id IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.run_id=u.id)"
        " AND NOT EXISTS (SELECT 1 FROM runs newer WHERE newer.request_id='web-retry:' || u.id"
        " OR (newer.id > u.id AND newer.kind = u.kind AND newer.company_key = u.company_key"
        " AND json_remove(newer.request,'$.company_query') IS json_remove(u.request,'$.company_query')))))"
        " ORDER BY CASE WHEN status='failed' THEN 1 ELSE 0 END, id DESC LIMIT 30", (since,)
    ).fetchall()


def latest_price(conn, code: str) -> dict | None:
    try:
        key = stock_codes.normalize_key(code)
    except ValueError:
        return None
    row = conn.execute(
        "SELECT body FROM prices WHERE company_key = ? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return None
    body = json.loads(row["body"])  # a price reference from `prices`, not a research report body
    # Levels are only shown while valid; afterwards the reference is history, not an action line.
    body["expired"] = datetime.now(db.SHANGHAI).date().isoformat() > body["expires"]
    body["ladder"] = charts.price_ladder(body["levels"])
    return body


def latest_batch(conn, *, summary: bool = False):
    if summary:
        # Skip the bulky universe and coverage JSON; only entered batches read the returns columns.
        columns = ", ".join(batches.PERFORMANCE_COLUMNS)
        return conn.execute(
            f"SELECT id, created_at, status, {columns} FROM batches ORDER BY id DESC LIMIT 1").fetchone()
    return conn.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 1").fetchone()


def run(conn, run_id: int):
    return conn.execute(
        "SELECT id, kind, code, name, status, step, error, started_at, finished_at, request, request_id, replay_of, batch_id FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()


def report_for_run(conn, run_id: int):
    return conn.execute("SELECT id FROM reports WHERE run_id = ?", (run_id,)).fetchone()


def queue_ahead(conn, run_id: int):
    return conn.execute(
        "SELECT COUNT(*) FROM runs u, (SELECT kind = 'diagnostic' AS diagnostic,"
        " batch_id IS NOT NULL AS batch FROM runs WHERE id = ?) me"
        " WHERE u.request_id IS NOT NULL AND u.id != ? AND (u.status = 'running' OR (u.status = 'queued' AND"
        " (u.kind = 'diagnostic', u.batch_id IS NOT NULL, u.id)"
        " < (me.diagnostic, me.batch, ?)))",
        (run_id, run_id, run_id)).fetchone()


def report(conn, report_id: int):
    return conn.execute("SELECT id, run_id, code, name, period, passed, verdict, created_at,"
                        " body_sha256, coverage, body FROM reports WHERE id = ?", (report_id,)).fetchone()


def report_run(conn, run_id: int):
    return conn.execute("SELECT kind, status, request, request_id, identity, replay_of, material, batch_id FROM runs WHERE id = ?", (run_id,)).fetchone()


def followups(conn, code: str, report_id: int):
    try:
        key = stock_codes.normalize_key(code)
    except ValueError:
        return []
    return conn.execute(
        "SELECT u.id, u.status, COALESCE(NULLIF(json_extract(u.request, '$.question'), ''),"
        " json_extract(f.body, '$.question')) AS question,"
        " json_extract(u.request, '$.feedback.text') AS feedback_text,"
        " json_extract(f.body, '$.prior.report_id') AS prior_report_id"
        " FROM runs u LEFT JOIN reports f ON f.run_id = u.id"
        " WHERE u.kind = 'research' AND u.company_key = ? AND (u.parent_report_id = ? OR"
        " (f.company_key = ? AND (json_extract(f.body, '$.parent_report_id') = ? OR"
        " json_extract(f.body, '$.prior.report_id') = ?))) ORDER BY u.id DESC",
        (key, report_id, key, report_id, report_id)).fetchall()


def api_run(conn, run_id: int, *, include_gaps: bool = False):
    gaps = (", COALESCE(json_extract(r.body, '$.gaps'), json_extract(u.material, '$.data.extra.gaps')) AS gaps"
            if include_gaps else "")
    return conn.execute("SELECT u.id, u.kind, u.code, u.name, u.status, u.step, u.error, u.created_at, u.finished_at,"
        f" r.id AS report_id{gaps} FROM runs u LEFT JOIN reports r ON r.run_id = u.id WHERE u.id = ?", (run_id,)).fetchone()


def api_reports(conn, code: str):
    try:
        key = stock_codes.normalize_key(code)
    except ValueError:
        return []
    return conn.execute("SELECT r.id, r.run_id, r.code, r.name, r.created_at, r.period, r.passed, u.status"
        " FROM reports r JOIN runs u ON u.id = r.run_id WHERE r.company_key = ? AND u.kind = 'research'"
        f" AND NOT ({db.HOLDINGS_AWARE}) ORDER BY r.id DESC LIMIT 100", (key,)).fetchall()


def stock_reports(conn, code: str, *, summaries: bool = False):
    try:
        key = stock_codes.normalize_key(code)
    except ValueError:
        return []
    # History needs metadata and saved series, not every report's full evidence body.
    columns = ("r.id,r.run_id,r.code,r.name,r.created_at,r.period,r.passed,r.verdict,r.coverage,"
               "json_extract(r.body, '$.series') AS operating_series" if summaries else
               "r.id,r.run_id,r.code,r.name,r.period,r.passed,r.verdict,r.created_at,"
               "r.body_sha256,r.coverage,r.body")
    return conn.execute(
        f"SELECT {columns}, u.replay_of AS replay_of, {db.INPUT_MODE} AS input_mode,"
        " COALESCE(NULLIF(json_extract(u.request, '$.question'), ''), json_extract(r.body, '$.question')) AS question,"
        " COALESCE(u.parent_report_id, json_extract(r.body, '$.parent_report_id')) AS parent_report_id,"
        " json_extract(u.request, '$.feedback.text') AS feedback_text,"
        " json_extract(r.body, '$.prior.report_id') AS prior_report_id FROM reports r"
        " JOIN runs u ON u.id = r.run_id WHERE r.company_key = ?"
        " AND u.kind = 'research' ORDER BY r.id DESC", (key,),
    ).fetchall()


def batch_list(conn):
    return conn.execute("SELECT id, status, error, created_at, as_of, l5, sl3, frozen_at FROM batches ORDER BY id DESC").fetchall()


def proposed_batches(conn):
    # By status, not the latest batch: a later month's batch must not hide one still awaiting the owner.
    return conn.execute("SELECT id, created_at FROM batches WHERE status = 'proposed' ORDER BY id DESC").fetchall()


def batch(conn, batch_id: int):
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
