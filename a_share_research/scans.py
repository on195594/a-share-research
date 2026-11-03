"""Narrow disclosure discovery and K2 review scheduling; no account or notification writes."""

from collections import Counter
from datetime import date, datetime, timedelta
import json
import logging
import math
import re

from a_share_research import companies, db, service, valuation

log = logging.getLogger(__name__)
# Generic 风险提示 titles (trading-volatility or redemption notices) are routine and stay out of scope.
RISK = re.compile(r'立案|行政处罚|监管工作函|监管函|问询函|关注函|退市风险|终止上市|非标准|(?<!无)保留意见|无法表示意见|否定意见|重大诉讼|债务逾期')
FORECAST = re.compile('业绩预告|业绩快报|业绩修正|业绩预亏|业绩预减|业绩预盈|业绩预增')
PERIODIC = re.compile('年度报告|季度报告|年报|半年报|季报')
EXTREMES = {'测算支持区', '测算透支区'}


def classify(title: str) -> tuple[str, int] | None:
    if RISK.search(title):
        return 'major_risk', 2 if any(word in title for word in ('问询函', '关注函')) and '回复' in title else 3
    if FORECAST.search(title):
        return 'forecast', 2
    # Corrections have their own announcement identity and must remain discoverable.
    if PERIODIC.search(title) and not any(word in title for word in ('摘要', '英文', 'English', '取消')):
        return 'periodic', 2
    return None


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=db.SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(db.SHANGHAI)


def after_card(event: dict, completed_at: str) -> bool:
    """Announcements carry only a date: one disclosed before the card's completion day was available to it."""
    return instant(event['announced']).date() >= instant(completed_at).date()


def pending_events(events: list[dict], completed_at: str) -> list[dict]:
    pending = [e for e in events if e['severity'] >= 2 and after_card(e, completed_at)
               and e['action'] != 'covered' and not (e['run_status'] == 'done' and e['passed'] == 1)]
    triggered = [e for e in events if e['run_id'] is not None]
    if triggered:
        latest = max(triggered, key=lambda e: e['run_id'])
        if (latest['run_status'] == 'failed' and latest.get('judgment_completed_at')
                and instant(latest['judgment_completed_at']) > instant(completed_at) and latest not in pending):
            pending.append(latest)
    return pending


def review_order(events: list[dict], scan_id: int) -> list[str]:
    """Old backlog first; new companies by maximum severity then oldest announcement."""
    grouped = {}
    for event in events:
        grouped.setdefault(event['code'], []).append(event)

    def order(code):
        items = grouped[code]
        old = [e for e in items if e['scan_id'] != scan_id]
        return (0, min(e['detected_at'] for e in old), -max(e['severity'] for e in items), code) if old else (
            1, -max(e['severity'] for e in items), min(e['announced'] for e in items), code)
    return sorted(grouped, key=order)


def notification_reasons(before: dict | None, after: dict | None, events: list[dict],
                         completions: list[dict], *, last_extreme_zone: str | None = None) -> list[dict]:
    reasons = []
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
    items = {r['code']: dict(r) for r in conn.execute('SELECT * FROM subscriptions ORDER BY code')}
    batch = conn.execute('SELECT l5 FROM batches WHERE frozen_at IS NOT NULL AND l5 IS NOT NULL '
                         'ORDER BY frozen_at DESC, id DESC LIMIT 1').fetchone()
    if batch:
        for code in json.loads(batch['l5'])['codes']:
            code = code[:6]
            if code not in items:
                items[code] = {'code': code, 'origin': 'l5', **latest_close(conn, code)}
    return list(items.values())


def replace_subscriptions(conn, codes: list[str], *, catalog: dict | None = None) -> list[dict]:
    if not isinstance(codes, list) or any(not isinstance(c, str) or not re.fullmatch(r'[0-9]{6}', c) for c in codes):
        raise service.RequestError('订阅代码必须为六位 A 股代码')
    catalog = companies.stock_list() if catalog is None and codes else (catalog or {})
    unknown = sorted(set(codes) - catalog.keys())
    if unknown:
        raise service.RequestError('未知 A 股代码：' + '、'.join(unknown))
    with db.transaction(conn):
        for row in conn.execute("SELECT code FROM subscriptions WHERE origin='external'").fetchall():
            if row['code'] not in codes:
                conn.execute('DELETE FROM subscriptions WHERE code=?', (row['code'],))
        for code in sorted(set(codes)):
            conn.execute("INSERT INTO subscriptions(code, added_at) VALUES (?,?) ON CONFLICT(code) DO NOTHING",
                         (code, db.now()))
    return subscriptions(conn)


def submit(conn) -> int:
    with db.transaction(conn):
        row = conn.execute("SELECT id FROM scans WHERE status IN ('queued','running')").fetchone()
        if row:
            return row['id']
        return conn.execute("INSERT INTO scans(status,requested_at) VALUES ('queued',?)", (db.now(),)).lastrowid


def recover(conn) -> None:
    # Persisted discoveries remain deferred, so the next scan can resume without duplicating runs.
    conn.execute("UPDATE scans SET status='failed',finished_at=?,error='扫描因服务重启中断；已发现事件保留' "
                 "WHERE status='running'", (db.now(),))


def events(conn, *, scan_id: int | None = None, code: str | None = None) -> list[dict]:
    where, params = [], []
    for column, value in (('e.scan_id', scan_id), ('e.code', code)):
        if value is not None:
            where.append(column + '=?')
            params.append(value)
    rows = conn.execute(
        'SELECT e.*,u.status AS run_status,u.finished_at AS judgment_completed_at,r.passed '
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
    row = conn.execute('SELECT last_close,last_close_date FROM subscriptions WHERE code=?', (code,)).fetchone()
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


class MarketSources:
    """All network access is confined to the worker's collection boundary."""

    def announcements(self, code, days):
        from a_share_research import sources
        return sources._query_announcements(code, days, '', '', 100)

    def valuation(self, code):
        from a_share_research import sources
        return sources.valuation(code)

    def trade_days(self, start, end):
        from a_share_research import screening
        return screening._trade_days(screening._pro(), start.replace('-', ''), end.replace('-', ''))

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
    if str(row.get('ts_code', code))[:6] != code:
        raise companies.SourceError('估值行情返回其他公司')
    close = float(row['close'])
    day = date.fromisoformat(str(row['trade_date']))
    if not math.isfinite(close) or close <= 0 or day > instant(at).date():
        raise companies.SourceError('收盘价或行情日期无效')
    return {'last_close': close, 'last_close_date': day.isoformat()}


def completed_reviews(conn, since: str | None, at: str) -> list[dict]:
    rows = conn.execute("SELECT DISTINCT u.* FROM runs u JOIN scan_events e ON e.run_id=u.id "
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
                "SELECT r.body FROM reports r JOIN runs u ON u.id=r.run_id WHERE substr(r.code,1,6)=? "
                "AND r.id<? AND r.passed=1 AND u.status='done' AND u.kind='research' AND u.replay_of IS NULL "
                "AND json_extract(COALESCE(u.request,'{}'),'$.feedback') IS NULL ORDER BY r.id DESC LIMIT 1",
                (row['code'][:6], current['id'])).fetchone()
            if previous:
                def judgment(body):
                    body = json.loads(body)
                    return {'zone': (body.get('valuation_calc') or {}).get('zone'), 'verdict': body.get('verdict')}
                item['change'] = {'before': judgment(previous['body']), 'after': judgment(current['body'])}
                item['changed'] = item['change']['before'] != item['change']['after']
        result.append(item)
    return result


def _dispatch(conn, scan_id: int, at: str, scope: set[str]) -> list[int]:
    with db.transaction(conn):
        pending = [e for e in events(conn) if e['action'] == 'deferred' and e['code'] in scope]
        eligible = []
        for event in pending:
            card = card_or_none(conn, event['code'])
            if card is None:
                conn.execute("UPDATE scan_events SET action='no_card' WHERE id=?", (event['id'],))
            elif not after_card(event, card['completed_at']):
                # Already available when the current card was written; re-researching it adds nothing.
                conn.execute("UPDATE scan_events SET action='covered' WHERE id=?", (event['id'],))
            else:
                eligible.append(event)
        # Runs, rather than events, count toward the Shanghai calendar-day budget.
        used = sum(instant(r['created_at']).date() == instant(at).date() for r in conn.execute(
            'SELECT DISTINCT u.id,u.created_at FROM runs u JOIN scan_events e ON e.run_id=u.id'))
        selected = review_order(eligible, scan_id)[:max(0, 5 - used)]
        queued = []
        for code in selected:
            run_id = service.submit(conn, code, f'scan-{scan_id}-{code}')
            # Explicit at makes offline replay and midnight boundary behavior deterministic.
            conn.execute('UPDATE runs SET created_at=? WHERE id=?', (at, run_id))
            conn.execute("UPDATE scan_events SET action='queued_review',run_id=? WHERE code=? AND action='deferred'",
                         (run_id, code))
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
    summary = {'failures': [], 'closes': {}, 'cards': {}, 'notify': [], 'queued_runs': [], 'names': {},
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
        scope = subscriptions(conn)
        history = [dict(r) | {'summary': json.loads(r['summary'] or '{}')} for r in conn.execute(
            "SELECT * FROM scans WHERE id<? AND status='done' ORDER BY id", (scan_id,))]
        previous = history[-1] if history else None
        notices = [n for scan in history for n in scan['summary'].get('notify', [])]
        summary['last_extreme_zones'] = last_extreme_zones(history)
        summary['scope'] = scope
        summary['before'] = {s['code']: card_or_none(conn, s['code']) for s in scope}
        for subscription in scope:
            code = subscription['code']
            successful = next((s for s in reversed(history) if code in s['summary'].get('closes', {})
                               and not any(f['code'] == code for f in s['summary'].get('failures', []))), None)
            days = min(30, max(3, (instant(at).date() - instant(successful['started_at']).date()).days + 2)) if successful else 30
            try:
                rows, coverage = source.announcements(code, days)
                if coverage.get('truncated'):
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
                conn.execute('UPDATE subscriptions SET last_close=?,last_close_date=? WHERE code=?',
                             (close['last_close'], close['last_close_date'], code))
            except Exception as exc:
                log.exception('scan %s valuation %s failed', scan_id, code)
                summary['failures'].append({'code': code, 'stage': 'valuation', 'reason': str(exc)})
        # Make L5 closes available to the same read-only card projection as explicit subscriptions.
        conn.execute('UPDATE scans SET summary=? WHERE id=?', (json.dumps(summary, ensure_ascii=False), scan_id))
        summary['queued_runs'] = _dispatch(conn, scan_id, fixed_at or db.now(), {s['code'] for s in scope})
        completed = completed_reviews(conn, previous['started_at'] if previous else None, at)
        new_events = [e for e in events(conn) if e['scan_id'] > (previous['id'] if previous else 0)]
        candidates = []
        for code in {s['code'] for s in scope} | {r['code'][:6] for r in completed}:
            card = card_or_none(conn, code, scan_id=scan_id)
            summary['cards'][code] = card
            before = (previous['summary'].get('cards', {}).get(code) if previous else None) or summary['before'].get(code)
            if before and summary['before'].get(code) and before['report_id'] != summary['before'][code]['report_id']:
                before = before | {'status': summary['before'][code]['status']}
            if before and previous is None:
                before = before | {'status': '有效'}
            related = [e for e in events(conn, code=code)
                       if e['scan_id'] == scan_id or e['run_id'] in {r['id'] for r in completed}]
            for reason in notification_reasons(before, card, [e for e in new_events if e['code'] == code],
                                               [r for r in completed if r['code'][:6] == code],
                                               last_extreme_zone=summary['last_extreme_zones'].get(code)):
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
    failures = []
    for row in conn.execute('SELECT * FROM scans ORDER BY id'):
        if instant(row['requested_at']) >= since:
            failures.extend({'scan_id': row['id'], **f} for f in json.loads(row['summary'] or '{}').get('failures', []))
            if row['status'] == 'failed':
                failures.append({'scan_id': row['id'], 'reason': row['error']})
    checkpoints = [{'code': item['code'], 'report_id': item['card']['report_id'], **checkpoint}
                   for item in items if item['card'] for checkpoint in item['card']['checkpoints']]
    return {'version': service.VERSION, 'since': since.isoformat(), 'as_of': at, 'subscriptions': items,
            'status_distribution': dict(Counter((s['card'] or {}).get('status', '无论点卡') for s in items)),
            'zone_distribution': dict(Counter(((s['card'] or {}).get('current') or {}).get('current_zone') or '未测算' for s in items)),
            'events': recent, 'checkpoints': checkpoints, 'checkpoint_count': len(checkpoints),
            'forecast_count': sum(bool(c.get('forecast')) for c in checkpoints),
            'backlog': [dict(e, **latency(e, at)) for e in all_events if e['action'] == 'deferred'],
            'failures': failures,
            'review_failures': [dict(e, **latency(e, at)) for e in all_events if e['run_status'] == 'failed'
                                and e['judgment_completed_at'] and instant(e['judgment_completed_at']) >= since]}
