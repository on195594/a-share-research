"""A4b failure replays: explicit availability/content/date variants of real captures.

No live data, paid research, or production notifications are used here.
"""
from copy import deepcopy

from a_share_research import disclosure_checks, material, scans, service
from support.financials import CAPTURES, frames
from a_share_research.tools import advisor_text
from support.scans import CapturedSources, run_scan


def source_with(source, mutate):
    class Variant(CapturedSources):
        def disclosures(self, code, periods):
            raw = source.disclosures(code, periods)
            mutate(raw)
            return raw
    return Variant(source.items)


def effective(conn, code):
    card = service.company_card(conn, code)
    return disclosure_checks.latest(scans.stored_checks(conn, card['report_id']))


def test_same_identity_missing_then_complete_retains_history(disclosure_replay, conn):
    code, _, source = disclosure_replay
    def missing(raw):
        raw['forecast'][0].update(net_profit_min=None, net_profit_max=None)
    first = run_scan(conn, source_with(source, missing))
    assert first['status'] == 'done'
    assert all(c['outcome'] == 'undetermined' for c in effective(conn, code))
    second = run_scan(conn, source, '2026-11-01T18:00:00+08:00')
    current = effective(conn, code)
    assert {c['kind']: c['outcome'] for c in current} == {'forecast': 'deviated', 'add_trigger': 'missed'}
    assert len(scans.stored_checks(conn, current[0]['report_id'])) == 4
    third = run_scan(conn, source, '2026-11-02T18:00:00+08:00')
    assert third['summary']['checks'][code] == []  # exact duplicate, no extra history/notices
    assert not [n for n in third['notify'] if n['kind'] == 'disclosure_check']
    assert any(n['kind'] == 'disclosure_check' for n in second['notify'])


def test_same_identity_numeric_conflict_does_not_pick_arrival_or_hash(disclosure_replay, conn):
    code, _, source = disclosure_replay
    run_scan(conn, source)
    def conflict(raw):
        raw['forecast'][0]['net_profit_min'] *= 2
        raw['forecast'][0]['net_profit_max'] *= 2
    changed = source_with(source, conflict)
    second = run_scan(conn, changed, '2026-11-01T18:00:00+08:00')
    current = effective(conn, code)
    assert all(c['outcome'] == 'undetermined' and '冲突' in c['reason'] for c in current)
    assert all(c['observed'] == {'low': None, 'high': None} for c in current)
    assert all(c['evidence']['value']['low'] is not None for c in current)
    assert all(c['observed'] == {'low': None, 'high': None} for c in service.company_card(conn, code)['checks'])
    assert second['status'] == 'done'
    # A repeat or reappearance of the original cannot silently resolve a numeric conflict.
    run_scan(conn, source, '2026-11-02T18:00:00+08:00')
    assert all(c['outcome'] == 'undetermined' and c['observed'] == {'low': None, 'high': None}
               and c['evidence']['value']['low'] is not None for c in effective(conn, code))
    def correction(raw):
        raw['forecast'][0]['ann_date'] = '20260124'
    run_scan(conn, source_with(source, correction), '2026-11-03T18:00:00+08:00')
    assert {c['outcome'] for c in effective(conn, code)} == {'deviated', 'missed'}


def test_new_periodic_acquired_but_empty_is_not_unsupported(disclosure_replay, conn):
    code, body, source = disclosure_replay
    fin = frames(deepcopy(CAPTURES['000858']))
    captured = material._latest_rows(fin['income'], consolidated=True).iloc[-1]
    raw = {'periodic': [{'statement': 'income', 'row': captured.to_dict(),
                         'table': [r for r in material.financial_table(fin, {}) if r['id'] == 'T.net_profit']}]}
    # Company/period/date + availability-only variants of captured 000858 statements.
    for group in raw['periodic']:
        group['row'].update(ts_code=code + '.SH', end_date='20251231', ann_date='20260301', f_ann_date='20260301')
        for row in group['table']:
            row['values'] = {}
            row['exact_values'] = {}
            row.pop('unavailable', None)
    def empty(raw_out):
        raw_out['periodic'] = deepcopy(raw['periodic'])
    def inside(raw_out):
        forecast = body['checkpoints'][0]['forecast']
        midpoint = (forecast['low'] + forecast['high']) / 2 * 10000
        raw_out['forecast'][0].update(net_profit_min=midpoint, net_profit_max=midpoint)
    run_scan(conn, source_with(source, inside))
    assert next(c for c in effective(conn, code) if c['kind'] == 'forecast')['outcome'] == 'inside'
    result = run_scan(conn, source_with(source, empty), '2026-11-01T18:00:00+08:00')
    assert result['status'] == 'done'
    assert all(c['source'] == 'periodic' and c['outcome'] == 'undetermined' and '缺失' in c['reason']
               for c in effective(conn, code))
    observation = disclosure_checks.normalize_periodic(raw['periodic'], code)[0]
    unsupported = next(iter(observation['values'])) + '_unsupported'
    item = effective(conn, code)[0] | {'metric': unsupported}
    assert disclosure_checks.judge(item, observation) is None


def test_persisted_checks_survive_post_write_failure_and_cooldown(disclosure_replay, conn, monkeypatch):
    code, _, source = disclosure_replay
    original = scans._dispatch
    monkeypatch.setattr(scans, '_dispatch', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('variant: after checks')))
    failed = run_scan(conn, source)
    assert failed['status'] == 'failed' and effective(conn, code)
    monkeypatch.setattr(scans, '_dispatch', original)
    recovered = run_scan(conn, source, '2026-11-01T18:00:00+08:00')
    assert recovered['summary']['checks'][code] == []
    notices = [n for n in recovered['notify'] if n['kind'] == 'disclosure_check']
    assert {n['reason'] for n in notices} == {'checkpoint_deviated', 'add_trigger_missed'}
    assert all(code == n['code'] and '披露' in n['detail'] for n in notices)
    repeated = run_scan(conn, source, '2026-11-02T18:00:00+08:00')
    assert not [n for n in repeated['notify'] if n['kind'] == 'disclosure_check']


def test_mechanical_failure_visible_separate_from_directory_and_cleared(disclosure_replay, conn, monkeypatch):
    code, _, source = disclosure_replay
    class Broken(CapturedSources):
        def disclosures(self, code, periods):
            raise RuntimeError('variant: financial fetch failed')
    broken = Broken(source.items)
    first = run_scan(conn, broken)
    second = run_scan(conn, broken, '2026-11-01T18:00:00+08:00')
    card = service.company_card(conn, code)
    failure = card.get('check_failure')
    assert failure and failure['report_id'] == card['report_id'] and failure['scan_id'] == second['id']
    assert scans.ScanHistory(conn, fields=('check_failures', 'checks_succeeded')).checks.get((code, card['report_id'])) == failure
    assert not any(e.get('kind') == 'scan_failure' for e in card['pending_events'])
    daily = advisor_text.render_scan(second, set())
    assert code in daily and 'financial fetch failed' in daily and '机械' in daily
    assert str(card['report_id']) in daily and str(second['id']) in daily
    weekly = advisor_text.render_weekly(scans.weekly(conn, at='2026-11-02T18:00:00+08:00'))
    assert code in weekly and 'financial fetch failed' in weekly and '机械' in weekly
    # Skipping the check is not recovery, even if the rest of a scan succeeds.
    check = scans._check_disclosures
    monkeypatch.setattr(scans, '_check_disclosures', lambda *a, **k: None)
    skipped = run_scan(conn, source, '2026-11-02T18:00:00+08:00')
    assert service.company_card(conn, code)['check_failure']['scan_id'] == second['id']
    monkeypatch.setattr(scans, '_check_disclosures', check)
    # A successful fetch clears the current failure even if a later scan step fails.
    monkeypatch.setattr(scans, '_dispatch', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('variant: after recovery')))
    recovered = run_scan(conn, source, '2026-11-03T18:00:00+08:00')
    assert skipped['status'] == 'done' and recovered['status'] == 'failed'
    restored = service.company_card(conn, code)
    assert restored['check_failure'] is None
    assert not any(e.get('kind') == 'check_failure' for e in restored['pending_events'])
    # Weekly retains failure history, even though the current card recovered.
    assert any(f['scan_id'] == first['id'] and f.get('report_id') == card['report_id']
               for f in scans.weekly(conn, at='2026-11-03T18:00:00+08:00')['failures'])
