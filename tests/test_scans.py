"""A4 offline replay: captured disclosures, saved reports, closes and SSE calendar.

Severity/correction/failure mutations below are explicitly adversarial changes to these
captures, not newly fabricated financial evidence or claims of live coverage.
"""
from copy import deepcopy
from datetime import datetime
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, companies, db, disclosure_checks, overview, scans, service, stance, valuation
from a_share_research.tools.registry import Context, invoke
from support.continuation import BODY as CAPTURED_BODIES, load
from support.valuation import BODY, RECORD, STORED, assumptions
from support.scans import (
    ANNOUNCEMENTS,
    AT,
    CATALOG,
    DATA,
    DAYS,
    DISCLOSURES,
    CapturedSources,
    captured_events,
    cite_in_card,
    citing,
    complete_cards_at,
    priced,
    run_scan,
)


def test_consensus_prefetch_runs_without_subscriptions(conn, monkeypatch):
    from support.market_replay import DailyReports

    from a_share_research import market
    pro = DailyReports()
    monkeypatch.setattr(market, 'client', lambda: pro)
    monkeypatch.setenv('TUSHARE_TOKEN', 'captured-response-only')
    monkeypatch.delenv('TUSHARE_CACHE', raising=False)
    monkeypatch.setenv('REPORT_RC_DAILY_BUDGET', '1')

    class Prefetch(CapturedSources):
        def report_rc_window(self, as_of):
            return scans.MarketSources().report_rc_window(as_of)

    result = run_scan(conn, Prefetch(), at='2026-08-27T18:00:00+08:00')
    prefetch = result['summary']['consensus_prefetch']
    assert prefetch['covered'] == 1 and len(prefetch['missing']) == prefetch['trading_days'] - 1
    assert '预算' in prefetch['reason']
    assert result['status'] == 'done' and result['summary']['failures'] == []
    assert scans.ScanHistory(conn, fields=('failures', 'scope', 'announcements_succeeded')).announcements.get('603606') is None


def test_consensus_prefetch_error_is_not_an_announcement_failure(conn):
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)

    class FailedPrefetch(CapturedSources):
        def report_rc_window(self, as_of):
            raise RuntimeError('卖方预测频次超限')

    result = run_scan(conn, FailedPrefetch())
    assert result['status'] == 'done' and result['summary']['failures'] == []
    assert result['summary']['announcements_succeeded'] == ['603606']
    assert '频次超限' in result['summary']['consensus_prefetch']['reason']
    assert scans.ScanHistory(conn, fields=('failures', 'scope', 'announcements_succeeded')).announcements.get('603606') is None


def test_classify_captured_titles_and_adversarial_mutations():
    for item in ANNOUNCEMENTS:
        title = item['raw']['announcementTitle']
        expected = ('periodic', 2) if item.get('period') else ('forecast', 2) if '快报' in title else None
        assert scans.classify(title) == expected
    title = next(i['raw']['announcementTitle'] for i in ANNOUNCEMENTS if i.get('period'))
    for excluded in ('摘要', '英文', 'English', '取消'):
        assert scans.classify(title + excluded) is None
    assert scans.classify(title + '更正公告') == ('periodic', 2)
    for risk in ('监管函', '退市风险', '终止上市', '无法表示意见', '否定意见', '重大诉讼', '债务逾期'):
        assert scans.classify(title + risk) == ('major_risk', 3)
    assert scans.classify(title + '延期回复问询函') == ('major_risk', 2)


@pytest.mark.parametrize(('title', 'expected'), [
    ('关于股票交易异常波动的风险提示公告', None),
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


@pytest.mark.parametrize(('completed_at', 'cites', 'action', 'status', 'count'), [
    ('2026-10-30T00:00:00+08:00', True, 'covered', '有效', 0),
    # A newer report that never read the announcement does not cover it.
    ('2026-10-30T00:00:00+08:00', False, 'queued_review', '待复核', 1),
    ('2026-10-29T23:59:59+08:00', False, 'queued_review', '待复核', 1),
])
def test_disclosure_is_covered_only_when_newer_card_cites_it(conn, completed_at, cites, action, status, count):
    complete_cards_at(conn, at=completed_at)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    source = CapturedSources([i for i in ANNOUNCEMENTS
                              if i['code'] == '603606' and i.get('period') == '2026-09-30'])
    if cites:
        cite_in_card(conn, '603606', captured_events(source, '603606'))
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
    if count:
        assert card['pending_events'][0]['review']['state'] == 'researching'
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
    tables = ('runs', 'reports', 'prices', 'batches', 'calls')
    # Column order changes in v9, so compare stored rows by name rather than iterdump's positional INSERTs.
    def rows(connection, table):
        names = [row[1] for row in connection.execute(f'PRAGMA table_info({table})')]
        ordered = sorted(names)
        return ordered, connection.execute(f"SELECT {', '.join(ordered)} FROM {table} ORDER BY 1").fetchall()
    before = {table: rows(old, table) for table in tables}
    old.close()
    upgraded = db.connect(path)
    assert upgraded.execute('PRAGMA user_version').fetchone()[0] == len(db.MIGRATIONS)
    for table in tables:
        names, stored = before[table]
        kept = [names.index(name) for name in names]
        assert [tuple(row[i] for i in kept) for row in stored] == [
            tuple(row) for row in upgraded.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY 1")]
    dump = list(upgraded.iterdump())
    upgraded.close()
    for database in (path, tmp_path / 'empty.db'):
        connection = db.connect(database)
        assert connection.execute('PRAGMA user_version').fetchone()[0] == len(db.MIGRATIONS)
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
    items = {r['code']: r for r in scans.subscriptions(conn)}
    for code in ('600900', '600519'):
        assert items[code]['last_close'] == result['summary']['closes'][code]['last_close']
        assert items[code]['last_close_date'] == result['summary']['closes'][code]['last_close_date']
    assert conn.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 1


def test_closing_prices_merge_last_scan_id_with_dated_subscription(conn):
    bars = json.loads((DATA / '600900-61d.json').read_text())['stock']
    early, middle, late = [scans.normalize_close(row, '600900', AT) for row in bars[-3:]]
    other = scans.normalize_close(CapturedSources().valuation('603606'), '603606', AT)
    # Replay the older quote last to distinguish scan id from trade date.
    for closes in ({'600900': late, '603606': other}, {'600900': middle}):
        conn.execute("INSERT INTO scans(status,requested_at,summary) VALUES ('done',?,?)",
                     (AT, json.dumps({'closes': closes})))
    conn.execute('INSERT INTO subscriptions(code,added_at,last_close,last_close_date) VALUES (?,?,?,?)',
                 ('600900.SH', AT, early['last_close'], early['last_close_date']))
    expected = {'600900': middle, '603606': other}
    assert scans.ScanHistory(conn).closes == expected
    assert scans._closing_prices(conn, codes=['600900']) == {'600900': middle}
    # Same-date correction from a subscription takes precedence; supplied history skips scans.
    conn.execute('UPDATE subscriptions SET last_close=?,last_close_date=?', (early['last_close'], middle['last_close_date']))
    corrected = middle | {'last_close': early['last_close']}
    assert scans.ScanHistory(conn).closes == (expected | {'600900': corrected})
    assert scans._closing_prices(conn, {'600900': middle}) == {'600900': corrected}


def test_duplicate_scan_is_idempotent_and_request_freezes_public_events(conn):
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
    frozen = json.loads(request['request'])
    assert frozen == {'question': '', 'feedback': None, 'parent_report_id': None,
                      'events': [scans.event_context(e) for e in sorted(first['events'], key=lambda e: e['id'])]}
    # Only public announcement identity is frozen: no severity, action, A4b outcome or account data.
    assert all(set(e) == {'id', 'code', 'title', 'announced', 'url', 'kind'} for e in frozen['events'])
    assert {e['run_id'] for e in scans.events(conn)} == {run_id}
    card = service.company_card(conn, '603606')
    assert card['status'] == '待复核' and len(card['pending_events']) == 4
    assert card['current']['current_zone'] == valuation.at(priced(conn)['valuation_calc'], 36.03, '2026-10-26')['zone']
    rolled = valuation.at(priced(conn)['valuation_calc'], 36.03, '2026-10-26')
    calc = priced(conn)['valuation_calc']
    assert card['current']['valuation'] == rolled | {
        'boundary': stance.on_boundary(rolled),
        'flip_distance': stance.flip_distance({**calc, **rolled, 'market_cap': calc['market_cap'] * rolled['close'] / calc['close']}),
    }
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
    # The captured report quotes its own annual (D0) and half-year (D1) reports, so those two
    # announcements are included; the two it never read stay visible.
    card = service.company_card(conn, '603606')
    read = {body['document']['url'], body['sources']['D1']['document']['url']}
    assert card['status'] == '待复核'
    assert {e['url'] for e in card['pending_events']} == {e['url'] for e in first['events']} - read
    assert {e['review']['state'] for e in card['pending_events']} == {'not_read'}
    conn.execute('UPDATE reports SET body=? WHERE run_id=?',
                 (json.dumps(citing(body, first['events']), ensure_ascii=False), run))
    assert service.company_card(conn, '603606')['status'] == '有效'
    assert service.company_card(conn, '603606')['pending_events'] == []
    third = run_scan(conn, at='2026-11-01T18:00:00+08:00')
    assert not any(n['reason'] == 'review_failed' for n in third['notify'])
    assert third['summary']['queued_runs'] == []


@pytest.mark.parametrize('deviated_first', [False, True])
def test_severity_order_daily_budget_backlog_and_shanghai_date(conn, deviated_first):
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
        # Finished runs still count toward the day; an active run would defer the company instead.
        db.update_run(conn, run, created_at='2026-10-30T00:00:01+08:00', status='failed')
        conn.execute('INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action,run_id) '
                     "VALUES (?,?,?,?,?,?,?,?,?,'queued_review',?)", (scan_id, original['code'], original['doc_key'] + str(i),
                       original['title'], original['announced'], original['url'], original['kind'], 2, AT, run))
    expected = codes[-1]
    if deviated_first:
        # Explicit cross-company storage variant of the captured 603960 forecast
        # and report 156 threshold, only testing queue priority (not performance).
        expected = codes[1]
        report_id = service.company_card(conn, expected)['report_id']
        item = deepcopy(DISCLOSURES['reports'][2]['checkpoints'][0]['forecast'])
        item.update(period='2025-12-31', ref='checkpoints[0]', kind='forecast')
        observation = disclosure_checks.normalize_forecast(DISCLOSURES['tushare']['forecast']['603960.SH'], '603960')[0]
        check = disclosure_checks.judge(item, observation)
        assert check['outcome'] == 'deviated'
        conn.execute('INSERT INTO disclosure_checks(code,report_id,item_ref,kind,metric,period,source,source_identity,'
                     'announced,outcome,direction,checked_at,observed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (expected, report_id, *[check[k] for k in ('ref', 'kind', 'metric', 'period', 'source', 'identity',
                                                               'announced', 'outcome', 'direction')], AT,
                      json.dumps(check['observed'])))
        assert scans.review_order(scans.events(conn), scan_id, {expected})[0] == expected
    queued = scans._dispatch(conn, scan_id, AT, set(codes))
    assert len(queued) == 1
    assert conn.execute('SELECT code FROM runs WHERE id=?', (queued[0],)).fetchone()[0] == expected
    assert scans._dispatch(conn, scan_id, AT, set(codes)) == []
    # UTC 16:00 is the start of the next Shanghai day.
    assert len(scans._dispatch(conn, scan_id, '2026-10-30T16:00:00+00:00', set(codes))) == 2
    old = original | {'scan_id': scan_id - 1, 'detected_at': '2026-10-29T18:00:00+08:00'}
    fresh = original | {'code': codes[-1], 'severity': 3}
    assert scans.review_order([fresh, old], scan_id)[0] == old['code']


@pytest.mark.parametrize(('slots', 'case'), [
    (5, 'today'), (1, 'today'), (0, 'today'), (5, 'old-lookback'),
    (5, 'weekend'), (1, 'same-day-rescan'), (1, 'seen-before'), (5, 'two-risks'), (1, 'two-risks'),
])
def test_dispatch_reserves_a_slot_for_a_new_major_risk(conn, slots, case):
    ordinary = ['000651', '000963', '002050', '600519', '600900']
    risk_code = '603606'
    records = json.loads((DATA / 'hypotheses_v3.json').read_text())['reports']
    for code in ('000963', '002050', '600900'):
        report = next(r for r in reversed(records) if r['code'][:6] == code and r['passed'])
        run = service.submit(conn, code, f'backlog-card-{code}')
        db.save_report(conn, run, report['code'], report['name'], report['period'], True, report['verdict'],
                       json.loads(report['body']))
        db.update_run(conn, run, status='done')
    complete_cards_at(conn, [*ordinary, risk_code])
    scans.replace_subscriptions(conn, [*ordinary, risk_code], catalog=dict.fromkeys([*ordinary, risk_code]))
    # Seed an older deferred queue, as if the day's budget had been used. The
    # captured announcement is reused only as a queue row, not as company evidence.
    at = '2026-11-02T08:30:00+00:00'  # Monday in Shanghai
    prior_at = '2026-11-02T00:30:00+08:00' if case == 'same-day-rescan' else AT  # Friday
    source = CapturedSources([])
    prior_close = {risk_code: scans.normalize_close(source.valuation(risk_code), risk_code, prior_at)}
    prior = scans.submit(conn)
    summary = {'closes': prior_close if case in ('weekend', 'same-day-rescan', 'seen-before') else {}}
    conn.execute("UPDATE scans SET status='done',started_at=?,summary=? WHERE id=?", (prior_at, json.dumps(summary), prior))
    base = scans.normalize_announcements([ANNOUNCEMENTS[0]['raw']], '600900')[0]

    def seed(code, key, event, run=None, *, scan=prior, detected_at=prior_at):
        conn.execute('INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action,run_id) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?,?)', (scan, code, key, event['title'], event['announced'], event['url'],
                         event['kind'], event['severity'], detected_at, 'queued_review' if run else 'deferred', run))

    for code in ordinary:
        detected_at = prior_at if code == ordinary[0] else prior_at.replace(':00+08:00', ':01+08:00')
        seed(code, base['doc_key'], base, detected_at=detected_at)
    # These five-or-fewer finished runs count on Monday in Shanghai, Sunday UTC.
    for i in range(5 - slots):
        run = service.submit(conn, ordinary[0], f'used-slot-{i}')
        db.update_run(conn, run, status='failed', created_at='2026-11-01T16:00:01+00:00')
        seed(ordinary[0], f'used-slot-{i}', base, run)

    risks = [risk_code, ordinary[1]] if case == 'two-risks' else [risk_code]
    disclosed = {'weekend': '2026-11-01', 'old-lookback': '2026-10-29', 'seen-before': '2026-10-31'}
    published = datetime.fromisoformat(disclosed.get(case, '2026-11-02') + 'T00:15:00+08:00')
    for code in risks:
        item = deepcopy(next(i for i in ANNOUNCEMENTS if i['code'] == code))
        raw = item['raw']
        raw['announcementTitle'] += '（问询函）'  # Test-only severity and release date mutations.
        raw['announcementTime'] = int(published.timestamp() * 1000)
        source.items.append(item)
    if case == 'seen-before':
        [event] = captured_events(source, risk_code)
        failed = scans.submit(conn)
        conn.execute("UPDATE scans SET status='failed' WHERE id=?", (failed,))
        seed(risk_code, event['doc_key'], event, scan=failed, detected_at=published.isoformat())

    result = run_scan(conn, source, at)
    assert result['status'] == 'done', result['error']
    assert {e['code'] for e in result['events'] if (e['kind'], e['severity']) == ('major_risk', 3)} == (
        set() if case == 'seen-before' else set(risks))
    queued = [conn.execute('SELECT code FROM runs WHERE id=?', (run,)).fetchone()['code']
              for run in result['summary']['queued_runs']]
    expected = ordinary[:slots]
    if slots and case not in ('old-lookback', 'seen-before') and not set(expected).intersection(risks):
        expected[-1] = ordinary[1] if case == 'two-risks' else risk_code
    assert queued == expected
    assert {e['code'] for e in result['summary']['backlog']} == set([*ordinary, risk_code]) - set(expected)


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


def test_zone_notification_uses_price_change_at_previous_valuation_date():
    calc = STORED[2]['valuation_calc']  # Captured 600900 assumptions; dates/prices are boundary mutations.

    def card(close, day, report_id=1):
        value = valuation.at(calc, close, day)
        return {'report_id': report_id, 'status': '有效',
                'current': {'current_zone': value['zone'], 'valuation': value}}

    before = card(calc['close'], calc['as_of'])
    later = card(calc['close'], '2027-10-30')
    assert before['current']['current_zone'] == '测算透支区'
    assert later['current']['current_zone'] == '中性区'  # Display still rolls.
    assert scans.notification_reasons(before, later, [], []) == []
    cheaper = card(calc['close'] / 2, '2027-10-30')
    reason, = scans.notification_reasons(before, cheaper, [], [])
    assert (reason['from_zone'], reason['to_zone']) == ('测算透支区', '测算支持区')
    # Different reports must not be filtered by the prior report's boundary.
    reason, = scans.notification_reasons(before, later | {'report_id': 2}, [], [])
    assert reason['to_zone'] == '中性区'
    # A same-day price move remains a valid notice; no future observations needed.
    assert scans.notification_reasons(before, card(calc['close'] / 2, calc['as_of']), [], [])
    assert valuation.at(calc, calc['close'], '2029-05-01')['zone'] is None


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


def _assistant_mcp(conn, path, monkeypatch):
    from support.mcp import ORIGIN, RESOURCE
    from a_share_research.tools.server import create_app

    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)
    token = api_keys.create(conn, 'hermes-scans', role='assistant')
    return TestClient(create_app(path, account_shadow_enabled=True), base_url=ORIGIN), token


def test_mcp_auth_single_active_and_weekly_tool(tmp_path, monkeypatch):
    from support.mcp import rpc, tool_data, tool_error

    path = tmp_path / 'app.db'
    conn = db.connect(path)
    client, token = _assistant_mcp(conn, path, monkeypatch)
    with client:
        for name, args in (('research_subscriptions', {}), ('research_scan_weekly', {}), ('research_scan_read', {'scan_id': 1})):
            assert rpc(client, 'invalid', 'tools/call', {'name': name, 'arguments': args}).status_code == 401
        assert tool_error(client, token, 'research_subscriptions_replace', {'codes': ['bad']})['code'] == 'invalid_arguments'
        tool_data(client, token, 'research_subscriptions_replace', {'codes': []})
        first = tool_data(client, token, 'research_scan_request', {'request_id': 'first'})['data']
        second = tool_data(client, token, 'research_scan_request', {'request_id': 'second'})['data']
        assert first['scan_id'] == second['scan_id'] and first['status'] == 'queued'
        assert tool_data(client, token, 'research_scan_weekly', {'compact': False})['data']['subscriptions'] == []
        assert tool_data(client, token, 'research_scan_read', {'scan_id': first['scan_id']})['data']['status'] == 'queued'
        assert tool_error(client, token, 'research_scan_read', {'scan_id': 999})['code'] == 'not_found'
        conn.execute("UPDATE scans SET status='done', summary=? WHERE id=?",
                     (json.dumps({'notify': [{'code': '600519', 'path': '/stock/600519',
                                              'card_path': '/api/companies/600519/card'}]}), first['scan_id']))
        historical = tool_data(client, token, 'research_scan_read', {'scan_id': first['scan_id'], 'compact': False})['data']
        assert historical['notify'] == [{'code': '600519', 'path': '/stock/600519'}]
        assert historical['summary']['notify'] == [{'code': '600519', 'path': '/stock/600519'}]
        assert 'card_path' in conn.execute('SELECT summary FROM scans WHERE id=?', (first['scan_id'],)).fetchone()[0]
    conn.close()


@pytest.mark.wide_scan_scope
def test_owner_and_assistant_weekly_read_holdings_and_blind_scope_excludes_them(web, bound_book, tmp_path,  # noqa: F811
                                                                                monkeypatch):
    from support.mcp import tool_data

    blind = scans.weekly(web.conn, account_sources=False)
    with TestClient(web.app, base_url=web.settings.base_url) as client:
        assert client.post('/auth/dev', headers={'Origin': web.settings.base_url}, follow_redirects=False).status_code == 303
        owner = client.get('/scans/weekly').json()
    tools, token = _assistant_mcp(web.conn, tmp_path / 'app.db', monkeypatch)
    with tools:
        assistant = tool_data(tools, token, 'research_scan_weekly', {'compact': False})['data']
    assert all('holding' not in item['origins'] for item in blind['scan_scope'])
    for view in (owner, assistant):
        assert {item['code'] for item in view['scan_scope'] if 'holding' in item['origins']} == {'600001'}


def test_judgment_change_is_notified_once_and_weekly_counts_current_agenda(conn):
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    first = run_scan(conn)
    run_id = first['summary']['queued_runs'][0]
    body = deepcopy(BODY)
    body['checkpoints'] = deepcopy(CAPTURED_BODIES[39]['checkpoints'])
    body['valuation_calc'] = valuation.compute(assumptions(), body['table'], body['market_date'])
    body = citing(body, first['events'])
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
    announcements = overview.company_overview(conn, '600900')['announcements']
    risks = {first['notify'][0]['event_id'], second['notify'][0]['event_id']}
    assert len(risks) == 2
    assert risks <= {e['id'] for e in announcements if e['action'] == 'no_card'}


@pytest.mark.parametrize('case', ['saved', 'not-saved', 'correction'])
def test_major_risk_notice_replayed_after_failed_scan(conn, monkeypatch, case):
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    previous = run_scan(conn, CapturedSources([]))
    # A prior ordinary notice makes this scan consult the calendar when the card
    # next becomes pending. No major-risk notice has been recorded yet.
    summary = previous['summary']
    summary['notify'] = [{'code': '603606', 'reason': 'pending_review', 'notified_at': AT}]
    conn.execute('UPDATE scans SET summary=? WHERE id=?', (json.dumps(summary), previous['id']))

    class CalendarFailure(CapturedSources):
        def trade_days(self, start, end):
            super().trade_days(start, end)
            raise RuntimeError('交易日历暂不可用')

    source = CalendarFailure([next(i for i in ANNOUNCEMENTS if i['code'] == '603606')])
    raw = source.items[0]['raw']
    raw['announcementTitle'] += '（问询函）'  # Test-only risk mutation.

    def abort_before_notification(*args):
        raise RuntimeError('通知生成前失败')

    with monkeypatch.context() as fail:
        if case == 'not-saved':
            fail.setattr(scans, 'completed_reviews', abort_before_notification)
        first = run_scan(conn, source)
    assert first['status'] == 'failed'
    [event] = first['events']
    if case == 'not-saved':
        assert first['error'] == '通知生成前失败' and first['notify'] == []
    else:
        assert first['error'] == '交易日历暂不可用' and source.calendar_windows == [(AT[:10], AT[:10])]
        assert [(n['reason'], n['event_id']) for n in first['notify']] == [('major_risk', event['id'])]

    if case == 'correction':
        correction = deepcopy(source.items[0])
        correction['raw']['announcementId'] += '-correction'
        correction['raw']['announcementTitle'] += '（更正公告）'
        source.items.append(correction)
    second = run_scan(conn, CapturedSources(source.items))
    assert second['status'] == 'done', second['error']
    all_notices = [(n['reason'], n['event_id']) for scan in (first, second) for n in scan['notify']]
    expected = [('major_risk', event['id'])]
    if case == 'correction':
        [corrected] = second['events']
        assert corrected['id'] != event['id']
        expected.append(('major_risk', corrected['id']))
    else:
        assert second['events'] == []  # Replay stored event, not a new discovery.
    assert all_notices == expected


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
    connection.close()


def test_disclosure_scan_append_only_pending_retry_and_cooldown(conn, disclosure_replay):
    code, body, source = disclosure_replay
    first = run_scan(conn, source)
    assert first['status'] == 'done' and first['summary']['check_failures'] == []
    stored = scans.stored_checks(conn, service.company_card(conn, code)['report_id'])
    assert len(stored) == len(first['summary']['checks'][code]) == 2
    assert {c['outcome'] for c in stored} == {'deviated', 'missed'}
    assert all(c['scan_id'] == first['id'] and c['scan_event_id'] == first['events'][0]['id'] for c in stored)
    notices = [n for n in first['notify'] if n['kind'] == 'disclosure_check']
    assert {n['reason'] for n in notices} == {'checkpoint_deviated', 'add_trigger_missed'}
    assert all('bypass_cooldown' not in n and '2026-01-23' in n['detail'] for n in notices)
    for notice in notices:
        history = [notice | {'notified_at': '2026-09-30T18:00:00+08:00'}]
        assert not scans.allow_notification(notice, history, '2026-10-09T18:00:00+08:00', DAYS)
        assert scans.allow_notification(notice, history, '2026-10-16T18:00:00+08:00', DAYS)
    second = run_scan(conn, source, '2026-10-31T18:00:00+08:00')
    assert second['summary']['checks'][code] == [] and second['notify'] == []
    assert len(scans.stored_checks(conn, stored[0]['report_id'])) == 2
    # A later version is new evidence but remains inside the same cooldown.
    class Revision(CapturedSources):
        def disclosures(self, code, periods):
            raw = super().disclosures(code, periods)
            raw['forecast'][0]['ann_date'] = '20260124'  # Announcement-date-only revision variant.
            return raw
    revision = Revision(source.items)
    third = run_scan(conn, revision, '2026-11-01T18:00:00+08:00')
    assert len(third['summary']['checks'][code]) == 2
    assert not [n for n in third['notify'] if n['kind'] == 'disclosure_check']
    # Explicit calendar-continuation variant (capture ends 10-23), only for retry timing.
    revision.trade_days = lambda start, end: [d for d in DAYS + [
        '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06'] if start <= d <= end]
    ready = run_scan(conn, revision, '2026-11-06T18:00:00+08:00')
    assert ready['summary']['checks'][code] == []
    assert len([n for n in ready['notify'] if n['kind'] == 'disclosure_check']) == 2
    repeated = run_scan(conn, revision, '2026-11-09T18:00:00+08:00')
    assert not [n for n in repeated['notify'] if n['kind'] == 'disclosure_check']


@pytest.mark.parametrize('has_annual_statement', [True, False])
def test_scan_express_revenue_uses_each_observations_period(conn, disclosure_replay, has_annual_statement):
    code, body, source = disclosure_replay
    # Route captured 605016 figures through the replay card. Change only routing, period and basis.
    capture = DISCLOSURES['tushare']
    express = capture['express']['605016.SH'][0] | {'ts_code': code + '.SH'}
    annual = capture['income']['605016.SH'][0] | {'ts_code': code + '.SH'}
    half = annual | {'end_date': '20260630'}
    if has_annual_statement:
        half['revenue'] -= 1
    checkpoint = body['checkpoints'][0]
    body['checkpoints'] = [checkpoint | {'forecast': checkpoint['forecast'] | {'metric': 'T.revenue', 'period': p}}
                           for p in ('2025-12-31', '2026-06-30')]
    report_id = service.company_card(conn, code)['report_id']
    conn.execute('UPDATE reports SET body=? WHERE id=?', (json.dumps(body), report_id))
    source.disclosures = lambda *_: {
        'forecast': [], 'periodic': [], 'express': [express, express | {'end_date': '20260630'}],
        'income': [annual, half] if has_annual_statement else [half]}
    run_scan(conn, source)
    result = {c['period']: c for c in scans.stored_checks(conn, report_id) if c['metric'] == 'T.revenue'}
    for period, confirmed in [('2025-12-31', has_annual_statement), ('2026-06-30', not has_annual_statement)]:
        assert (result[period]['reason'] is None) == confirmed
        if not confirmed:
            assert result[period]['outcome'] == 'undetermined'


def test_disclosure_failure_does_not_change_announcement_window(conn, disclosure_replay):
    code, _, source = disclosure_replay
    # Use 603606's captured close only as an explicit identity variant so this test
    # isolates disclosure failure from the existing valuation-failure window rule.
    class Failure(CapturedSources):
        def valuation(self, requested):
            return super().valuation('603606') | {'ts_code': requested}

        def disclosures(self, code, periods):
            raise RuntimeError('披露取数暂不可用')
    failing = Failure(source.items)
    first = run_scan(conn, failing)
    assert first['status'] == 'done' and first['summary']['failures'] == []
    assert first['summary']['check_failures'] == [{'code': code, 'report_id': service.company_card(conn, code)['report_id'],
                                                  'stage': 'disclosure_checks', 'reason': '披露取数暂不可用'}]
    assert scans.ScanHistory(conn, fields=('failures', 'scope', 'announcements_succeeded')).announcements.get(code) is None
    card = service.company_card(conn, code)
    second = run_scan(conn, failing, '2026-10-31T18:00:00+08:00')
    assert second['status'] == 'done' and failing.windows == [(code, 30), (code, 3)]
    assert service.company_card(conn, code)['status'] == card['status']
    assert second['summary']['checks'] == {}


def test_disclosure_effective_only_and_old_card_date(conn, disclosure_replay):
    code, body, source = disclosure_replay
    first = run_scan(conn, source)
    card = service.company_card(conn, code)
    stored = scans.stored_checks(conn, card['report_id'])
    items = {i['ref']: i for i in disclosure_checks.items(body)}
    stored = [c | {'detail': scans.check_detail(c, items[c['ref']], body)} for c in stored]
    # Later overlapping/inside revisions suppress stale newly inserted deviations.
    later = [c | {'id': c['id'] + 100, 'announced': '2026-01-24', 'outcome': 'inside'} for c in stored]
    assert scans.notification_reasons(card, card, [], [], checks=stored + later, new_checks=stored) == []
    assert first['summary']['checks'][code]
    # Card-day-only variant excludes all disclosures before the new completion day.
    complete_cards_at(conn, [code], at='2026-02-01T18:00:00+08:00')
    assert scans._check_disclosures(conn, source, code, service.company_card(conn, code), first['id'], AT) is None


def test_disclosures_network_boundary_and_periodic_versions(monkeypatch):
    import pandas as pd
    from a_share_research import market, material, sources
    from support.financials import CAPTURES, frames

    captured = deepcopy(CAPTURES['000858'])
    fin = frames(captured)
    periods = ['2025-12-31', '2025-09-30']
    calls = []
    sentinel = object()

    def query(client, api, **params):
        assert client is sentinel
        calls.append((api, params))
        # No 000858 forecast/express is captured. Empty results model provider lag;
        # financial statements are replayed from the existing real captures.
        return fin['income'].copy() if api == 'income' else pd.DataFrame()

    monkeypatch.setattr(market, 'client', lambda: sentinel)
    monkeypatch.setattr(market, 'query', query)
    monkeypatch.setattr(sources, 'financials', lambda code: frames(captured) if code == '000858' else None)
    raw = scans.MarketSources().disclosures('000858', periods)
    assert calls == [(api, {'ts_code': '000858.SZ', 'period': period})
                     for api in ('forecast', 'express') for period in ('20251231', '20250930')] + [
                         ('income', {'ts_code': '000858.SZ', 'report_type': '1'})]
    observations = disclosure_checks.normalize_periodic(raw['periodic'], '000858')
    assert observations
    table = material.financial_table(fin, {})
    for observation in observations:
        assert observation['period'] in periods
        for metric, bounds in observation['values'].items():
            row = next(r for r in table if r['id'] == metric)
            if reason := row.get('unavailable', {}).get(observation['period']):
                assert bounds == {'low': None, 'high': None, 'reason': reason}
            elif observation['period'] not in row['exact_values']:
                # The capture predates a column the table now reads (e.g. parent equity).
                assert bounds == {'low': None, 'high': None, 'reason': '已取得该期报表，但目标字段缺失'}
            else:
                assert str(bounds['low']) == row['exact_values'][observation['period']]
    with pytest.raises(companies.SourceError, match='其他公司'):
        disclosure_checks.normalize_periodic(raw['periodic'], '603960')
    # Explicit announcement-date correction variant must produce a new version
    # without changing the captured exact table values.
    revised = deepcopy(raw['periodic'])
    revised[0]['row']['f_ann_date'] = '2026-11-16'
    original = disclosure_checks.normalize_periodic(raw['periodic'][:1], '000858')[0]
    changed = disclosure_checks.normalize_periodic(revised[:1], '000858')[0]
    assert changed['identity'] != original['identity'] and changed['announced'] == '2026-11-16'
    assert changed['values'] == original['values']


@pytest.mark.wide_scan_scope
def test_wide_scope_adds_tracking_l5_and_card_companies_but_cards_api_keeps_policy_origins(conn):
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    # Batch rows are minimal variants; codes are captured fixture companies.
    for status, frozen, codes in (('tracking', '2026-10-23T20:00:00+08:00', ['000963.SZ']),
                                  ('closed', '2026-09-01T20:00:00+08:00', ['600900.SH']),
                                  ('tracking', AT, ['603606.SH'])):
        conn.execute("INSERT INTO batches(status,created_at,rule,l5,frozen_at) VALUES (?,?,'{}',?,?)",
                     (status, frozen, json.dumps({'codes': codes}), frozen))
    assert {s['code']: s['origin'] for s in scans.scan_scope(conn)} == {
        '603606': 'external', '000963': 'l5', '000651': 'card', '600519': 'card'}
    assert {s['code']: s['origin'] for s in scans.subscriptions(conn)} == {'603606': 'external'}
    cards = service.company_cards(conn)
    assert [(c['code'], c['origin']) for c in cards['cards']] == [('603606', 'external')] and not cards['missing']
    result = run_scan(conn, CapturedSources([]))
    assert {s['code'] for s in result['summary']['scope']} == {'603606', '000963', '000651', '600519'}
    # 000963/000651 have no captured close: the gap is a recorded failure, not silence.
    assert {(f['code'], f['stage']) for f in result['summary']['failures']} == {('000963', 'valuation'), ('000651', 'valuation')}
    assert {s['origin'] for s in scans.weekly(conn)['scan_scope']} == {'external', 'l5', 'card'}


@pytest.mark.wide_scan_scope
def test_bound_holding_and_public_scan_sources_merge_without_changing_subscriptions(bound_book):  # noqa: F811
    path, conn, principal, _, _ = bound_book
    ctx = Context(conn, path, principal, account_shadow_enabled=True)
    code = '600001'  # The existing bound-ledger fixture.
    assert [(s['code'], s['origins']) for s in scans.scan_scope(conn)] == [(code, ['holding'])]
    assert scans.weekly(conn, account_sources=False)['scan_scope'] == []
    conn.execute('INSERT INTO subscriptions(code,added_at) VALUES (?,?)', (code, AT))
    conn.execute("INSERT INTO batches(status,created_at,rule,l5,frozen_at) VALUES ('frozen',?,'{}',?,?)",
                 (AT, json.dumps({'codes': [code + '.SH']}), AT))
    scan = run_scan(conn, CapturedSources([]))
    read = invoke(ctx, 'research_scan_read', {'scan_id': scan['id'], 'compact': False})['data']
    [item] = read['summary']['scope']
    assert (item['code'], item['origin'], item['origins']) == (code, 'external', ['external', 'l5', 'holding'])
    weekly = invoke(ctx, 'research_scan_weekly', {'compact': False})['data']
    assert weekly['scan_scope'] == [dict(code=code, origin='external', origins=item['origins'])]
    assert scans.subscriptions(conn)[0]['origins'] == ['external', 'l5']


@pytest.mark.wide_scan_scope
def test_unverifiable_holding_scope_keeps_scanning_public_subscriptions(bound_book):  # noqa: F811
    _, conn, _, directory, sha = bound_book
    # Remove one required evidence file from the temporary ledger fixture.
    (directory / sha).unlink()
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    # Check the current scope before a scan can contribute any historical failures.
    weekly = scans.weekly(conn, at=AT)
    assert weekly['scan_scope'] == [{'code': '603606', 'origin': 'external', 'origins': ['external']}]
    assert [item['stage'] for item in weekly['failures']] == ['holding_scope']
    assert scans.weekly(conn, at=AT, compact=True)['failures'] == weekly['failures']
    assert scans.weekly(conn, at=AT, account_sources=False)['failures'] == []
    assert service.company_cards(conn, holdings=True)['holdings_available'] is False

    result = run_scan(conn)
    assert result['status'] == 'done'
    assert [s['code'] for s in result['summary']['scope']] == ['603606']
    assert result['summary']['announcements_succeeded'] == ['603606']
    assert any(item['stage'] == 'holding_scope' for item in result['summary']['failures'])


@pytest.mark.wide_scan_scope
@pytest.mark.parametrize('scanned_before', [False, True])
def test_code_entering_scan_history_reviews_old_filing_without_pending_notice(conn, monkeypatch, scanned_before):
    scans.replace_subscriptions(conn, ['603606' if scanned_before else '600519'], catalog=CATALOG)
    with monkeypatch.context() as narrow:
        narrow.setattr(scans, 'scan_scope', scans.subscriptions)
        run_scan(conn, CapturedSources([]))
    source = CapturedSources([i for i in ANNOUNCEMENTS if i['code'] == '603606' and i.get('period') == '2026-09-30'])
    result = run_scan(conn, source, '2026-10-31T18:00:00+08:00')
    origin = {s['code']: s['origin'] for s in result['summary']['scope']}['603606']
    assert origin == ('external' if scanned_before else 'card')
    [event] = [e for e in result['events'] if e['code'] == '603606']
    assert event['action'] == 'queued_review' and event['run_id'] in result['summary']['queued_runs']
    assert service.company_card(conn, '603606')['status'] == '待复核'
    pending = [n for n in result['notify'] if n['code'] == '603606' and n['reason'] == 'pending_review']
    assert bool(pending) == scanned_before


def test_no_card_disclosure_closes_only_after_a_formal_report_cites_it(conn):
    code = '603606'
    # Replay the captured Q3 filing while the company's existing reports are unavailable.
    conn.execute("UPDATE runs SET status='failed' WHERE company_key=?", (code,))
    scans.replace_subscriptions(conn, [code], catalog=CATALOG)
    source = CapturedSources([i for i in ANNOUNCEMENTS if i['code'] == code and i.get('period') == '2026-09-30'])
    event, = run_scan(conn, source)['events']
    assert event['action'] == 'no_card'

    run = service.submit(conn, code, 'after-no-card')
    db.save_report(conn, run, RECORD['code'], RECORD['name'], RECORD['period'], True, RECORD['verdict'], BODY)
    db.update_run(conn, run, status='done', finished_at='2026-11-01T10:00:00+08:00')
    before, = overview.company_overview(conn, code)['announcements']
    assert before['unresolved']
    cite_in_card(conn, code, [event])
    after, = overview.company_overview(conn, code)['announcements']
    assert not after['unresolved']
    assert after['action'] == 'no_card' and after['review']['state'] == 'included'
    assert after['path'] == f"/reports/{after['review']['report_id']}"


@pytest.mark.wide_scan_scope
def test_compact_weekly_skips_card_company_scope(conn):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        scans.weekly(conn, at=AT, compact=True)
    finally:
        conn.set_trace_callback(None)
    assert not any('SELECT DISTINCT r.company_key FROM reports' in sql for sql in statements)
