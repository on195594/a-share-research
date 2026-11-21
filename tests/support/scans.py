"""Captured reports, disclosures and announcement scan replays."""

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

from a_share_research import db, scans, service, valuation
from support.valuation import BODY, RECORD, assumptions

DATA = Path(__file__).resolve().parents[1] / 'data'
ANNOUNCEMENTS = sum((json.loads((DATA / f).read_text())['items'] for f in
                     ('announcements.json', 'periodic-announcements.json')), [])
CATALOG = {item['code']: item['raw'] for item in ANNOUNCEMENTS}
DAYS = [date.fromisoformat(d).isoformat() for d in json.loads((DATA / 'backtest-dates.json').read_text())['days']]
AT = '2026-10-30T18:00:00+08:00'
DISCLOSURES = json.loads((DATA / 'disclosure-checks.json').read_text())


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

    def disclosures(self, code, periods):
        rows = DISCLOSURES['tushare']
        return {**{kind: deepcopy(next((v for k, v in rows[kind].items() if k[:6] == code), []))
                   for kind in ('forecast', 'express', 'income')}, 'periodic': []}

    def trade_days(self, start, end):
        self.calendar_windows.append((start, end))
        return [d for d in DAYS if start <= d <= end]

    def report_rc_window(self, as_of):
        # No sell-side capture in these announcement replays; leave its coverage unavailable.
        return {'trading_days': 0, 'covered_days': 0, 'missing_days': [], 'truncated_days': [],
                'reason': '公告扫描重放未采集卖方预测'}


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


def citing(body, events):
    """Explicit read/cite variant of a captured report body.

    Only the report-to-announcement relationship is mutated (the report is recorded as having
    read each announcement and quoting its title); no financial fact is added.
    """
    body = deepcopy(body)
    for i, event in enumerate(events):
        key = f'DV{i}'
        body['sources'][key] = {'document': {'code': event['code'], 'title': event['title'], 'period': None,
                                             'announced': event['announced'], 'url': event['url']}, 'pages': [1]}
        body['risks'][0]['cites'].append({'source': key, 'page': 1, 'quote': event['title']})
    return body


def captured_events(source, code):
    return scans.normalize_announcements([i['raw'] for i in source.items if i['code'] == code], code)


def cite_in_card(conn, code, events):
    report_id = service.company_card(conn, code)['report_id']
    body = json.loads(conn.execute('SELECT body FROM reports WHERE id=?', (report_id,)).fetchone()[0])
    conn.execute('UPDATE reports SET body=? WHERE id=?', (json.dumps(citing(body, events), ensure_ascii=False), report_id))


def complete_cards_at(conn, codes=('603606',), at='2026-01-01T18:00:00+08:00'):
    # Explicitly shift captured card timestamps before disclosures for new-event replays.
    # Equal report dates retain the latest captured card by report id.
    for code in codes:
        conn.execute('UPDATE runs SET finished_at=? WHERE id IN '
                     '(SELECT run_id FROM reports WHERE substr(code,1,6)=?)', (at, code))
        conn.execute('UPDATE reports SET created_at=? WHERE substr(code,1,6)=?', (at, code))


def replay_disclosure(conn):
    # Cross-company routing/period variant, never investment evidence: reuse captured
    # report 156's frozen thresholds with 603960's untouched 2025 forecast. The
    # report shell comes from BODY; its price fields are irrelevant to this check.
    report = deepcopy(next(r for r in DISCLOSURES['reports'] if r['report_id'] == 156))
    body = deepcopy(BODY)
    body['checkpoints'] = report['checkpoints'][:1]
    body['checkpoints'][0]['forecast']['period'] = '2025-12-31'
    body['stance'] = {'action': report['stance']}
    body['stance_calc'] = report['stance_calc']
    # Metric/period variant makes the captured add threshold testable by forecasts.
    body['stance_calc']['add_trigger'].update(metric='T.net_profit', period='2025-12-31')
    code = '603960'
    run = service.submit(conn, code, 'disclosure-replay')
    db.save_report(conn, run, code, code, '2025-12-31', True, RECORD['verdict'], body)
    db.update_run(conn, run, status='done', finished_at='2026-01-01T18:00:00+08:00')
    conn.execute('INSERT INTO subscriptions(code,added_at) VALUES (?,?)', (code, AT))
    # Announcement identity/company variant of the captured directory, used only
    # to put a forecast event in the pending queue. Disclosure facts stay untouched.
    original = next(i for i in ANNOUNCEMENTS if scans.classify(i['raw']['announcementTitle']) == ('forecast', 2))
    announcement = deepcopy(original)
    announcement['code'] = code
    announcement['raw']['secCode'] = code
    source = CapturedSources([announcement])
    return code, body, source
