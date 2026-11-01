"""A4 offline replay: captured disclosures, saved reports, closes and SSE calendar.

Severity/correction/failure mutations below are explicitly adversarial changes to these
captures, not newly fabricated financial evidence or claims of live coverage.
"""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3

import pytest

from a_share_research import db, scans, service, valuation
from support.continuation import BODY as CAPTURED_BODIES, load
from support.valuation import BODY, RECORD, assumptions
from test_service_http import server

DATA = Path(__file__).parent / 'data'
ANNOUNCEMENTS = sum((json.loads((DATA / f).read_text())['items'] for f in
                     ('announcements.json', 'periodic-announcements.json')), [])
CATALOG = {item['code']: item['raw'] for item in ANNOUNCEMENTS}
DAYS = [date.fromisoformat(d).isoformat() for d in json.loads((DATA / 'backtest-dates.json').read_text())['days']]
AT = '2026-10-30T18:00:00+08:00'


class CapturedSources:
    def __init__(self, items=ANNOUNCEMENTS):
        self.items = deepcopy(items)
        self.windows = []
        self.calendar_windows = []

    def announcements(self, code, days):
        self.windows.append((code, days))
        # Like other raw replay adapters, retain the recorded response without fabricating
        # observations for uncaptured dates. Window arguments are asserted separately.
        return [i['raw'] for i in self.items if i['code'] == code], {'truncated': False}

    def valuation(self, code):
        if code == '600519':
            return json.loads((DATA / '600519-financials.json').read_text())['market']
        if code == '600900':
            return json.loads((DATA / '600900-61d.json').read_text())['stock'][-1]
        if code == '603606':
            row = next(r for r in BODY['table'] if r['id'] == 'M.close')
            day, close = next(iter(row['values'].items()))
            return {'ts_code': RECORD['code'], 'trade_date': day, 'close': close}
        raise LookupError(f'无该公司真实行情夹具：{code}')

    def trade_days(self, start, end):
        self.calendar_windows.append((start, end))
        return [d for d in DAYS if start <= d <= end]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'scans.db')
    load(connection)
    yield connection
    connection.close()


def run_scan(conn, source=None, at=AT):
    scan_id = scans.submit(conn)
    scans.execute(conn, scan_id, source=source or CapturedSources(), at=at)
    return scans.view(conn, scan_id)


def priced(conn):
    body = deepcopy(BODY)
    body['valuation_calc'] = valuation.compute(assumptions(), body['table'], body['market_date'])
    # Extend the existing captured report exactly as the A2 projection test does.
    conn.execute('UPDATE reports SET body=? WHERE id=39', (json.dumps(body, ensure_ascii=False),))
    return body


def complete_cards_at(conn, codes=('603606',), at='2026-01-01T18:00:00+08:00'):
    # Explicitly shift captured card timestamps before disclosures for new-event replays.
    # Equal report dates retain the latest captured card by report id.
    for code in codes:
        conn.execute('UPDATE runs SET finished_at=? WHERE id IN '
                     '(SELECT run_id FROM reports WHERE substr(code,1,6)=?)', (at, code))
        conn.execute('UPDATE reports SET created_at=? WHERE substr(code,1,6)=?', (at, code))


def test_classify_captured_titles_and_adversarial_mutations():
    for item in ANNOUNCEMENTS:
        title = item['raw']['announcementTitle']
        expected = ('periodic', 2) if item.get('period') else ('forecast', 2) if '快报' in title else None
        assert scans.classify(title) == expected
    title = next(i['raw']['announcementTitle'] for i in ANNOUNCEMENTS if i.get('period'))
    for excluded in ('摘要', '英文', 'English', '取消'):
        assert scans.classify(title + excluded) is None
    assert scans.classify(title + '更正公告') == ('periodic', 2)
    for risk in ('立案', '行政处罚', '监管函', '问询函', '关注函', '退市风险', '终止上市',
                 '非标准', '保留意见', '无法表示意见', '否定意见', '重大诉讼', '债务逾期'):
        assert scans.classify(title + risk) == ('major_risk', 3)
    assert scans.classify(title + '问询函回复') == ('major_risk', 2)
    assert scans.classify(title + '延期回复问询函') == ('major_risk', 2)


@pytest.mark.parametrize('title', [
    '关于股票交易异常波动的风险提示公告',
    '关于可转债赎回的风险提示公告',
])
def test_routine_risk_notice_is_not_major_risk(title):
    # Explicit title mutations exercise routine notices without claiming a new capture.
    assert scans.classify(title) is None
    assert scans.classify(title + '（收到问询函）') == ('major_risk', 3)


@pytest.mark.parametrize(('title', 'expected'), [
    ('关于会计师事务所出具标准无保留意见审计报告的专项说明', None),
    ('关于会计师事务所出具保留意见审计报告的专项说明', ('major_risk', 3)),
    ('关于会计师事务所出具非标准无保留意见审计报告的专项说明', ('major_risk', 3)),
    ('关于收到上海证券交易所监管工作函的公告', ('major_risk', 3)),
    ('2025年半年度业绩预亏公告', ('forecast', 2)),
    ('2025年年度业绩预减公告', ('forecast', 2)),
    ('2025年年度业绩预盈公告', ('forecast', 2)),
    ('2025年年度业绩预增公告', ('forecast', 2)),
    ('关于业绩预告事项收到中国证监会立案告知书的公告', ('major_risk', 3)),
    ('关于业绩预告信息披露违法收到行政处罚决定书的公告', ('major_risk', 3)),
    ('关于对上海证券交易所问询函的回复公告', ('major_risk', 2)),
    ('关于对深圳证券交易所关注函的回复公告', ('major_risk', 2)),
    ('关于收到深圳证券交易所关注函的公告', ('major_risk', 3)),
])
def test_disclosure_title_style_mutations(title, expected):
    # Adversarial title variants, not claims of additional captured disclosures.
    assert scans.classify(title) == expected


@pytest.mark.parametrize(('completed_at', 'action', 'status', 'count'), [
    ('2026-10-30T00:00:00+08:00', 'covered', '有效', 0),
    ('2026-10-29T23:59:59+08:00', 'queued_review', '待复核', 1),
])
def test_disclosure_before_card_is_covered_but_same_day_triggers_review(conn, completed_at, action, status, count):
    complete_cards_at(conn, at=completed_at)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    source = CapturedSources([i for i in ANNOUNCEMENTS
                              if i['code'] == '603606' and i.get('period') == '2026-09-30'])
    before = service.company_card(conn, '603606')
    runs_before = conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
    result = run_scan(conn, source)
    assert result['status'] == 'done' and not result['summary']['failures']
    assert len(result['events']) == 1
    event = result['events'][0]
    assert event['announced'] == '2026-10-29'
    assert event['action'] == action
    assert len(result['summary']['queued_runs']) == count
    assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == runs_before + count
    assert event['run_id'] == (result['summary']['queued_runs'][0] if count else None)
    card = service.company_card(conn, '603606')
    assert card['report_id'] == before['report_id']
    assert card['completed_at'] == completed_at
    assert card['status'] == status
    assert [e['id'] for e in card['pending_events']] == ([event['id']] if count else [])
    assert any(n['reason'] == 'pending_review' for n in result['notify']) == bool(count)


def test_v7_empty_and_v6_migration_preserves_all_old_records(tmp_path):
    path = tmp_path / 'v6.db'
    old = sqlite3.connect(path, isolation_level=None)
    old.row_factory = sqlite3.Row
    old.executescript(db.SCHEMA)
    for migration in db.MIGRATIONS[:6]:
        migration(old)
    old.execute('PRAGMA user_version=6')
    load(old)
    before = list(old.iterdump())
    old.close()
    upgraded = db.connect(path)
    assert upgraded.execute('PRAGMA user_version').fetchone()[0] == 7
    assert set(before) <= set(upgraded.iterdump())
    dump = list(upgraded.iterdump())
    upgraded.close()
    for database in (path, tmp_path / 'empty.db'):
        connection = db.connect(database)
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 7
        with db.transaction(connection):
            db._migrate_v7(connection)
        if database == path:
            assert list(connection.iterdump()) == dump
            assert connection.total_changes == 0
        connection.close()


def test_subscriptions_replace_atomic_validation_and_l5(conn):
    scans.replace_subscriptions(conn, ['603606', '600519'], catalog=CATALOG)
    conn.execute("UPDATE subscriptions SET last_close=36.03,last_close_date='2026-10-26' WHERE code='603606'")
    before = [dict(r) for r in conn.execute('SELECT * FROM subscriptions')]
    for invalid in (['999999'], ['603606.SH'], ['６０３６０６'], [603606]):
        with pytest.raises(service.RequestError):
            scans.replace_subscriptions(conn, invalid, catalog=CATALOG)
        assert [dict(r) for r in conn.execute('SELECT * FROM subscriptions')] == before
    conn.execute("INSERT INTO batches(status,created_at,rule,l5,frozen_at) VALUES ('frozen',?,'{}',?,?)",
                 (AT, json.dumps({'codes': ['600900.SH', '600519.SH']}), AT))
    scans.replace_subscriptions(conn, ['603606', '603606'], catalog=CATALOG)
    items = {r['code']: r for r in scans.subscriptions(conn)}
    assert items['600900']['origin'] == items['600519']['origin'] == 'l5'
    assert items['603606']['last_close'] == 36.03
    assert conn.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 1
    result = run_scan(conn)
    assert result['status'] == 'done' and not result['summary']['failures']
    assert result['summary']['closes']['600900']['last_close'] > 0
    assert scans.latest_close(conn, '600900')['last_close_date']
    items = {r['code']: r for r in scans.subscriptions(conn)}
    for code in ('600900', '600519'):
        assert items[code]['last_close'] == result['summary']['closes'][code]['last_close']
        assert items[code]['last_close_date'] == result['summary']['closes'][code]['last_close_date']
    assert conn.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 1


def test_duplicate_scan_is_idempotent_and_k2_request_is_empty(conn):
    complete_cards_at(conn)
    priced(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    scan_id = scans.submit(conn)
    assert scans.submit(conn) == scan_id
    source = CapturedSources()
    scans.execute(conn, scan_id, source=source, at=AT)
    first = scans.view(conn, scan_id)
    assert first['status'] == 'done' and not first['summary']['failures']
    assert len(first['events']) == 4 and len(first['summary']['queued_runs']) == 1
    run_id = first['summary']['queued_runs'][0]
    request = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    assert request['request_id'] == f'scan-{scan_id}-603606'
    assert json.loads(request['request']) == {'question': '', 'feedback': None, 'parent_report_id': None}
    card = service.company_card(conn, '603606')
    assert card['status'] == '待复核' and len(card['pending_events']) == 4
    assert card['current']['current_zone'] == scans.current_zone(priced(conn)['valuation_calc'], 36.03)
    assert card['current']['note'] == '待复核，区间暂停解读'
    assert any(n['reason'] == 'pending_review' for n in first['notify'])
    second = run_scan(conn, source, '2026-10-31T18:00:00+08:00')
    assert second['events'] == [] and second['summary']['queued_runs'] == [] and not second['notify']
    assert len(scans.events(conn)) == 4
    assert source.windows == [('603606', 30), ('603606', 3)]
    # Database-level identity protects every writer, not only this scanner's SELECT path.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action) '
                     'SELECT scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action FROM scan_events LIMIT 1')


def test_correction_identity_and_no_card_are_retained(conn):
    scans.replace_subscriptions(conn, ['000963'], catalog=CATALOG)
    source = CapturedSources()
    first = run_scan(conn, source)
    assert all(e['action'] == 'no_card' for e in first['events'])
    assert first['summary']['failures'][0]['stage'] == 'valuation'
    original = next(i for i in source.items if i['code'] == '000963')
    correction = deepcopy(original)
    # Identity mutation models a separately published correction, not a fake official capture.
    correction['raw']['announcementId'] += '-correction'
    correction['raw']['announcementTitle'] += '（更正公告）'
    source.items.append(correction)
    second = run_scan(conn, source)
    assert len(second['events']) == 1 and second['events'][0]['action'] == 'no_card'
    assert len(scans.events(conn)) == 3


def test_failures_never_become_no_events_and_other_codes_continue(conn):
    scans.replace_subscriptions(conn, ['603606', '600519'], catalog=CATALOG)
    source = CapturedSources()
    # A malformed captured timestamp must fail at the data boundary.
    next(i for i in source.items if i['code'] == '603606')['raw']['announcementTime'] = None
    first = run_scan(conn, source)
    assert first['status'] == 'done'
    assert first['summary']['failures'][0]['code'] == '603606'
    assert first['summary']['failures'][0]['stage'] == 'announcements'
    assert first['summary']['closes']['600519']['last_close'] == 1318.49
    assert not first['events']
    healthy = CapturedSources()
    second = run_scan(conn, healthy)
    assert second['events'] and healthy.windows == [('600519', 3), ('603606', 30)]
    report = scans.weekly(conn, at=AT)
    assert report['failures'] and report['events']
    assert all('disclosure_to_detection_seconds' in e for e in report['events'])


def test_pending_state_and_failed_review_completion(conn):
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    first = run_scan(conn)
    run = first['summary']['queued_runs'][0]
    for status in ('queued', 'running', 'failed', 'done'):
        db.update_run(conn, run, status=status)
        assert service.company_card(conn, '603606')['status'] == '待复核'
    db.update_run(conn, run, status='failed', error='复核未通过', finished_at='2026-10-31T10:00:00+08:00')
    second = run_scan(conn, at='2026-10-31T18:00:00+08:00')
    failure = next(n for n in second['notify'] if n['reason'] == 'review_failed')
    assert failure['latencies'][0]['judgment_completed_at'] == '2026-10-31T10:00:00+08:00'
    assert service.company_card(conn, '603606')['status'] == '待复核'
    body = deepcopy(BODY)
    db.save_report(conn, run, RECORD['code'], RECORD['name'], RECORD['period'], True, RECORD['verdict'], body)
    db.update_run(conn, run, status='done', finished_at='2026-10-31T19:00:00+08:00')
    assert service.company_card(conn, '603606')['status'] == '有效'
    assert service.company_card(conn, '603606')['pending_events'] == []
    third = run_scan(conn, at='2026-11-01T18:00:00+08:00')
    assert not any(n['reason'] == 'review_failed' for n in third['notify'])


def test_severity_order_daily_budget_backlog_and_shanghai_date(conn):
    # Use real company identities/reports and mutate event severity only, never financial inputs.
    records = json.loads((DATA / 'continuation-records.json').read_text())['records']
    codes = sorted({r['report']['code'][:6] for r in records})
    complete_cards_at(conn, codes)
    scan_id = scans.submit(conn)
    base = scans.normalize_announcements([ANNOUNCEMENTS[0]['raw']], ANNOUNCEMENTS[0]['code'])[0]
    for code in codes:
        conn.execute('INSERT INTO subscriptions(code,added_at) VALUES (?,?)', (code, AT))
        conn.execute('INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action) '
                     "VALUES (?,?,?,?,?,?,?,?,?,'deferred')", (scan_id, code, base['doc_key'], base['title'], base['announced'],
                       base['url'], base['kind'], 3 if code == codes[-1] else 2, AT))
    ordered = scans.review_order(scans.events(conn), scan_id)
    assert ordered[0] == codes[-1]
    # Consume four slots with four recorded event→run relationships in this day's ledger.
    original = scans.events(conn)[0]
    for i in range(4):
        run = service.submit(conn, original['code'], f'scan-budget-{i}')
        db.update_run(conn, run, created_at='2026-10-30T00:00:01+08:00')
        conn.execute('INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action,run_id) '
                     "VALUES (?,?,?,?,?,?,?,?,?,'queued_review',?)", (scan_id, original['code'], original['doc_key'] + str(i),
                       original['title'], original['announced'], original['url'], original['kind'], 2, AT, run))
    queued = scans._dispatch(conn, scan_id, AT, set(codes))
    assert len(queued) == 1
    assert conn.execute('SELECT code FROM runs WHERE id=?', (queued[0],)).fetchone()[0] == codes[-1]
    assert scans._dispatch(conn, scan_id, AT, set(codes)) == []
    # UTC 16:00 is the start of the next Shanghai day.
    assert len(scans._dispatch(conn, scan_id, '2026-10-30T16:00:00+00:00', set(codes))) == 2
    old = original | {'scan_id': scan_id - 1, 'detected_at': '2026-10-29T18:00:00+08:00'}
    fresh = original | {'code': codes[-1], 'severity': 3}
    assert scans.review_order([fresh, old], scan_id)[0] == old['code']


def test_notify_rules_cooldown_holidays_and_major_risk_exemption(conn):
    card = service.company_card(conn, '603606')
    before = card | {'current': {'current_zone': '中性区'}}
    after = card | {'status': '待复核', 'current': {'current_zone': '测算支持区'}}
    event = scans.normalize_announcements([ANNOUNCEMENTS[0]['raw']], '600900')[0]
    event = event | {'id': 1, 'kind': 'major_risk'}  # Severity mutation, not a captured risk assertion.
    reasons = scans.notification_reasons(before, after, [event], [{'id': 1, 'status': 'failed', 'error': '复核失败'}])
    assert {n['reason'] for n in reasons} == {'major_risk', 'pending_review', 'current_zone_changed', 'review_failed'}
    for reason in reasons:
        item = reason | {'code': '603606'}
        history = [item | {'notified_at': '2026-09-30T18:00:00+08:00'}]
        assert scans.allow_notification(item, history, '2026-10-01T18:00:00+08:00', DAYS) == bool(item.get('bypass_cooldown'))
        if not item.get('bypass_cooldown'):
            assert not scans.allow_notification(item, history, '2026-10-09T18:00:00+08:00', DAYS)
            assert scans.allow_notification(item, history, '2026-10-16T18:00:00+08:00', DAYS)
    item = next(r for r in reasons if r['reason'] == 'current_zone_changed') | {'code': '603606'}
    history = [item | {'notified_at': AT}]
    reverse = item | {'from_zone': item['to_zone'], 'to_zone': item['from_zone']}
    assert not scans.allow_notification(reverse, history, AT, DAYS)
    assert scans.notification_reasons(before, before, [], []) == []


@pytest.mark.parametrize(('first', 'opposite'), [
    ('测算支持区', '测算透支区'), ('测算透支区', '测算支持区'),
])
def test_zone_reversal_across_neutral_and_boundary_oscillation(conn, first, opposite):
    card = service.company_card(conn, '603606')
    history, notices = [], []
    before = card | {'current': {'current_zone': '中性区'}}
    for zone, allowed in [(first, True), ('中性区', False), (first, False),
                          ('中性区', False), (opposite, True), (first, True)]:
        # Mutate only the observed zone state of a captured card, not financial evidence.
        after = card | {'current': {'current_zone': zone}}
        memory = scans.last_extreme_zones(history)
        reason, = scans.notification_reasons(before, after, [], [], last_extreme_zone=memory.get('603606'))
        item = reason | {'code': '603606', 'notified_at': AT}
        assert scans.allow_notification(item, notices, AT, DAYS) == allowed
        assert reason['bypass_cooldown'] == (zone in scans.EXTREMES and memory.get('603606') in scans.EXTREMES
                                             and zone != memory['603606'])
        emitted = [item] if allowed else []
        notices.extend(emitted)
        history.append({'summary': {'cards': {'603606': after}, 'notify': emitted}})
        before = after
    assert scans.last_extreme_zones(history) == {'603606': first}


@pytest.mark.parametrize(('notified_at', 'expected_start'), [
    ('2026-01-01T18:00:00+08:00', '2026-09-30'),
    ('2026-10-29T18:00:00+08:00', '2026-10-29'),
])
def test_calendar_window_and_extreme_memory_persist_between_scans(conn, notified_at, expected_start):
    priced(conn)
    complete_cards_at(conn, at=AT)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    first = run_scan(conn)
    summary = first['summary']
    # Mutate a past observed state; the next scan still consumes the actual captured close.
    summary['cards']['603606']['current']['current_zone'] = '测算支持区'
    summary['notify'] = [{'code': '603606', 'reason': 'current_zone_changed',
                          'from_zone': '中性区', 'to_zone': '测算支持区', 'notified_at': notified_at},
                         {'code': '600519', 'reason': 'current_zone_changed', 'notified_at': '2025-01-01'}]
    conn.execute('UPDATE scans SET summary=? WHERE id=?', (json.dumps(summary), first['id']))
    source = CapturedSources()
    second = run_scan(conn, source)
    assert second['status'] == 'done'
    assert source.calendar_windows == [(expected_start, AT[:10])]
    assert second['summary']['last_extreme_zones'] == {'603606': '测算支持区'}
    assert second['summary']['cards']['603606']['current']['current_zone'] == '中性区'
    # A scan outside this code's subscription scope must not erase its memory.
    scans.replace_subscriptions(conn, [], catalog=CATALOG)
    third = run_scan(conn)
    assert third['summary']['last_extreme_zones'] == {'603606': '测算支持区'}


def test_scan_failure_recovery_and_unknown_scan(conn):
    scan_id = scans.submit(conn)
    conn.execute("UPDATE scans SET status='running' WHERE id=?", (scan_id,))
    scans.recover(conn)
    assert scans.view(conn, scan_id)['status'] == 'failed'
    with pytest.raises(service.RequestError) as exc:
        scans.view(conn, scan_id + 100)
    assert exc.value.status == 404
    scan_id = scans.submit(conn)
    # Invalid stored batch data represents an overall scan failure, not a company with no event.
    conn.execute("INSERT INTO batches(status,created_at,rule,l5,frozen_at) VALUES ('frozen',?,'{}','invalid',?)", (AT, AT))
    scans.execute(conn, scan_id, source=CapturedSources(), at=AT)
    assert scans.view(conn, scan_id)['status'] == 'failed'
    assert scans.view(conn, scan_id)['error']


def test_api_auth_single_active_and_weekly_route(server):
    conn, client, url, *_ = server
    for path in ('/api/subscriptions', '/api/scans/weekly', '/api/scans/1'):
        assert client.get(url + path, headers={'Authorization': 'Bearer invalid'}, timeout=3).status_code == 401
    assert client.put(url + '/api/subscriptions', json={'codes': ['bad']}, timeout=3).status_code == 422
    assert client.put(url + '/api/subscriptions', json={'codes': []}, timeout=3).status_code == 200
    first = client.post(url + '/api/scans', timeout=3)
    second = client.post(url + '/api/scans', timeout=3)
    assert first.status_code == 202 and first.json() == second.json()
    assert client.get(url + '/api/scans/weekly', timeout=3).json()['subscriptions'] == []
    assert client.get(url + f"/api/scans/{first.json()['scan_id']}", timeout=3).json()['status'] == 'queued'
    assert client.get(url + '/api/scans/999', timeout=3).status_code == 404


def test_judgment_change_is_notified_once_and_weekly_counts_current_agenda(conn):
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    first = run_scan(conn)
    run_id = first['summary']['queued_runs'][0]
    body = deepcopy(BODY)
    body['checkpoints'] = deepcopy(CAPTURED_BODIES[39]['checkpoints'])
    body['valuation_calc'] = valuation.compute(assumptions(), body['table'], body['market_date'])
    db.save_report(conn, run_id, RECORD['code'], RECORD['name'], RECORD['period'], True, RECORD['verdict'], body)
    db.update_run(conn, run_id, status='done', finished_at='2026-10-31T10:00:00+08:00')
    second = run_scan(conn, at='2026-10-31T18:00:00+08:00')
    notice = next(n for n in second['notify'] if n['reason'] == 'judgment_changed')
    assert notice['change']['before']['zone'] is None
    assert notice['change']['after']['zone'] == body['valuation_calc']['zone']
    assert notice['card']['status'] == '有效'
    assert notice['latencies'] and notice['path'] == '/stock/603606'
    third = run_scan(conn, at='2026-11-01T18:00:00+08:00')
    assert not third['notify']
    weekly = scans.weekly(conn, at='2026-11-01T18:00:00+08:00')
    assert weekly['status_distribution'] == {'有效': 1}
    assert weekly['checkpoint_count'] == len(body['checkpoints'])
    assert weekly['forecast_count'] == sum(bool(c.get('forecast')) for c in body['checkpoints'])
    assert not weekly['backlog'] and not weekly['failures']


def test_major_risk_bypasses_scan_cooldown_and_has_no_card_name(conn):
    scans.replace_subscriptions(conn, ['600900'], catalog=CATALOG)
    source = CapturedSources()
    raw = next(i['raw'] for i in source.items if i['code'] == '600900')
    raw['announcementTitle'] += '问询函'  # Explicit adversarial risk-title mutation.
    first = run_scan(conn, source)
    assert first['notify'][0]['reason'] == 'major_risk'
    assert first['notify'][0]['name'] == raw['secName']
    assert first['events'][0]['action'] == 'no_card'
    raw['announcementId'] += '-correction'
    second = run_scan(conn, source)
    assert second['status'] == 'done'
    assert len(second['notify']) == 1 and second['notify'][0]['reason'] == 'major_risk'
    assert not run_scan(conn, source)['notify']


def test_truncated_directory_does_not_silently_succeed(conn):
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)

    class TruncatedCapture(CapturedSources):
        def announcements(self, code, days):
            rows, coverage = super().announcements(code, days)
            return rows, coverage | {'truncated': True}

    result = run_scan(conn, TruncatedCapture())
    assert result['summary']['failures'][0]['reason'] == '公告目录分页截断，不能确认扫描完整'
    assert result['events'] == [] and not result['summary']['queued_runs']


def test_new_events_order_by_announcement_and_backlog_age():
    rows = [scans.normalize_announcements([i['raw']], i['code'])[0] for i in ANNOUNCEMENTS if i.get('period')]
    rows = [r | {'scan_id': 2, 'detected_at': AT} for r in rows]
    expected = sorted({r['code'] for r in rows}, key=lambda c: min(r['announced'] for r in rows if r['code'] == c))
    assert scans.review_order(rows, 2) == expected
    event = rows[0] | {'action': 'deferred', 'judgment_completed_at': None}
    assert scans.latency(event, '2026-11-01T18:00:00+08:00')['backlog_days'] == 2


def test_queue_concurrency_and_release_idle_guard(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from a_share_research import deploy
    path = tmp_path / 'queue.db'
    connection = db.connect(path)

    def enqueue(_):
        other = db.connect(path)
        try:
            return scans.submit(other)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(enqueue, range(4)))
    assert len(set(ids)) == 1
    for status in ('queued', 'running'):
        connection.execute('UPDATE scans SET status=?', (status,))
        with pytest.raises(RuntimeError, match='扫描'):
            deploy.idle(path)
    connection.execute("UPDATE scans SET status='done'")
    deploy.idle(path)
    connection.execute('DROP TABLE scan_events')
    connection.execute('DROP TABLE scans')
    deploy.idle(path)  # v6 pre-migration compatibility.
    connection.close()
