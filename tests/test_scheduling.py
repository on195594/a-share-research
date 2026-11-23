"""Slot, policy receipt and delivery contracts against isolated SQLite databases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from threading import Event

from fastapi.testclient import TestClient
import pytest

from a_share_research import api_keys, db, scans
from a_share_research.account import mirror
from a_share_research.policy import benchmark, monitor, registry, stop_alert
from a_share_research.policy.v0 import commands_monitor
from a_share_research.tools.registry import Context, ToolError, invoke
from a_share_research.tools.server import create_app
from support.evaluations import benchmark_bundle, stop_bundle
from support.mcp import RESOURCE, ORIGIN, rpc
from support.scans import CapturedSources

CARDS = Path(__file__).parent / 'policy_v0/fixtures/monitor'
DAY = '2026-10-22'


@pytest.fixture
def context(policy_setup, monkeypatch):
    path, conn, principals, *_ = policy_setup
    clock = [DAY + 'T10:40:00+08:00']
    monkeypatch.setattr(db, 'now', lambda: clock[0])
    return Context(conn, path, principals['assistant'], True), clock


@pytest.fixture
def stop_capture(monkeypatch):
    calls = []

    def capture(database, module, payload):
        assert module.endswith('.stop_alert')
        calls.append(payload['previous_state'])
        return stop_bundle(state=calls[-1]) | {'risk_policy': payload['parameters']}

    monkeypatch.setattr(monitor, '_capture', capture)
    return calls


def run(ctx, job, slot):
    return invoke(ctx, 'schedule_run', dict(job=job, slot=slot))['data']


def ack(ctx, row):
    return invoke(ctx, 'schedule_ack', dict(run_id=row['run_id']))['data']


def receipt(ctx, row):
    return registry.evaluation(ctx.connection, ctx.principal, row['evaluation_id'])


def test_stop_delivered_state_and_replay(context, stop_capture):
    ctx, clock = context
    a, b, c = [DAY + 'T' + t for t in ('10:25', '10:30', '10:35')]
    first = run(ctx, 'stop_alert', a)  # Exactly 15 minutes remains eligible.
    assert first['status'] == 'ready' and '止损预警' in first['text']
    assert run(ctx, 'stop_alert', a) == first and len(stop_capture) == 1
    repeated = run(ctx, 'stop_alert', b)
    assert repeated['text'] == first['text'] and stop_capture == [{}, {}]
    assert first['state'] == receipt(ctx, first)['result']['state']
    saved = ack(ctx, repeated)
    clock[0] = DAY + 'T10:40:01+08:00'
    assert ack(ctx, repeated) == saved == run(ctx, 'stop_alert', b)
    after = run(ctx, 'stop_alert', c)
    assert after['text'] == '' and stop_capture[-1] == repeated['state']
    assert run(ctx, 'stop_alert', a)['status'] == 'expired'  # A ready, undelivered stale alert.
    clock[0] = '2026-10-23T09:31:00+08:00'
    run(ctx, 'stop_alert', '2026-10-23T09:30')
    assert stop_capture[-1] == {}


def test_stop_state_is_private_even_with_same_job_and_slot(context, stop_capture):
    ctx, _ = context
    slot = DAY + 'T10:35'
    ack(ctx, run(ctx, 'stop_alert', slot))
    principal = api_keys.authenticate(ctx.connection, api_keys.create(ctx.connection, 'other', role='assistant'))
    other = Context(ctx.connection, ctx.database, principal, True)
    assert run(other, 'stop_alert', slot)['text'] and stop_capture == [{}, {}]


def test_concurrent_duplicate_collects_once(context, stop_capture, monkeypatch):
    ctx, _ = context
    inside, second, release = Event(), Event(), Event()
    capture = monitor._capture

    def collect(*args):
        inside.set()
        assert release.wait(5)
        return capture(*args)

    monkeypatch.setattr(monitor, '_capture', collect)

    def on_connection(mark=None):
        with closing(sqlite3.connect(ctx.database, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            if mark:
                mark.set()
            return run(Context(conn, ctx.database, ctx.principal, True), 'stop_alert', DAY + 'T10:35')

    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(on_connection)
        assert inside.wait(5)
        two = pool.submit(on_connection, second)
        assert second.wait(5)
        try:
            with pytest.raises(TimeoutError):
                two.result(timeout=.15)
        finally:
            release.set()
        assert one.result() == two.result()
    assert len(stop_capture) == 1


@pytest.mark.parametrize(('job', 'slot'), [
    ('stop_alert', DAY + 'T10:31'), ('stop_alert', DAY + 'T10:3'), ('stop_alert', DAY + 'T10:45'),
    ('daily_brief', '2026-02-30'), ('daily_brief', '20261022'), ('daily_brief', '2026-10-23'),
    ('advisor_scan', '2026-10-23'), ('advisor_weekly', '2025-W53'), ('advisor_weekly', '2026-W44'),
    ('advisor_weekly', '2026-W1'), ('benchmark', '2026-9'), ('benchmark', '2026-11'),
    ('monthly_submit', '2026-10'),
])
def test_invalid_or_future_slots_write_nothing(context, job, slot):
    ctx, _ = context
    with pytest.raises(ToolError) as exc:
        run(ctx, job, slot)
    assert exc.value.code == 'invalid_arguments'
    assert not ctx.connection.execute('SELECT 1 FROM scheduled_runs').fetchone()


@pytest.mark.parametrize(('job', 'slot'), [('stop_alert', DAY + 'T10:20'),
                                          ('daily_brief', '2026-10-21'), ('advisor_scan', '2026-10-21')])
def test_expired_before_collection(context, job, slot):
    ctx, _ = context
    stale = run(ctx, job, slot)
    assert stale['status'] == 'expired' and bool(stale['text']) == (job != 'stop_alert')
    assert run(ctx, job, slot) == stale and not stale['activation_id'] and not stale['scan_id']
    assert not ctx.connection.execute('SELECT 1 FROM policy_evaluations UNION ALL SELECT 1 FROM scans').fetchone()
    with pytest.raises(ToolError, match='ready'):
        ack(ctx, stale)


@pytest.mark.parametrize(('exception', 'code'), [
    (registry.PolicyError('采集失败 token=secret', 503), 'unavailable'),
    (ToolError('capture_timeout', '采集失败 token=secret', 504), 'capture_timeout'),
    (RuntimeError('采集失败 token=secret'), 'service_failure'),
])
def test_failed_receipt_and_retry(context, stop_capture, monkeypatch, exception, code):
    ctx, _ = context
    capture = monitor._capture

    def fail(*args):
        raise exception

    monkeypatch.setattr(monitor, '_capture', fail)
    failed = run(ctx, 'stop_alert', DAY + 'T10:35')
    assert failed['status'] == 'failed' and failed['error'] == '采集失败 token=[REDACTED]'
    assert failed['error_code'] == code and failed['attempts'] == 1
    row = ctx.connection.execute('SELECT error,error_code FROM scheduled_runs').fetchone()
    assert tuple(row) == (failed['error'], failed['error_code'])
    with pytest.raises(ToolError, match='ready'):
        ack(ctx, failed)
    monkeypatch.setattr(monitor, '_capture', capture)
    retried = run(ctx, 'stop_alert', failed['slot'])
    assert retried['run_id'] == failed['run_id'] and retried['attempts'] == 2
    assert retried['status'] == 'ready' and retried['error_code'] is None and retried['error'] is None


@pytest.mark.parametrize('switch', [False, True])
def test_failed_evaluation_retries_on_new_activation(context, policy_setup, monkeypatch, switch):
    ctx, clock = context
    _, conn, principals, manifest, active, *_ = policy_setup
    calls = []

    def capture(database, module, payload, *, market_token):
        calls.append(payload['end'])
        bundle = benchmark_bundle()
        bundle.update(risk_policy=payload['parameters'], end=payload['end'])
        if len(calls) == 1:
            bundle.update(events=[], indices={}, prices={})
        else:
            bundle['prices']['600900'].update(status='degraded', fallback_reason='停牌', freshness_days=1)
        return bundle

    monkeypatch.setattr(monitor, '_capture', capture)
    failed = run(ctx, 'benchmark', '2026-10')
    assert failed['status'] == 'failed' and failed['error_code'] == 'no_events'
    assert failed['activation_id'] == active['id'] and receipt(ctx, failed)['result']['exit_code'] == 1
    new = active
    if switch:
        new = registry.activate(conn, principals['owner'], manifest['identity'], expected_id=active['id'],
                                request_id='switch', reason='retry a failed schedule with a new activation')
    clock[0] = '2026-11-01T09:00:00+08:00'
    ready = run(ctx, 'benchmark', '2026-10')
    saved = receipt(ctx, ready)
    assert ready['status'] == 'ready' and ready['attempts'] == 2 and ready['activation_id'] == new['id']
    assert saved['activation_id'] == new['id'] and saved['request_id'] != receipt(ctx, failed)['request_id']
    assert calls == [DAY, DAY] and ready['evaluation_id'] != failed['evaluation_id']
    assert ready['text'] == '📊 A股账本月度对比\n' + saved['result']['stdout']
    assert ready['result']['stderr'] == saved['result']['stderr'] and '价格参考' in ready['result']['stderr']


@pytest.mark.parametrize('switch', [True, False])
def test_interrupted_after_evaluation_recovers_on_current_activation(context, policy_setup, stop_capture, monkeypatch, switch):
    ctx, _ = context
    _, conn, principals, manifest, active, *_ = policy_setup
    original = stop_alert.run

    def interrupt(*args):
        original(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(stop_alert, 'run', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(ctx, 'stop_alert', DAY + 'T10:35')
    assert conn.execute('SELECT status FROM scheduled_runs').fetchone()[0] == 'running'
    if switch:
        active = registry.activate(conn, principals['owner'], manifest['identity'], expected_id=active['id'],
                                   request_id='interrupt-switch', reason='change before recovery')
    else:
        monkeypatch.setattr(stop_alert, 'run', original)
        ack(ctx, run(ctx, 'stop_alert', DAY + 'T10:30'))
    monkeypatch.setattr(stop_alert, 'run', original)
    ready = run(ctx, 'stop_alert', DAY + 'T10:35')
    assert ready['status'] == 'ready' and ready['activation_id'] == active['id'] and ready['attempts'] == 1
    assert len(stop_capture) == 2 and receipt(ctx, ready)['input']['previous_state'] == {}


def test_ack_ownership_and_running_conflict(context):
    ctx, _ = context
    scan = run(ctx, 'advisor_scan', DAY)
    assert scan['status'] == 'running' and scan['scan_id']
    with pytest.raises(ToolError) as exc:
        ack(ctx, scan)
    assert exc.value.code == 'conflict'
    token = api_keys.create(ctx.connection, 'different assistant', role='assistant')
    other = Context(ctx.connection, ctx.database, api_keys.authenticate(ctx.connection, token), True)
    with pytest.raises(ToolError) as exc:
        ack(other, scan)
    assert exc.value.code == 'not_found'
    ctx.connection.execute("UPDATE scans SET status='running' WHERE id=?", (scan['scan_id'],))
    scans.recover(ctx.connection)
    ready = run(ctx, 'advisor_scan', DAY)
    assert ready['status'] == 'ready' and ready['result']['scan']['status'] == 'failed'
    with pytest.raises(ToolError) as exc:
        ack(other, ready)
    assert exc.value.code == 'not_found'
    assert ack(ctx, ready)['status'] == 'acked'


def test_today_replays_ready_backlog_until_ack(context, policy_setup):
    ctx, clock = context
    bundle = policy_setup[-2]
    bundle.update(now='2026-10-23T16:37:00+08:00', research_result=dict(status='ok', cards=[], missing=[]))
    old = run(ctx, 'advisor_scan', DAY)
    clock[0] = '2026-10-23T16:37:00+08:00'
    waiting = run(ctx, 'advisor_scan', '2026-10-23')
    assert (waiting['slot'], waiting['status'], waiting['scan_id']) == (DAY, 'running', old['scan_id'])
    assert ctx.connection.execute('SELECT count(*) FROM scans').fetchone()[0] == 1
    scans.execute(ctx.connection, old['scan_id'], source=CapturedSources(), at=clock[0])
    late = run(ctx, 'advisor_scan', '2026-10-23')
    assert (late['slot'], late['status'], late['result']['scan']['id']) == (DAY, 'ready', old['scan_id'])
    assert run(ctx, 'advisor_scan', '2026-10-23') == late
    assert ctx.connection.execute('SELECT count(*) FROM scheduled_runs').fetchone()[0] == 1
    ack(ctx, late)
    today = run(ctx, 'advisor_scan', '2026-10-23')
    assert today['slot'] == '2026-10-23' and today['status'] == 'running' and today['scan_id'] != old['scan_id']


def test_scan_polls_one_request_and_only_inherits_acked_current_keys(context, policy_setup):
    ctx, clock = context
    bundle, captured = policy_setup[-2:]
    card = json.loads((CARDS / 'research-card-stance.json').read_text())['card']
    bundle.update(now='2026-11-04T16:37:00+08:00', research_result=dict(status='ok', cards=[card], missing=[]))
    key = "688578|1|" + card['stance_calc']['action'] + "|" + card['current']['valuation']['v2']['zone']

    def scan(day, cards):
        clock[0] = day + 'T16:37:00+08:00'
        bundle['research_result']['cards'] = cards
        running = run(ctx, 'advisor_scan', day)
        before = len(captured)
        assert running['status'] == 'running' and run(ctx, 'advisor_scan', day) == running
        assert len(captured) == before
        assert ctx.connection.execute('SELECT count(*) FROM scan_requests WHERE scan_id=?', (running['scan_id'],)).fetchone()[0] == 1
        scans.execute(ctx.connection, running['scan_id'], source=CapturedSources(), at=clock[0])
        ready = run(ctx, 'advisor_scan', day)
        assert ready['status'] == 'ready', ready
        assert ready['result']['scan']['id'] == running['scan_id'] and len(captured) == before + 1
        assert run(ctx, 'advisor_scan', day) == ready and len(captured) == before + 1
        return ready

    first = scan('2026-11-04', [card])
    assert first['state'] == {'seen': [key]} and len(first['result']['opportunities']) == 1
    clock[0] = '2026-11-05T16:37:00+08:00'
    assert run(ctx, 'advisor_scan', '2026-11-05') == first
    ack(ctx, first)
    muted = scan('2026-11-06', [card])
    assert muted['result']['opportunities'] == [] and muted['state'] == first['state']
    ack(ctx, muted)
    gone = scan('2026-11-07', [])
    assert gone['state'] == {'seen': []}
    ack(ctx, gone)
    assert scan('2026-11-08', [card])['result']['opportunities'] == first['result']['opportunities']


def test_weekly_filters_current_comparisons_separately(context, policy_setup, monkeypatch):
    ctx, clock = context
    payload = json.loads((CARDS / 'research-cards.json').read_text())['response']
    for item in payload['cards']:
        if valuation := item['current']['valuation']:
            valuation['neutral_return_without_dividends'] = valuation['annual_returns']['中性']
    # Replay the captured holding membership and company cards through the shipping monitor.
    position = next(p for p in json.loads((CARDS / 'five-holding-snapshot.json').read_text())['holdings']
                    if p['code'] == '600900')
    name = next(c['name'] for c in payload['cards'] if c['code'] == position['code'])
    ctx.connection.execute("INSERT INTO holdings(code,name,shares,updated_at,framework) VALUES (:code,:name,:shares,:updated_at,'未核实')",
                           position | {'name': name, 'updated_at': db.now()})
    ctx.connection.execute('UPDATE account_source SET source_sha256=?',
                           (mirror.digest(mirror._read(ctx.connection, ctx.database.parent)),))
    monkeypatch.setenv('POLICY_V0_DB', str(ctx.database))
    bundle = policy_setup[-2]
    bundle.update(local=commands_monitor._load_monitor_local_snapshot(), now='2026-11-02T15:00:00+08:00',
                  research_result=payload | {'status': 'ok'})
    clock[0] = '2026-11-08T18:07:00+08:00'
    first = run(ctx, 'advisor_weekly', '2026-W45')
    assert first['status'] == 'ready', first['error']
    costs = [p for p in receipt(ctx, first)['result']['review_prompts'] if p['kind'] == 'opportunity_cost']
    assert len(costs) == 1 and first['result']['opportunity_costs'] == costs
    assert first['result']['weekly']['as_of'] == clock[0]
    ack(ctx, first)
    clock[0] = '2026-11-15T18:07:00+08:00'
    second = run(ctx, 'advisor_weekly', '2026-W46')
    assert second['result']['opportunity_costs'] == [] and second['state'] == first['state']


def test_daily_real_capture_over_mcp_then_next_day_lookup(context, monkeypatch):
    ctx, clock = context
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)
    token = api_keys.create(ctx.connection, 'scheduled-mcp', role='assistant')
    app = create_app(ctx.database, account_shadow_enabled=True)
    with TestClient(app, base_url=ORIGIN) as client:
        def call(name, **args):
            body = rpc(client, token, 'tools/call', {'name': name, 'arguments': args}).json()
            result = body['result']
            assert not result.get('isError'), result
            return result['structuredContent']['data']

        # The shipping child reads the isolated empty mirror and needs no market connection.
        clock[0] = datetime.now(db.SHANGHAI).isoformat(timespec='seconds')
        slot = clock[0][:10]
        ready = call('schedule_run', job='daily_brief', slot=slot)
        assert ready['status'] == 'ready' and ready['evaluation_id']
        clock[0] = (datetime.fromisoformat(clock[0]) + timedelta(days=1)).isoformat()
        assert call('schedule_run', job='daily_brief', slot=slot) == ready
        assert call('schedule_ack', run_id=ready['run_id'])['status'] == 'acked'


def test_daily_text_uses_account_reconciliation_wording(context, daily_bundle, monkeypatch):
    ctx, _ = context
    # The recorded dividend fixture adds its holding after the isolated mirror was registered.
    ctx.connection.execute('UPDATE account_source SET source_sha256=?',
                           (mirror.digest(mirror._read(ctx.connection, ctx.database.parent)),))
    monkeypatch.setattr(monitor, '_capture', lambda database, module, payload, **kw: daily_bundle)
    ready = run(ctx, 'daily_brief', DAY)
    assert '登记日2026-09-17 应收280.00元（通过 a-share 核对券商流水）' in ready['text']
    assert '运行 dividend-check 核对' in receipt(ctx, ready)['result']['stdout']


def test_stop_text_replaces_legacy_policy_name(context, stop_capture, monkeypatch):
    from a_share_research.policy import stop_evaluation
    evaluate = stop_evaluation.evaluate

    def legacy_wording(*args):
        result = evaluate(*args)
        result['stdout'] = result['stdout'].replace('按 a-share 的生效政策复核', '按 a-stock-monitor 复核')
        return result

    monkeypatch.setattr(stop_evaluation, 'evaluate', legacy_wording)
    ctx, _ = context
    ready = run(ctx, 'stop_alert', DAY + 'T10:35')
    assert '按 a-share 的生效政策复核止损与 L3' in ready['text']
    assert '按 a-stock-monitor 复核止损与 L3' in receipt(ctx, ready)['result']['stdout']


@pytest.mark.parametrize(('day', 'lookup'), [(DAY, DAY), ('2026-10-23', DAY), ('2026-10-23', '2026-10-23')])
def test_recover_scan_submission_before_expiry(context, monkeypatch, day, lookup):
    ctx, clock = context
    submit = scans.submit_request

    def interrupted(*args):
        scan_id = submit(*args)
        # A terminal worker cannot be found via the active-scan coalescing path.
        ctx.connection.execute("UPDATE scans SET status='done' WHERE id=?", (scan_id,))
        raise KeyboardInterrupt

    monkeypatch.setattr(scans, 'submit_request', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(ctx, 'advisor_scan', DAY)
    assert ctx.connection.execute('SELECT scan_id FROM scheduled_runs').fetchone()[0] is None
    existing, request_id = ctx.connection.execute('SELECT scan_id,request_id FROM scan_requests').fetchone()
    assert DAY in request_id
    monkeypatch.setattr(scans, 'submit_request', submit)
    clock[0] = day + 'T16:37:00+08:00'
    ready = run(ctx, 'advisor_scan', lookup)
    assert (ready['slot'], ready['status'], ready['scan_id']) == (DAY, 'ready', existing)
    assert ctx.connection.execute('SELECT count(*) FROM scans').fetchone()[0] == 1
    assert ctx.connection.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 1


@pytest.mark.parametrize('older_higher', [True, False])
def test_late_acks_keep_highest_stop_level(context, monkeypatch, older_higher):
    ctx, _ = context
    prices = iter([22.0, 24.0, 22.0] if older_higher else [24.0, 22.0, 22.0])

    def capture(database, module, payload):
        return stop_bundle(price=next(prices), state=payload['previous_state'])

    monkeypatch.setattr(monitor, '_capture', capture)
    older = run(ctx, 'stop_alert', DAY + 'T10:30')
    newer = run(ctx, 'stop_alert', DAY + 'T10:35')
    high, low = (older, newer) if older_higher else (newer, older)
    assert high['state']['levels'] == {'000963': 'stop2'} and low['state']['levels'] == {'000963': 'stop1'}
    ack(ctx, newer)
    ack(ctx, older)
    latest = run(ctx, 'stop_alert', DAY + 'T10:40')
    assert receipt(ctx, latest)['input']['previous_state'] == high['state'] and latest['text'] == ''


def test_stop_stales_while_capture_in_progress(context, stop_capture, monkeypatch):
    ctx, clock = context
    capture = monitor._capture

    def crosses_cutoff(*args):
        clock[0] = DAY + 'T10:40:01+08:00'
        return capture(*args)

    monkeypatch.setattr(monitor, '_capture', crosses_cutoff)
    row = run(ctx, 'stop_alert', DAY + 'T10:25')
    assert row['status'] == 'expired' and row['text'] == '' and row['state'] is None
    assert receipt(ctx, row)['result']['alerts']


def test_interrupted_before_capture_uses_latest_ack(context, stop_capture, monkeypatch):
    ctx, _ = context

    def interrupted(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(stop_alert, 'run', interrupted)
        with pytest.raises(KeyboardInterrupt):
            run(ctx, 'stop_alert', DAY + 'T10:35')
    earlier = ack(ctx, run(ctx, 'stop_alert', DAY + 'T10:30'))
    resumed = run(ctx, 'stop_alert', DAY + 'T10:35')
    assert resumed['status'] == 'ready' and resumed['text'] == ''
    assert stop_capture == [{}, earlier['state']]


def test_late_disclosures_keep_seen_until_todays_monitor(context, policy_setup, monkeypatch):
    ctx, clock = context
    card = json.loads((CARDS / 'research-card-stance.json').read_text())['card']
    bundle, captured = policy_setup[-2:]
    bundle.update(now='2026-11-04T16:37:00+08:00', research_result=dict(status='ok', cards=[card], missing=[]))
    clock[0] = bundle['now']
    first = run(ctx, 'advisor_scan', '2026-11-04')
    scans.execute(ctx.connection, first['scan_id'], source=CapturedSources(), at=clock[0])
    seen = ack(ctx, run(ctx, 'advisor_scan', first['slot']))['state']
    assert seen['seen']
    clock[0] = '2026-11-05T16:37:00+08:00'
    old = run(ctx, 'advisor_scan', '2026-11-05')
    clock[0] = '2026-11-06T16:37:00+08:00'
    scans.execute(ctx.connection, old['scan_id'], source=CapturedSources(), at=clock[0])
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise registry.PolicyError('采集超时', 504)
        patch.setattr(monitor, 'capture', unavailable)
        late = run(ctx, 'advisor_scan', '2026-11-06')
    assert late['status'] == 'ready' and late['evaluation_id'] is None
    assert late['result']['scan']['id'] == old['scan_id'] and late['result']['opportunities'] == []
    assert ack(ctx, late)['state'] == seen
    today = run(ctx, 'advisor_scan', '2026-11-06')
    scans.execute(ctx.connection, today['scan_id'], source=CapturedSources(), at=clock[0])
    ready = run(ctx, 'advisor_scan', today['slot'])
    assert len(captured) == 2 and ready['state'] == seen and ready['result']['opportunities'] == []


def test_unreadable_backlog_expires_once_and_releases_today(context, monkeypatch):
    ctx, clock = context
    old = run(ctx, 'advisor_scan', DAY)
    scans.execute(ctx.connection, old['scan_id'], source=CapturedSources(), at=clock[0])
    def unavailable(*args):
        raise registry.PolicyError('扫描回执读取失败', 503)
    monkeypatch.setattr(scans, 'view', unavailable)
    assert run(ctx, 'advisor_scan', DAY)['status'] == 'failed'
    clock[0] = '2026-10-23T16:37:00+08:00'
    terminal = run(ctx, 'advisor_scan', '2026-10-23')
    assert (terminal['slot'], terminal['status']) == (DAY, 'expired')
    assert '扫描回执读取失败' in terminal['text'] and terminal['error_code'] == 'unavailable'
    today = run(ctx, 'advisor_scan', '2026-10-23')
    assert today['slot'] == '2026-10-23' and today['scan_id'] != old['scan_id']


def test_weekly_retry_reuses_digest_after_week_ends(context, monkeypatch):
    ctx, clock = context
    clock[0] = '2026-10-25T18:07:00+08:00'
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise registry.PolicyError('采集超时', 504)
        patch.setattr(monitor, 'capture', unavailable)
        failed = run(ctx, 'advisor_weekly', '2026-W43')
    assert failed['status'] == 'failed' and failed['error_code'] == 'timeout'
    assert failed['result']['weekly']['as_of'] == clock[0] and failed['result']['opportunity_costs'] == []
    clock[0] = '2026-10-26T09:00:00+08:00'
    ready = run(ctx, 'advisor_weekly', '2026-W43')
    assert ready['status'] == 'ready' and ready['result']['weekly'] == failed['result']['weekly']


def test_historical_week_without_digest_expires(context):
    ctx, _ = context
    row = run(ctx, 'advisor_weekly', '2026-W42')
    assert row['status'] == 'expired' and row['result'] is None


@pytest.mark.parametrize('interrupted_legacy_receipt', [False, True])
def test_historical_benchmark_ends_in_requested_month(context, monkeypatch, interrupted_legacy_receipt):
    ctx, clock = context
    clock[0] = '2026-11-01T09:00:00+08:00'
    def capture(database, module, payload, *, market_token):
        return benchmark_bundle() | dict(risk_policy=payload['parameters'], now=clock[0], end=payload['end'])
    monkeypatch.setattr(monitor, '_capture', capture)
    if interrupted_legacy_receipt:
        # v26 could commit a next-month evaluation before linking it to the run.
        active = registry._current(ctx.connection)['id']
        run_id = ctx.connection.execute("""INSERT INTO scheduled_runs
            (principal_key_id,job,slot,status,created_at,activation_id) VALUES (?,'benchmark','2026-10','running',?,?)""",
            (ctx.principal.key_id, clock[0], active)).lastrowid
        benchmark.run(ctx.connection, ctx.principal, ctx.database, f'schedule:{run_id}:{active}:1', active, '2026-11-01')
    row = run(ctx, 'benchmark', '2026-10')
    assert row['status'] == 'ready' and receipt(ctx, row)['input']['end'] == '2026-10-31'


@pytest.mark.parametrize('switch_again', [False, True])
def test_activation_change_during_capture_rebinds_once(context, policy_setup, stop_capture, monkeypatch, switch_again):
    ctx, _ = context
    _, conn, principals, manifest, active, *_ = policy_setup
    requests = []
    evaluate = stop_alert.run
    def record(conn, principal, database, request_id, *args):
        requests.append(request_id)
        return evaluate(conn, principal, database, request_id, *args)
    capture = monitor._capture
    def switch(*args):
        nonlocal active
        if len(requests) == 1 or switch_again:
            active = registry.activate(conn, principals['owner'], manifest['identity'], expected_id=active['id'],
                                       request_id=f'switch-{len(requests)}', reason='switch during capture')
        return capture(*args)
    monkeypatch.setattr(stop_alert, 'run', record)
    monkeypatch.setattr(monitor, '_capture', switch)
    row = run(ctx, 'stop_alert', DAY + 'T10:35')
    assert len(stop_capture) == len(requests) == 2 and requests[0] != requests[1]
    if switch_again:
        assert row['status'] == 'failed' and row['error_code'] == 'policy_activation_changed'
        assert not conn.execute('SELECT 1 FROM policy_evaluations').fetchone()
    else:
        assert row['status'] == 'ready' and row['activation_id'] == active['id']
        assert receipt(ctx, row)['request_id'] == requests[1]
        assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 1
