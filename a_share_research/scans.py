"""Narrow disclosure discovery and K2 review scheduling; no account or notification writes."""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import logging
import math
import re

from a_share_research import report as report_body
from a_share_research import companies, db, disclosure_checks, service, stock_codes, valuation

log = logging.getLogger(__name__)
# Generic 风险提示 titles (trading-volatility or redemption notices) are routine and stay out of scope.
RISK = re.compile(r'立案|行政处罚|监管工作函|监管函|问询函|关注函|退市风险|终止上市|非标准|(?<!无)保留意见|无法表示意见|否定意见|重大诉讼|债务逾期')
# Current governance red lines; a title hit only triggers investigation, never a finding of violation.
GOVERNANCE = re.compile(r'资金占用|占用(?:上市)?公司资金|违规担保')
# Policies, periodic fund-flow tables and no-occupation undertakings are routine filings.
GOVERNANCE_ROUTINE = re.compile(r'制度|办法|规则|汇总表|专项说明|承诺函|不存在|其他关联资金往来')
FORECAST = re.compile('业绩预告|业绩快报|业绩修正|业绩预亏|业绩预减|业绩预盈|业绩预增')
PERIODIC = re.compile('年度报告|季度报告|年报|半年报|季报')
PERIOD = re.compile(r'(\d{4})\s*年?\s*(前三季度|第?三季度?|半年度|上半年|第?一季度?|年度|全年)')
EXTREMES = {'测算支持区', '测算透支区'}
# Sections whose cited sources show a disclosure actually entered the reasoning.
CITED_SECTIONS = ('answer', 'risks', 'business', 'financial', 'checkpoints', 'prior_status')
RESOLVED = ('included', 'superseded')
REVIEW_LABELS = {
    'included': '已纳入研究（不等于风险排除）',
    'superseded': '已由其后同期定期报告核对（不等于风险排除）',
    'open': '研究引用了该公告，但复核留有待核问题',
    'unable': '研究读取后写明无法判断',
    'read_uncited': '研究读取了该公告但未引用',
    'not_read': '研究未读取该公告',
    'awaiting_research': '等待研究（每日最多 5 家）',
    'researching': '研究进行中',
    'research_failed': '研究失败，可重试',
}


def classify(title: str) -> tuple[str, int] | None:
    if RISK.search(title):
        return 'major_risk', 2 if any(word in title for word in ('问询函', '关注函')) and '回复' in title else 3
    if GOVERNANCE.search(title) and not GOVERNANCE_ROUTINE.search(title):
        return 'major_risk', 3
    if FORECAST.search(title):
        return 'forecast', 2
    # Corrections have their own announcement identity and must remain discoverable.
    if PERIODIC.search(title) and not any(word in title for word in ('摘要', '英文', 'English', '取消', '说明会')):
        return 'periodic', 2
    return None


def period_of(title: str) -> str | None:
    """Financial period a periodic report or forecast title names; None when the title gives none."""
    match = PERIOD.search(title or '')
    if match is None:
        return None
    word = match[2]
    end = '09-30' if '三季' in word else '06-30' if '半' in word else '03-31' if '一季' in word else '12-31'
    return f'{match[1]}-{end}'


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=db.SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(db.SHANGHAI)


def after_card(event: dict, completed_at: str) -> bool:
    """Announcements carry only a date: one disclosed before the card's completion day was available to it."""
    return instant(event['announced']).date() >= instant(completed_at).date()


def event_context(event: dict) -> dict:
    """Public identity frozen into a research request; no scan verdicts or account data."""
    return {key: event[key] for key in ('id', 'code', 'title', 'announced', 'url', 'kind')}


def _citing(body: dict, keys: set[str]) -> list[tuple[str, dict]]:
    return [(f'{section}[{index}]', item) for section in CITED_SECTIONS
            for index, item in enumerate(body.get(section) or [])
            # Table cites carry `ref`; only quote cites point at a document (default D0).
            if isinstance(item, dict) and any(isinstance(cite, dict) and 'ref' not in cite and cite.get('source', 'D0') in keys
                                              for cite in item.get('cites') or [])]


def _settled(body: dict, refs: list[tuple[str, dict]]) -> str:
    # Review questions point at an item (`risks[2]`) or a whole section (`risks`).
    open_where = {q.get('where') for q in body.get('open_questions') or [] if isinstance(q, dict)}
    unsettled = {ref for ref, _ in refs if ref in open_where or ref.split('[')[0] in open_where}
    if any(ref not in unsettled and item.get('status') != '无法判断' and '无法判断' not in str(item.get('text', ''))
           for ref, item in refs):
        return 'included'
    return 'open' if unsettled else 'unable'


def _issuer(code) -> str | None:
    """Six-digit company key of a stored code, or None when it is missing or malformed."""
    try:
        return stock_codes.normalize_key(code)
    except ValueError:
        return None


def event_evidence(body: dict, event: dict) -> dict:
    """How one published report used an announcement, judged only from its read sources and citations.

    A newer report or a passed review alone proves nothing: the announcement must have been read
    and cited by a settled item. A forecast also counts once a later periodic report covering its
    period was read and cited, since the official statements replace the estimate; that report must
    name the forecast's issuer, so a peer's statements read for comparison never replace it.
    """
    documents = {'D0': body.get('document') or {}} | {
        key: (source or {}).get('document') or {} for key, source in (body.get('sources') or {}).items()}
    keys = {key for key, doc in documents.items() if doc.get('url') == event['url']}
    refs = _citing(body, keys)
    state = _settled(body, refs) if refs else 'read_uncited' if keys else 'not_read'
    issuer = _issuer(event.get('code'))
    if (state not in RESOLVED and event['kind'] == 'forecast' and issuer is not None
            and (period := period_of(event['title']))):
        # A missing or unparsable issuer code proves nothing about whose statements these are.
        later = {key for key, doc in documents.items() if key not in keys and doc.get('period')
                 and _issuer(doc.get('code')) == issuer
                 and doc['period'] >= period and (doc.get('announced') or '') >= event['announced']}
        covered = _citing(body, later)
        if covered and _settled(body, covered) == 'included':
            keys, refs, state = later, covered, 'superseded'
    return {'state': state, 'label': REVIEW_LABELS[state], 'sources': sorted(keys), 'items': [ref for ref, _ in refs]}


def event_reviews(conn, code: str) -> list[dict]:
    """Every dispatchable event of a company with its handling state; read-only."""
    code = stock_codes.normalize_key(code)
    items = [e for e in events(conn, code=code)
             if e['severity'] >= 2 and e['action'] in ('deferred', 'queued_review', 'covered')]
    if not items:
        return []
    first = min(instant(e['announced']).date() for e in items)
    reports = [dict(r) for r in conn.execute(
        'SELECT r.id, r.run_id, COALESCE(u.finished_at, r.created_at) AS completed_at, r.body FROM reports r '
        f'JOIN runs u ON u.id=r.run_id WHERE r.company_key=? AND {db.FORMAL_REPORT} ORDER BY r.id DESC', (code,))
        if instant(r['completed_at']).date() >= first]
    bodies = {}

    def evidence(report, event):
        if report['id'] not in bodies:
            bodies[report['id']] = report_body.load(report['body'])
        return event_evidence(bodies[report['id']], event)

    result = []
    for event in items:
        candidates = [r for r in reports if instant(r['completed_at']).date() >= instant(event['announced']).date()]
        found = next(((r, ev) for r in candidates if (ev := evidence(r, event))['state'] in RESOLVED), None)
        own = next((r for r in candidates if r['run_id'] == event['run_id']), None)
        if found:
            report, review = found
        elif event['action'] == 'deferred':
            report, review = None, {'state': 'awaiting_research'}
        elif event['run_status'] in ('queued', 'running'):
            report, review = None, {'state': 'researching'}
        elif event['run_status'] == 'failed':
            report, review = None, {'state': 'research_failed'}
        elif report := own or (candidates[0] if candidates else None):
            review = evidence(report, event)
        else:
            review = {'state': 'not_read'}
        review = {'sources': [], 'items': []} | review | {
            'label': REVIEW_LABELS[review['state']], 'report_id': report['id'] if report else None}
        result.append(event | {'review': review})
    return result


def open_events(conn, code: str) -> list[dict]:
    """Events not yet shown to be read and cited by a published report: they stay visible until then."""
    return [e for e in event_reviews(conn, code) if e['review']['state'] not in RESOLVED]


def review_order(events: list[dict], scan_id: int, priority: set[str] | None = None) -> list[str]:
    """Priority companies first; within each group retain backlog/severity/date order."""
    grouped = {}
    for event in events:
        grouped.setdefault(event['code'], []).append(event)

    def order(code):
        items = grouped[code]
        old = [e for e in items if e['scan_id'] != scan_id]
        return (0, min(e['detected_at'] for e in old), -max(e['severity'] for e in items), code) if old else (
            1, -max(e['severity'] for e in items), min(e['announced'] for e in items), code)
    return sorted(grouped, key=lambda code: (code not in (priority or set()), order(code)))


def notification_reasons(before: dict | None, after: dict | None, events: list[dict],
                         completions: list[dict], *, checks: list[dict] | None = None, new_checks: list[dict] | None = None,
                         last_extreme_zone: str | None = None) -> list[dict]:
    reasons = []
    effective = disclosure_checks.latest(checks or [])
    new_ids = {c['id'] for c in (new_checks or [])}
    for kind, outcome, reason in (('forecast', 'deviated', 'checkpoint_deviated'),
                                  ('add_trigger', 'missed', 'add_trigger_missed')):
        matching = [c for c in effective if c['kind'] == kind and c['outcome'] == outcome]
        if any(c['id'] in new_ids for c in matching):
            reasons.append({'kind': 'disclosure_check', 'reason': reason,
                            'detail': '；'.join(c['detail'] for c in matching), 'checks': matching})
    for event in events:
        if event['kind'] == 'major_risk':
            reasons.append({'kind': 'major_risk', 'reason': 'major_risk', 'event_id': event['id'],
                            'detail': event['title'], 'bypass_cooldown': True})
    if after and after['status'] == '待复核' and (before or {}).get('status', '有效') == '有效':
        reasons.append({'kind': 'card_status', 'reason': 'pending_review', 'detail': '论点卡由有效变为待复核'})
    for completion in completions:
        if completion['status'] == 'failed':
            reasons.append({'kind': 'review', 'reason': 'review_failed', 'run_id': completion['id'],
                            'detail': completion['error'], 'bypass_cooldown': True})
        elif completion['changed']:
            reasons.append({'kind': 'review', 'reason': 'judgment_changed', 'run_id': completion['id'],
                            'detail': '接续研究的参考区间或结论变化', 'change': completion['change']})
    old = ((before or {}).get('current') or {}).get('current_zone')
    new = ((after or {}).get('current') or {}).get('current_zone')
    # Same report: require the price move to cross the already-known boundary too.
    # Reuse the previous scan's valuation date, never a future date or a new report.
    previous_value = ((before or {}).get('current') or {}).get('valuation') or {}
    current_value = ((after or {}).get('current') or {}).get('valuation') or {}
    if (before and after and before.get('report_id') is not None
            and before['report_id'] == after.get('report_id')
            and previous_value.get('as_of') and current_value.get('as_of')
            and previous_value['as_of'] <= current_value['as_of']
            and current_value.get('close') is not None
            and all(previous_value.get(k) is not None for k in ('support_below', 'stretched_above'))):
        price_only = valuation.price_zone(current_value['close'], previous_value['support_below'],
                                     previous_value['stretched_above'])
        if price_only == old:
            new = old  # Suppress only the time-driven notice; keep the displayed rolling zone.
    last_extreme_zone = last_extreme_zone or (old if old in EXTREMES else None)
    if old is not None and new is not None and old != new and (old in EXTREMES or new in EXTREMES):
        reasons.append({'kind': 'current_zone', 'reason': 'current_zone_changed',
                        'detail': f'{old} → {new}', 'from_zone': old, 'to_zone': new,
                        'bypass_cooldown': new in EXTREMES and last_extreme_zone in EXTREMES
                                           and new != last_extreme_zone})
    return reasons


def allow_notification(item: dict, history: list[dict], at: str, trade_days: list[str]) -> bool:
    if item.get('bypass_cooldown'):
        return True
    previous = next((old for old in reversed(history)
                     if (old['code'], old['reason']) == (item['code'], item['reason'])), None)
    if previous is None:
        return True
    start, end = instant(previous['notified_at']).date(), instant(at).date()
    return sum(start < date.fromisoformat(day) <= end for day in trade_days) >= 5


def subscriptions(conn) -> list[dict]:
    items = {r['code']: dict(r) for r in conn.execute(
        'SELECT code,origin,added_at,last_close,last_close_date FROM subscriptions ORDER BY code')}
    batch = conn.execute('SELECT l5 FROM batches WHERE frozen_at IS NOT NULL AND l5 IS NOT NULL '
                         'ORDER BY frozen_at DESC, id DESC LIMIT 1').fetchone()
    if batch:
        for code in json.loads(batch['l5'])['codes']:
            code = stock_codes.normalize_key(code)
            if code not in items:
                items[code] = {'code': code, 'origin': 'l5', **latest_close(conn, code)}
    return list(items.values())


def scan_scope(conn) -> list[dict]:
    """Subscriptions plus every tracking batch's L5 and every company with a formal card.

    The extra codes are scanned, checked and queued for review only; /api/cards and the policy
    candidate set stay on subscriptions().
    """
    items = {s['code']: s for s in subscriptions(conn)}
    extra = [(code, 'l5') for batch in conn.execute(
        "SELECT l5 FROM batches WHERE frozen_at IS NOT NULL AND l5 IS NOT NULL AND status='tracking' ORDER BY frozen_at DESC, id DESC")
        for code in json.loads(batch['l5'])['codes']]
    extra += [(row['company_key'], 'card') for row in conn.execute(
        f"SELECT DISTINCT r.company_key FROM reports r JOIN runs u ON u.id = r.run_id WHERE {db.FORMAL_REPORT} ORDER BY r.company_key")]
    for code, origin in extra:
        code = stock_codes.normalize_key(code)
        if code not in items:
            items[code] = {'code': code, 'origin': origin, **latest_close(conn, code)}
    return list(items.values())


def replace_subscriptions(conn, codes: list[str], *, catalog: dict | None = None) -> list[dict]:
    if not isinstance(codes, list) or any(not isinstance(c, str) or not stock_codes.KEY_RE.fullmatch(c) for c in codes):
        raise service.RequestError('订阅代码必须为六位 A 股代码')
    catalog = companies.stock_list() if catalog is None and codes else (catalog or {})
    unknown = sorted(set(codes) - catalog.keys())
    if unknown:
        raise service.RequestError('未知 A 股代码：' + '、'.join(unknown))
    with db.transaction(conn):
        for row in conn.execute("SELECT code FROM subscriptions WHERE origin='external'").fetchall():
            if row['code'] not in codes:
                conn.execute('DELETE FROM subscriptions WHERE company_key=?', (stock_codes.normalize_key(row['code']),))
        for code in sorted(set(codes)):
            conn.execute("INSERT INTO subscriptions(code, added_at) VALUES (?,?) ON CONFLICT(code) DO NOTHING",
                         (code, db.now()))
    return subscriptions(conn)


def _submit(conn) -> int:
    row = conn.execute("SELECT id FROM scans WHERE status IN ('queued','running')").fetchone()
    if row:
        return row['id']
    return conn.execute("INSERT INTO scans(status,requested_at) VALUES ('queued',?)", (db.now(),)).lastrowid


def submit(conn) -> int:
    with db.transaction(conn):
        return _submit(conn)


def submit_request(conn, requester: str, request_id: str) -> int:
    """Recover a scan by authenticated caller and durable key, including terminal scans.

    Identity comes from the API authenticator, never a client body. Invalid keys
    fail before writing; scan creation and the mapping commit together.
    """
    if (not isinstance(requester, str) or re.fullmatch(r'(key|agent):[0-9a-f]{64}', requester) is None
            or not isinstance(request_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', request_id) is None):
        raise service.RequestError('扫描请求身份或编号格式无效', 400)
    with db.transaction(conn):
        row = conn.execute('SELECT scan_id FROM scan_requests WHERE requester=? AND request_id=?',
                           (requester, request_id)).fetchone()
        if row:
            return row['scan_id']
        scan_id = _submit(conn)
        conn.execute('INSERT INTO scan_requests(requester,request_id,scan_id,created_at) VALUES (?,?,?,?)',
                     (requester, request_id, scan_id, db.now()))
        return scan_id


def recover(conn) -> None:
    # Persisted discoveries remain deferred, so the next scan can resume without duplicating runs.
    conn.execute("UPDATE scans SET status='failed',finished_at=?,error='扫描因服务重启中断；已发现事件保留' "
                 "WHERE status='running'", (db.now(),))


def events(conn, *, scan_id: int | None = None, code: str | None = None) -> list[dict]:
    if code is not None:
        code = stock_codes.normalize_key(code)
    where, params = [], []
    for column, value in (('e.scan_id', scan_id), ('e.company_key', code)):
        if value is not None:
            where.append(column + '=?')
            params.append(value)
    rows = conn.execute(
        'SELECT e.id,e.scan_id,e.code,e.doc_key,e.title,e.announced,e.url,e.kind,e.severity,e.detected_at,e.action,e.run_id,'
        'u.status AS run_status,u.finished_at AS judgment_completed_at,r.passed '
        'FROM scan_events e LEFT JOIN runs u ON u.id=e.run_id LEFT JOIN reports r ON r.run_id=u.id '
        + ('WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY e.id', params)
    return [dict(r) for r in rows]


def latency(event: dict, at: str) -> dict:
    announced, detected = instant(event['announced']), instant(event['detected_at'])
    finished = event.get('judgment_completed_at')
    return {'announced': event['announced'], 'detected_at': event['detected_at'],
            'judgment_completed_at': finished,
            'disclosure_to_detection_seconds': (detected - announced).total_seconds(),
            'detection_to_judgment_seconds': (instant(finished) - detected).total_seconds() if finished else None,
            'backlog_days': max(0, (instant(at).date() - detected.date()).days) if event['action'] == 'deferred' else 0,
            'announcement_precision': 'date'}


def card_or_none(conn, code: str, *, scan_id: int | None = None) -> dict | None:
    try:
        return service.company_card(conn, code, scan_id=scan_id)
    except service.RequestError as exc:
        if exc.status != 404:
            raise
        return None


def latest_close(conn, code: str) -> dict:
    code = stock_codes.normalize_key(code)
    row = conn.execute('SELECT last_close,last_close_date FROM subscriptions WHERE company_key=?', (code,)).fetchone()
    candidates = [dict(row)] if row and row['last_close'] is not None else []
    for scan in conn.execute('SELECT summary FROM scans WHERE summary IS NOT NULL ORDER BY id DESC'):
        close = json.loads(scan['summary']).get('closes', {}).get(code)
        if close:
            candidates.append(close)
            break
    return max(candidates, key=lambda x: x['last_close_date']) if candidates else {'last_close': None, 'last_close_date': None}


def announcement_failure(conn, code: str, *, scan_id: int | None = None) -> dict | None:
    # During execute, the fully collected current summary is projected before the scan is finalized.
    for row in conn.execute("SELECT id,status,summary FROM scans WHERE summary IS NOT NULL "
                            "AND (status IN ('done','failed') OR id=?) ORDER BY id DESC", (scan_id,)):
        summary = json.loads(row['summary'])
        failure = next((f for f in summary.get('failures', [])
                        if f['code'] == code and f['stage'] == 'announcements'), None)
        if failure:
            return {'kind': 'scan_failure', 'scan_id': row['id'], **failure}
        if (code in summary.get('announcements_succeeded', [])
                or row['status'] == 'done' and any(s['code'] == code for s in summary.get('scope', []))):
            return None
    return None


def mechanical_failure(conn, code: str, report_id: int, *, scan_id: int | None = None) -> dict | None:
    """Only an explicit successful check clears the current report's failure."""
    code = stock_codes.normalize_key(code)
    for row in conn.execute("SELECT id,summary FROM scans WHERE summary IS NOT NULL "
                            "AND (status IN ('done','failed') OR id=?) ORDER BY id DESC", (scan_id,)):
        summary = json.loads(row['summary'])
        failure = next((f for f in summary.get('check_failures', [])
                        if f['code'] == code and (f.get('report_id') or
                            (summary.get('before', {}).get(code) or {}).get('report_id') or
                            (summary.get('cards', {}).get(code) or {}).get('report_id')) == report_id), None)
        if failure:
            return {'kind': 'check_failure', **failure, 'report_id': report_id, 'scan_id': row['id']}
        if {'code': code, 'report_id': report_id} in summary.get('checks_succeeded', []):
            return None
    return None


def last_extreme_zones(history: list[dict]) -> dict[str, str]:
    zones = {}
    for scan in history:
        summary = scan['summary']
        # Older v7 summaries can seed the memory from their notices and card snapshots.
        for notice in summary.get('notify', []):
            zone = notice.get('to_zone') if notice.get('to_zone') in EXTREMES else notice.get('from_zone')
            if zone in EXTREMES:
                zones[notice['code']] = zone
        for code, card in summary.get('cards', {}).items():
            zone = ((card or {}).get('current') or {}).get('current_zone')
            if zone in EXTREMES:
                zones[code] = zone
        zones.update(summary.get('last_extreme_zones', {}))
    return zones


def _done_scans(conn, scan_id: int) -> list[dict]:
    return [dict(r) | {'summary': json.loads(r['summary'] or '{}')} for r in conn.execute(
        "SELECT * FROM scans WHERE id<? AND status='done' ORDER BY id", (scan_id,))]


def last_success(history: list[dict], code: str) -> dict | None:
    return next((s for s in reversed(history) if code in s['summary'].get('closes', {})
                 and not any(f['code'] == code for f in s['summary'].get('failures', []))), None)


def coverage(history: list[dict], code: str, at: str) -> dict:
    """Last successful scan and the interval the 30-day lookback can no longer reach; no backfill."""
    successful = last_success(history, code)
    gap = None
    if successful:
        since, reach = instant(successful['started_at']).date(), instant(at).date() - timedelta(days=30)
        if since < reach:
            gap = {'last_success': successful['started_at'], 'uncovered_from': since.isoformat(),
                   'uncovered_to': reach.isoformat()}
    return {'code': code, 'last_success': successful['started_at'] if successful else None, 'gap': gap}


class MarketSources:
    """All network access is confined to the worker's collection boundary."""

    def announcements(self, code, days):
        from a_share_research import sources
        return sources._query_announcements(code, days, '', '', 100)

    def valuation(self, code):
        from a_share_research import sources
        return sources.valuation(code)

    def trade_days(self, start, end):
        from a_share_research import market
        return market.trade_days(market.client(), start.replace('-', ''), end.replace('-', ''))

    def disclosures(self, code: str, periods: list[str]) -> dict:
        from a_share_research import market, material, sources
        from a_share_research.core.providers.tushare_quotes import to_tushare_stock_code
        pro = market.client()
        result = {name: [row for period in periods for row in market.query(
            pro, name, ts_code=to_tushare_stock_code(code), period=period.replace('-', '')
        ).to_dict(orient='records')] for name in ('forecast', 'express')}
        # The target period may not have an income statement yet: compare the
        # latest disclosed consolidated basis, not only the forecast's period.
        result['income'] = market.query(pro, 'income', ts_code=to_tushare_stock_code(code),
                                        report_type='1').to_dict(orient='records')
        fin = sources.financials(code)
        table = material.financial_table(fin, {})
        result['periodic'] = []
        for statement, frame in fin.items():
            rows = material._latest_rows(frame, consolidated=statement in ('income', 'cashflow', 'balance'))
            metric_ids = {f'T.{m[0]}' for m in material.METRICS if m[3] == statement}
            for period, row in rows.iterrows():
                if period in periods:
                    # Provider-normalized nulls must not become NaN date strings.
                    clean = row.astype(object).where(row.notna(), None).to_dict()
                    result['periodic'].append({'statement': statement, 'row': clean | {'end_date': period},
                                              'table': [r for r in table if r['id'] in metric_ids]})
        return result

    def report_rc_window(self, as_of):
        from a_share_research import sources
        return sources.report_rc_window(as_of)


def normalize_announcements(rows: list[dict], code: str) -> list[dict]:
    from a_share_research import sources
    result = []
    for raw in rows:
        classification = classify(re.sub(r'<[^>]+>', '', raw.get('announcementTitle', '')))
        doc = sources.announcement_document(raw, code)
        if doc is None:
            if raw.get('secCode') != code:
                raise companies.SourceError('公告目录混入其他公司')
            if classification:
                raise companies.SourceError('相关公告缺少可核查的官方 PDF 地址')
            continue
        if classification:
            result.append({'code': code, 'doc_key': str(raw.get('announcementId') or doc.url),
                           'title': doc.title, 'announced': doc.announced, 'url': doc.url,
                           'kind': classification[0], 'severity': classification[1]})
    return result


def normalize_close(row: dict, code: str, at: str) -> dict:
    # External TuShare identifier: a nonstandard form must surface as the source error below.
    if str(row.get('ts_code', code))[:6] != code:
        raise companies.SourceError('估值行情返回其他公司')
    close = float(row['close'])
    day = date.fromisoformat(str(row['trade_date']))
    if not math.isfinite(close) or close <= 0 or day > instant(at).date():
        raise companies.SourceError('收盘价或行情日期无效')
    return {'last_close': close, 'last_close_date': day.isoformat()}


def completed_reviews(conn, since: str | None, at: str) -> list[dict]:
    rows = conn.execute("SELECT DISTINCT u.id,u.code,u.status,u.error,u.finished_at FROM runs u JOIN scan_events e ON e.run_id=u.id "
                        "WHERE u.status IN ('done','failed') AND u.finished_at IS NOT NULL ORDER BY u.id")
    result = []
    for row in rows:
        if instant(row['finished_at']) > instant(at) or (since and instant(row['finished_at']) <= instant(since)):
            continue
        item = dict(row) | {'changed': False}
        if row['status'] == 'done':
            current = conn.execute('SELECT id,body FROM reports WHERE run_id=? AND passed=1', (row['id'],)).fetchone()
            if current is None:
                continue
            previous = conn.execute(
                "SELECT r.body FROM reports r JOIN runs u ON u.id=r.run_id WHERE r.company_key=? "
                f"AND r.id<? AND {db.FORMAL_REPORT} ORDER BY r.id DESC LIMIT 1",
                (stock_codes.normalize_key(row['code']), current['id'])).fetchone()
            if previous:
                def judgment(body):
                    body = report_body.load(body)
                    return {'zone': (body.get('valuation_calc') or {}).get('zone'), 'verdict': body.get('verdict')}
                item['change'] = {'before': judgment(previous['body']), 'after': judgment(current['body'])}
                item['changed'] = item['change']['before'] != item['change']['after']
        result.append(item)
    return result


def stored_checks(conn, report_id: int) -> list[dict]:
    """Read append-only evidence in the pure judge's field vocabulary."""
    rows = conn.execute('SELECT * FROM disclosure_checks WHERE report_id=? ORDER BY id', (report_id,))
    return [dict(r) | {'ref': r['item_ref'], 'identity': r['source_identity'],
                      'observed': json.loads(r['observed']),
                      'evidence': json.loads(r['evidence']) if r['evidence'] else None} for r in rows]


def check_detail(check: dict, item: dict, body: dict) -> str:
    row = next((r for r in body.get('table', []) if r['id'] == item['metric']), {})
    label = row.get('label', item['metric'])
    unit = row.get('unit', '')
    observed = check['observed']
    expected = f"[{item['low']}, {item['high']}]" if item['kind'] == 'forecast' else f"{item['op']} {item['value']}"
    direction = {'above': '高于预测区间', 'below': '低于预测区间'}.get(check['direction'], '未达到条件')
    return (f"{label} {item['period']}：披露 [{observed['low']}, {observed['high']}] {unit}，"
            f"原条件 {expected} {unit}，{direction}；{check['source']} {check['announced']}")


def _check_disclosures(conn, source, code: str, card: dict | None, scan_id: int, at: str) -> list[dict] | None:
    if card is None:
        return None
    pending = [e for e in open_events(conn, code)
               if e['kind'] in ('forecast', 'periodic') and after_card(e, card['completed_at'])]
    if not pending:
        return None
    body = report_body.load(conn.execute('SELECT body FROM reports WHERE id=?', (card['report_id'],)).fetchone()['body'])
    items = disclosure_checks.items(body)
    if not items:
        return None
    raw = source.disclosures(code, sorted({i['period'] for i in items}))
    observations = (disclosure_checks.normalize_forecast(raw['forecast'], code)
                    + disclosure_checks.normalize_express(raw['express'], code,
                        revenue_basis_matches=disclosure_checks.revenue_basis_matches(raw['income']))
                    + disclosure_checks.normalize_periodic(raw['periodic'], code))
    event = max(pending, key=lambda e: (e['announced'], e['id']))
    inserted = []
    with db.transaction(conn):
        for observation in observations:
            if not after_card(observation, card['completed_at']):
                continue
            for item in items:
                check = disclosure_checks.judge(item, observation)
                if check is None:
                    continue
                evidence = {'value': observation['values'][item['metric']]}
                if 'variants' in observation:
                    evidence['variants'] = observation['variants']
                evidence = json.loads(json.dumps(evidence, default=str))
                history = [c for c in stored_checks(conn, card['report_id'])
                           if c['ref'] == check['ref'] and c['identity'] == check['identity']]
                if history:
                    previous = history[-1]
                    if previous['evidence'] == evidence or (previous['evidence'] is None
                            and all(previous[k] == check[k] for k in ('observed', 'outcome', 'direction', 'reason'))):
                        continue
                    if any(c['reason'] == disclosure_checks.IDENTITY_CONFLICT_REASON or any(
                            c['observed'][k] is not None and check['observed'][k] is not None
                            and Decimal(c['observed'][k]) != Decimal(check['observed'][k])
                            for k in ('low', 'high')) for c in history):
                        check.update(outcome='undetermined', direction=None, observed={'low': None, 'high': None},
                                     reason=disclosure_checks.IDENTITY_CONFLICT_REASON)
                version = history[-1]['version'] + 1 if history else 1
                row = conn.execute(
                    'INSERT INTO disclosure_checks(code,report_id,scan_id,scan_event_id,item_ref,kind,metric,period,'
                    'source,source_identity,announced,outcome,direction,reason,checked_at,observed,version,evidence) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id',
                    (code, card['report_id'], scan_id, event['id'], *[check[k] for k in
                     ('ref', 'kind', 'metric', 'period', 'source', 'identity', 'announced', 'outcome', 'direction', 'reason')],
                     at, json.dumps(check['observed']), version, json.dumps(evidence, ensure_ascii=False))).fetchone()
                inserted.append(check | {'id': row['id'], 'version': version, 'report_id': card['report_id']})
    return inserted


def _dispatch(conn, scan_id: int, at: str, scope: set[str], *, history: list[dict] | None = None) -> list[int]:
    scope = {stock_codes.normalize_key(code) for code in scope}
    with db.transaction(conn):
        pending = [e for e in events(conn) if e['action'] == 'deferred' and e['code'] in scope]
        eligible = []
        priority = set()
        reviews = {}
        for event in pending:
            card = card_or_none(conn, event['code'])
            if card is None:
                conn.execute("UPDATE scan_events SET action='no_card' WHERE id=?", (event['id'],))
                continue
            if event['code'] not in reviews:
                reviews[event['code']] = {e['id']: e['review'] for e in event_reviews(conn, event['code'])}
            if reviews[event['code']][event['id']]['state'] in RESOLVED:
                # A published report already read and cited it; a newer date alone is not enough.
                conn.execute("UPDATE scan_events SET action='covered' WHERE id=?", (event['id'],))
            elif conn.execute("SELECT 1 FROM runs WHERE company_key=? AND kind='research' AND batch_id IS NULL "
                              "AND status IN ('queued','running')", (event['code'],)).fetchone():
                # Stays deferred: that run picks up open events when it starts, or the next scan decides.
                continue
            else:
                eligible.append(event)
                if any(c['outcome'] in ('deviated', 'missed') for c in
                       disclosure_checks.latest(stored_checks(conn, card['report_id']))):
                    priority.add(event['code'])
        # Runs, rather than events, count toward the Shanghai calendar-day budget.
        used = sum(instant(r['created_at']).date() == instant(at).date() for r in conn.execute(
            'SELECT DISTINCT u.id,u.created_at FROM runs u JOIN scan_events e ON e.run_id=u.id'))
        ordered = review_order(eligible, scan_id, priority)
        selected = ordered[:max(0, 5 - used)]
        risks = [e for e in eligible if e['scan_id'] == scan_id and e['kind'] == 'major_risk' and e['severity'] == 3]
        if selected and risks:
            # Use the scan's as-of day for freshness, even if dispatch crosses midnight.
            started = conn.execute('SELECT started_at FROM scans WHERE id=?', (scan_id,)).fetchone()['started_at']
            day = instant(started or at).date()
            history = _done_scans(conn, scan_id) if history is None else history
            fresh = set()
            for event in risks:
                successful = last_success(history, event['code'])
                since = instant(successful['started_at']).date() if successful else day
                first_day = min(day, since + timedelta(days=1))
                if first_day <= instant(event['announced']).date() <= day:
                    fresh.add(event['code'])
            if fresh and fresh.isdisjoint(selected):
                selected[-1] = next(code for code in ordered if code in fresh)
        queued = []
        for code in selected:
            frozen = [event_context(e) for e in eligible if e['code'] == code]
            run_id = service.submit(conn, code, f'scan-{scan_id}-{code}', events=frozen)
            # Explicit at makes offline replay and midnight boundary behavior deterministic.
            conn.execute('UPDATE runs SET created_at=? WHERE id=?', (at, run_id))
            conn.executemany("UPDATE scan_events SET action='queued_review',run_id=? WHERE id=? AND action='deferred'",
                             [(run_id, e['id']) for e in frozen])
            queued.append(run_id)
        return queued


def execute(conn, scan_id: int, *, source=None, at: str | None = None) -> None:
    fixed_at = at
    at = at or db.now()
    source = source or MarketSources()
    claimed = conn.execute("UPDATE scans SET status='running',started_at=? WHERE id=? AND status='queued' RETURNING id",
                           (at, scan_id)).fetchone()
    if not claimed:
        return
    summary = {'checks': {}, 'checks_succeeded': [], 'check_failures': [], 'coverage_gaps': [], 'failures': [], 'closes': {}, 'cards': {}, 'notify': [], 'queued_runs': [], 'names': {},
               'announcements_succeeded': []}
    try:
        prefetch = source.report_rc_window((instant(at).date() - timedelta(days=1)).isoformat())
        summary['consensus_prefetch'] = {
            'covered': prefetch['covered_days'], 'missing': prefetch['missing_days'],
            'trading_days': prefetch['trading_days'], 'truncated_days': prefetch['truncated_days'],
            'empty_days': prefetch.get('empty_days', []), 'retry_at': prefetch.get('retry_at')}
        if prefetch.get('reason'):
            summary['consensus_prefetch']['reason'] = prefetch['reason']
            log.info('scan %s consensus prefetch: %s', scan_id, prefetch['reason'])
    except Exception as exc:
        log.exception('scan %s consensus prefetch failed', scan_id)
        summary['consensus_prefetch'] = {'covered': 0, 'missing': [], 'reason': str(exc)}
    try:
        scope = scan_scope(conn)
        history = _done_scans(conn, scan_id)
        # A code joining an existing scan history looks back 30 days on its first pass; those
        # old filings are recorded and reviewed, but do not each push a pending-review notice.
        entering = {s['code'] for s in scope if history and last_success(history, s['code']) is None}
        previous = history[-1] if history else None
        notices = [n for row in conn.execute(
            "SELECT summary FROM scans WHERE id<? AND status IN ('done','failed') ORDER BY id", (scan_id,))
            for n in json.loads(row['summary'] or '{}').get('notify', [])]
        summary['last_extreme_zones'] = last_extreme_zones(history)
        summary['scope'] = scope
        summary['before'] = {s['code']: card_or_none(conn, s['code']) for s in scope}
        for subscription in scope:
            code = subscription['code']
            successful = last_success(history, code)
            days = min(30, max(3, (instant(at).date() - instant(successful['started_at']).date()).days + 2)) if successful else 30
            if gap := coverage(history, code, at)['gap']:
                summary['coverage_gaps'].append({'code': code, **gap})
            try:
                rows, listing = source.announcements(code, days)
                if listing.get('truncated'):
                    raise companies.SourceError('公告目录分页截断，不能确认扫描完整')
                found = normalize_announcements(rows, code)
                if rows:
                    summary['names'][code] = rows[0].get('secName')
                with db.transaction(conn):
                    for event in found:
                        conn.execute('INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action) '
                                     "VALUES (?,?,?,?,?,?,?,?,?,'deferred') ON CONFLICT(code,doc_key) DO NOTHING",
                                     (scan_id, *[event[k] for k in ('code','doc_key','title','announced','url','kind','severity')],
                                      fixed_at or db.now()))
                summary['announcements_succeeded'].append(code)
            except Exception as exc:
                log.exception('scan %s announcements %s failed', scan_id, code)
                summary['failures'].append({'code': code, 'stage': 'announcements', 'reason': str(exc)})
            try:
                close = normalize_close(source.valuation(code), code, at)
                summary['closes'][code] = close
                conn.execute('UPDATE subscriptions SET last_close=?,last_close_date=? WHERE company_key=?',
                             (close['last_close'], close['last_close_date'], stock_codes.normalize_key(code)))
            except Exception as exc:
                log.exception('scan %s valuation %s failed', scan_id, code)
                summary['failures'].append({'code': code, 'stage': 'valuation', 'reason': str(exc)})
        # Make L5 closes available to the same read-only card projection as explicit subscriptions.
        conn.execute('UPDATE scans SET summary=? WHERE id=?', (json.dumps(summary, ensure_ascii=False), scan_id))
        for subscription in scope:
            code = subscription['code']
            check_card = None
            try:
                check_card = card_or_none(conn, code, scan_id=scan_id)
                checked = _check_disclosures(conn, source, code, check_card, scan_id, fixed_at or db.now())
                summary['checks'][code] = checked or []
                if checked is not None:
                    summary['checks_succeeded'].append({'code': code, 'report_id': check_card['report_id']})
            except Exception as exc:
                log.exception('scan %s disclosure checks %s failed', scan_id, code)
                summary['check_failures'].append({'code': code, 'report_id': check_card['report_id'] if check_card else None,
                                                'stage': 'disclosure_checks', 'reason': str(exc)})
        conn.execute('UPDATE scans SET summary=? WHERE id=?', (json.dumps(summary, ensure_ascii=False), scan_id))
        notified_checks = {c['id'] for n in notices for c in n.get('checks', [])}
        notified_risks = {n['event_id'] for n in notices if n.get('reason') == 'major_risk' and 'event_id' in n}
        summary['queued_runs'] = _dispatch(conn, scan_id, fixed_at or db.now(), {s['code'] for s in scope}, history=history)
        completed = completed_reviews(conn, previous['started_at'] if previous else None, at)
        # Replay discoveries from failed scans, but not risk notices already saved with them.
        new_events = [e for e in events(conn) if e['scan_id'] > (previous['id'] if previous else 0)
                      and not (e['kind'] == 'major_risk' and e['id'] in notified_risks)]
        candidates = []
        for code in {s['code'] for s in scope} | {stock_codes.normalize_key(r['code']) for r in completed}:
            card = card_or_none(conn, code, scan_id=scan_id)
            summary['cards'][code] = card
            before = (previous['summary'].get('cards', {}).get(code) if previous else None) or summary['before'].get(code)
            if before and summary['before'].get(code) and before['report_id'] != summary['before'][code]['report_id']:
                before = before | {'status': summary['before'][code]['status']}
            if before and previous is None:
                before = before | {'status': '有效'}
            related = [e for e in events(conn, code=code)
                       if e['scan_id'] == scan_id or e['run_id'] in {r['id'] for r in completed}]
            checks = stored_checks(conn, card['report_id']) if card else []
            if checks:
                body = report_body.load(conn.execute('SELECT body FROM reports WHERE id=?', (card['report_id'],)).fetchone()['body'])
                items = {i['ref']: i for i in disclosure_checks.items(body)}
                checks = [c | {'detail': check_detail(c, items[c['ref']], body)} for c in checks if c['ref'] in items]
            for reason in notification_reasons(before, card, [e for e in new_events if e['code'] == code],
                                               [r for r in completed if stock_codes.normalize_key(r['code']) == code],
                                               checks=checks, new_checks=[c for c in checks if c['id'] not in notified_checks],
                                               last_extreme_zone=summary['last_extreme_zones'].get(code)):
                if code in entering and reason['reason'] == 'pending_review':
                    continue
                candidates.append({'code': code, 'name': (card or {}).get('name') or summary['names'].get(code), **reason,
                                   'card': card, 'path': f'/stock/{code}', 'card_path': f'/api/companies/{code}/card',
                                   'latencies': [latency(e, at) for e in related], 'notified_at': at})
            zone = ((card or {}).get('current') or {}).get('current_zone')
            old_zone = ((before or {}).get('current') or {}).get('current_zone')
            if zone in EXTREMES:
                summary['last_extreme_zones'][code] = zone
            elif old_zone in EXTREMES:
                summary['last_extreme_zones'][code] = old_zone
        trade_days = []
        # Emergency notices survive even a calendar failure later in this scan.
        summary['notify'] = [item for item in candidates if item.get('bypass_cooldown')
                             and allow_notification(item, notices, at, [])]
        cooling = {(c['code'], c['reason']) for c in candidates if not c.get('bypass_cooldown')}
        relevant = [n for n in notices if (n['code'], n['reason']) in cooling]
        if relevant:
            start = max(min(instant(n['notified_at']).date().isoformat() for n in relevant),
                        (instant(at) - timedelta(days=30)).date().isoformat())
            trade_days = [date.fromisoformat(str(d)).isoformat() for d in source.trade_days(start, at[:10])]
        for candidate in candidates:
            if not candidate.get('bypass_cooldown') and allow_notification(candidate, notices, at, trade_days):
                summary['notify'].append(candidate)
        summary['completed_runs'] = [r['id'] for r in completed]
        summary['backlog'] = [dict(e, **latency(e, at)) for e in events(conn) if e['action'] == 'deferred']
        conn.execute("UPDATE scans SET status='done',finished_at=?,summary=? WHERE id=?",
                     (db.now(), json.dumps(summary, ensure_ascii=False), scan_id))
    except Exception as exc:
        log.exception('scan %s failed', scan_id)
        conn.execute("UPDATE scans SET status='failed',finished_at=?,error=?,summary=? WHERE id=?",
                     (db.now(), str(exc), json.dumps(summary, ensure_ascii=False), scan_id))


def view(conn, scan_id: int) -> dict:
    row = conn.execute('SELECT * FROM scans WHERE id=?', (scan_id,)).fetchone()
    if row is None:
        raise service.RequestError('扫描不存在', 404)
    summary = json.loads(row['summary'] or '{}')
    return {'version': service.VERSION, **dict(row), 'summary': summary,
            'events': [dict(e, **latency(e, db.now())) for e in events(conn, scan_id=scan_id)],
            'notify': summary.get('notify', [])}


def weekly(conn, *, at: str | None = None) -> dict:
    at = at or db.now()
    since = instant(at) - timedelta(days=7)
    items = [s | {'card': card_or_none(conn, s['code'])} for s in subscriptions(conn)]
    all_events = events(conn)
    recent = [dict(e, **latency(e, at)) for e in all_events if instant(e['detected_at']) >= since]
    failures, gaps = [], []
    rows = [dict(r) | {'summary': json.loads(r['summary'] or '{}')} for r in conn.execute('SELECT * FROM scans ORDER BY id')]
    for row in rows:
        if instant(row['requested_at']) >= since:
            failures.extend({'scan_id': row['id'], 'kind': 'check_failure' if key == 'check_failures' else 'scan_failure', **f}
                            for key in ('failures', 'check_failures')
                            for f in row['summary'].get(key, []))
            # A gap stays reported for the week even after a later scan succeeds: it was never backfilled.
            gaps.extend(row['summary'].get('coverage_gaps', []))
            if row['status'] == 'failed':
                failures.append({'scan_id': row['id'], 'reason': row['error']})
    done = [r for r in rows if r['status'] == 'done']
    checkpoints = [{'code': item['code'], 'report_id': item['card']['report_id'], **checkpoint}
                   for item in items if item['card'] for checkpoint in item['card']['checkpoints']]
    return {'version': service.VERSION, 'since': since.isoformat(), 'as_of': at, 'subscriptions': items,
            'scan_scope': [{'code': s['code'], 'origin': s['origin']} for s in scan_scope(conn)],
            'status_distribution': dict(Counter((s['card'] or {}).get('status', '无论点卡') for s in items)),
            'zone_distribution': dict(Counter(((s['card'] or {}).get('current') or {}).get('current_zone') or '未测算' for s in items)),
            'events': recent, 'checkpoints': checkpoints, 'checkpoint_count': len(checkpoints),
            'forecast_count': sum(bool(c.get('forecast')) for c in checkpoints),
            'backlog': [dict(e, **latency(e, at)) for e in all_events if e['action'] == 'deferred'],
            'failures': failures,
            'last_scan': {k: done[-1][k] for k in ('id', 'started_at', 'finished_at')} if done else None,
            'coverage': (current := [coverage(done, s['code'], at) for s in items]),
            'coverage_gaps': list({(g['code'], g['uncovered_from']): g for g in
                                   gaps + [c | c['gap'] for c in current if c['gap']]}.values()),
            'open_events': [dict(e, **latency(e, at)) for s in items for e in open_events(conn, s['code'])],
            'review_failures': [dict(e, **latency(e, at)) for e in all_events if e['run_status'] == 'failed'
                                and e['judgment_completed_at'] and instant(e['judgment_completed_at']) >= since]}
