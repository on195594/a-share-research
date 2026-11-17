"""Event discovery → continuation research → handling state, on captured announcements and reports.

Mutations below are explicitly marked variants of real captures (citation placement, open
questions, document periods, run status); they never add financial facts.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from a_share_research import db, prompts, research, scans, service, sources
from a_share_research.material import Material
from support.valuation import BODY, RECORD
from test_scans import AT, CATALOG, CapturedSources, captured_events, citing, complete_cards_at, conn, run_scan  # noqa: F401

DATA = Path(__file__).parent / 'data'
GOVERNANCE = json.loads((DATA / 'governance-announcements.json').read_text())['items']
REVIEWS = {c['event']['id']: c for c in json.loads((DATA / 'scan-event-reviews.json').read_text())['cases']}


@pytest.mark.parametrize('item', GOVERNANCE, ids=lambda i: i['code'])
def test_governance_titles_only_trigger_investigation(item):
    found = scans.classify(item['raw']['announcementTitle'])
    assert (found and found[0]) == item['expected']
    if found:
        assert found == ('major_risk', 3)
    # The scanner stores a trigger only; the event context carries no violation finding.
    events = scans.normalize_announcements([item['raw']], item['code'])
    assert bool(events) == bool(item['expected'])


def test_earnings_call_notice_is_not_a_periodic_report():
    # Production scan_events id 11 queued an empty-question run for this title.
    assert scans.classify(REVIEWS[11]['event']['title']) is None
    assert scans.classify('上海艾力斯医药科技股份有限公司2026年第三季度报告') == ('periodic', 2)


@pytest.mark.parametrize('event_id', sorted(REVIEWS))
def test_production_reports_settle_events_by_reading_and_citing(event_id):
    case = REVIEWS[event_id]
    assert scans.event_evidence(case['body'], case['event'])['state'] == case['expected']


def test_forecast_is_superseded_only_by_a_cited_later_periodic_report():
    case = REVIEWS[6]
    found = scans.event_evidence(case['body'], case['event'])
    # Report 165 never read the 2026-10-15 forecast but quoted the 2026-10-17 Q3 report.
    assert found['state'] == 'superseded' and found['sources'] == ['D1']
    assert found['label'].endswith('（不等于风险排除）')
    earlier = deepcopy(case['body'])
    earlier['sources']['D1']['document']['period'] = '2026-06-30'  # Period-mismatch variant.
    assert scans.event_evidence(earlier, case['event'])['state'] == 'not_read'


def test_open_question_unable_and_uncited_states_stay_open():
    case = REVIEWS[11]
    event, body = case['event'], case['body']
    assert scans.event_evidence(body, event)['items'] == ['answer[0]']
    questioned = deepcopy(body)
    questioned['open_questions'] = [{'id': 'q1', 'where': 'answer[0]', 'problem': '变体：复核未结'}]
    assert scans.event_evidence(questioned, event)['state'] == 'open'
    questioned['open_questions'][0]['where'] = 'answer'
    assert scans.event_evidence(questioned, event)['state'] == 'open'
    unable = deepcopy(body)
    unable['answer'][0]['text'] += '（变体：无法判断）'
    assert scans.event_evidence(unable, event)['state'] == 'unable'
    uncited = deepcopy(body)
    uncited['answer'][0]['cites'] = [c for c in uncited['answer'][0]['cites'] if c.get('source') != 'D7']
    assert scans.event_evidence(uncited, event)['state'] == 'read_uncited'
    # A table cite (`ref`) is not a reading of the announcement.
    uncited['answer'][0]['cites'].append({'ref': 'T.revenue', 'source': 'D7'})
    assert scans.event_evidence(uncited, event)['state'] == 'read_uncited'


def test_event_found_during_active_research_waits_then_is_frozen_once(conn):  # noqa: F811
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    active = service.submit(conn, '603606', 'manual-in-flight')
    first = run_scan(conn)
    assert first['summary']['queued_runs'] == []
    assert {e['action'] for e in first['events']} == {'deferred'}
    card = service.company_card(conn, '603606')
    assert {e['review']['state'] for e in card['pending_events']} == {'awaiting_research'}
    db.update_run(conn, active, status='failed', error='变体：人工研究失败', finished_at='2026-10-30T19:00:00+08:00')
    second = run_scan(conn, at='2026-10-31T18:00:00+08:00')
    run_id, = second['summary']['queued_runs']
    frozen = json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (run_id,)).fetchone()[0])['events']
    # Events from both scans merge into one request; the frozen contexts are public identities only.
    assert [e['id'] for e in frozen] == sorted(e['id'] for e in first['events'])
    assert {e['run_id'] for e in scans.events(conn)} == {run_id}
    third = run_scan(conn, at='2026-11-01T18:00:00+08:00')
    assert third['summary']['queued_runs'] == []


def test_retry_keeps_frozen_events_and_moves_their_run(conn):  # noqa: F811
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    first = run_scan(conn)
    run_id, = first['summary']['queued_runs']
    request = json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (run_id,)).fetchone()[0])
    db.update_run(conn, run_id, status='failed', error='变体：研究失败', finished_at='2026-10-30T19:00:00+08:00')
    assert {e['review']['state'] for e in scans.open_events(conn, '603606')} == {'research_failed'}
    retried = service.retry_run(conn, run_id)
    assert service.retry_run(conn, run_id) == retried
    assert json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (retried,)).fetchone()[0]) == request
    assert {e['run_id'] for e in scans.events(conn)} == {retried}
    assert {e['review']['state'] for e in scans.open_events(conn, '603606')} == {'researching'}
    # The retried task, not a fresh scan dispatch, owns the events: no second paid run.
    assert run_scan(conn, at='2026-10-31T18:00:00+08:00')['summary']['queued_runs'] == []


def test_old_payloads_keep_their_idempotency_key(conn):  # noqa: F811
    old = service.submit(conn, '603606', 'scan-legacy')
    assert service.submit(conn, '603606', 'scan-legacy', events=[]) == old
    assert json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (old,)).fetchone()[0]) == {
        'question': '', 'feedback': None, 'parent_report_id': None}


def test_weekly_keeps_open_events_failures_and_coverage_gap_after_recovery(conn):  # noqa: F811
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    first = run_scan(conn, at=AT)
    # Timestamp-shift variant: the captured close only validates from 10-26, so the successful
    # scan is moved back to model a 40-day outage after it.
    conn.execute("UPDATE scans SET started_at='2026-09-20T18:00:00+08:00' WHERE id=?", (first['id'],))
    late = run_scan(conn, at=AT)
    gap, = late['summary']['coverage_gaps']
    assert gap == {'code': '603606', 'last_success': '2026-09-20T18:00:00+08:00',
                   'uncovered_from': '2026-09-20', 'uncovered_to': '2026-09-30'}
    assert late['summary']['closes']['603606']
    run_scan(conn, at='2026-10-31T18:00:00+08:00')
    report = scans.weekly(conn, at='2026-10-31T18:00:00+08:00')
    assert report['last_scan']['started_at'] == '2026-10-31T18:00:00+08:00'
    assert [c['gap'] for c in report['coverage']] == [None]
    assert report['coverage_gaps'] == [gap]
    assert report['open_events'] and all(e['review']['label'] for e in report['open_events'])


def _material():
    doc = sources.Document(**{k: BODY['document'][k] for k in ('code', 'name', 'title', 'period', 'announced', 'url')})
    return Material(code=RECORD['code'], name=RECORD['name'], doc=doc, pages={}, raw_pages={},
                    table=deepcopy(BODY['table']), market_date=BODY['market_date'], extra={'catalog': {'D0': doc.info()}})


def test_continuation_lists_events_in_catalog_with_financial_checks(conn):  # noqa: F811
    material = _material()
    half = BODY['sources']['D1']['document']
    events = [{'id': 1, 'code': '603606', 'title': half['title'], 'announced': half['announced'], 'url': half['url'],
               'kind': 'periodic'},
              {'id': 2, 'code': '603606', 'title': '东方电缆2026年第三季度报告', 'announced': '2026-10-29',
               'url': 'https://static.cninfo.com.cn/finalpage/2026-10-29/1225633509.PDF', 'kind': 'periodic'},
              {'id': 3, 'code': '603606', 'title': GOVERNANCE[1]['raw']['announcementTitle'], 'announced': '2026-11-07',
               'url': 'https://static.cninfo.com.cn/' + GOVERNANCE[1]['raw']['adjunctUrl'], 'kind': 'major_risk'}]
    listed = research.scan_events(conn, material, events)
    catalog = material.extra['catalog']
    assert [catalog[e['source']]['url'] for e in listed] == [e['url'] for e in events]
    assert catalog[listed[1]['source']]['period'] == '2026-09-30' and catalog[listed[2]['source']]['period'] == ''
    half_checks = listed[0]['financial_checks']
    assert (half_checks['period'], half_checks['compare']) == ('2026-06-30', '2025-06-30')
    assert {m['metric'] for m in half_checks['metrics']} == set(research.FINANCIAL_CHECKS)
    assert all(set(m['periods'].values()) == {'available'} for m in half_checks['metrics'])
    q3 = listed[1]['financial_checks']['metrics']
    assert all(m['periods']['2026-09-30'].startswith('财务表无该期数值') for m in q3)
    assert 'financial_checks' not in listed[2]
    material.extra['events'] = listed
    assert prompts.EVENTS in prompts.output_schema(material) and '相减' in prompts.EVENTS


def test_unrelated_newer_report_leaves_event_open_and_cited_one_settles(conn):  # noqa: F811
    complete_cards_at(conn)
    scans.replace_subscriptions(conn, ['603606'], catalog=CATALOG)
    source = CapturedSources([i for i in CapturedSources().items if i['code'] == '603606' and i.get('period') == '2026-09-30'])
    run_id, = run_scan(conn, source)['summary']['queued_runs']
    db.save_report(conn, run_id, RECORD['code'], RECORD['name'], RECORD['period'], True, RECORD['verdict'], deepcopy(BODY))
    db.update_run(conn, run_id, status='done', finished_at='2026-10-31T10:00:00+08:00')
    event, = scans.open_events(conn, '603606')
    assert event['review']['state'] == 'not_read'
    manual = service.submit(conn, '603606', 'manual-cites')
    db.save_report(conn, manual, RECORD['code'], RECORD['name'], RECORD['period'], True, RECORD['verdict'],
                   citing(BODY, captured_events(source, '603606')))
    db.update_run(conn, manual, status='done', finished_at='2026-11-01T10:00:00+08:00')
    assert scans.open_events(conn, '603606') == []


def test_scan_request_reaches_research_and_records_per_event_outcome(tmp_path, monkeypatch):
    from support.reports import MATERIAL, report
    connection = db.connect(tmp_path / 'event-run.db')
    raw = next(i['raw'] for i in CapturedSources().items if i['code'] == '600519')
    # Real announcement identity; the classifier skips this title, so the kind is a marked variant.
    event = {'id': 1, 'code': '600519', 'title': raw['announcementTitle'], 'kind': 'major_risk',
             'announced': sources.announcement_document(raw, '600519').announced,
             'url': 'https://static.cninfo.com.cn/' + raw['adjunctUrl']}
    run = service.submit(connection, '600519', 'scan-1-600519', events=[scans.event_context(event)])
    material = deepcopy(MATERIAL)
    material.extra['catalog'] = {'D0': material.doc.info()}  # gather() always lists the start report.
    snapshot = material.snapshot()
    seen = {}

    def explore(runner, material, acquisition):
        seen['question'], seen['events'] = material.extra['question'], material.extra['events']
        seen['prompt'] = material.prompt_block()
        return report()
    monkeypatch.setattr(research, 'gather', lambda code: research.Material.from_snapshot(deepcopy(snapshot)))
    monkeypatch.setattr(research, 'explore', explore)
    monkeypatch.setattr(research, 'revise', lambda runner, material, rep, ledger, acquisition: (rep, [], True))
    research.execute(connection, run)
    body = json.loads(connection.execute('SELECT body FROM reports WHERE run_id=?', (run,)).fetchone()[0])
    assert seen['question'] == research.EVENT_QUESTION
    listed, = seen['events']
    assert listed['url'] == event['url'] and listed['source'] != 'D0'
    assert event['url'] in seen['prompt'] and 'holding' not in seen['prompt']
    recorded, = body['events']
    # The draft never read the announcement: published, but the event stays open.
    assert recorded['evidence']['state'] == 'not_read'
    connection.close()
