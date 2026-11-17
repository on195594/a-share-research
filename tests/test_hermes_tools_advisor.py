"""Scheduled-client transport and retry tests; research prompts use a captured real card."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import fcntl
import json
import subprocess
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from a_share_research.policy.v0 import monitor_research
from a_share_research import disclosure_checks
from test_ops_hermes import ROOT, load
from test_scans import conn, disclosure_replay  # noqa: F401  (fixtures)


@pytest.fixture
def advisor(tmp_path, monkeypatch):
    module = load('a-share-advisor')
    monkeypatch.setattr(module, 'OPPORTUNITY_STATE', tmp_path / 'opportunities.json')
    monkeypatch.setattr(module, 'SCAN_STATE', tmp_path / 'scan.json')
    monkeypatch.setattr(module, 'settings', lambda: ('http://127.0.0.1:8620', 'structural-test-token', None))
    return module


@pytest.fixture
def prompts():
    card = json.loads((ROOT / 'tests/policy_v0/fixtures/monitor/research-card-stance.json').read_text())['card']
    _, prompts, _ = monitor_research.summarize({'status': 'ok', 'cards': [card], 'missing': []}, [], date(2026, 11, 4))
    return [p for p in prompts if p['kind'] == 'research_opportunity']


def transport(advisor, monkeypatch, prompts):
    calls = []
    def call(name, args):
        calls.append((name, deepcopy(args)))
        if name == 'policy.current':
            return dict(installed=True, activation={'id': 7})
        assert name == 'policy.monitor'
        # The request is durable before transport, including when its response is lost.
        assert advisor.read_opportunity_state()['pending'] == args
        return dict(mode='service_monitor_capture', request_id=args['request_id'], activation_id=args['expected_activation_id'],
                    capture_context={'capture_finished_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()},
                    result={'review_prompts': deepcopy(prompts)})
    monkeypatch.setattr(advisor, 'tool', call)
    return call, calls


def test_client_uses_private_config_and_suppresses_server_error_bodies(advisor, monkeypatch):
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'result': {'data': {'codes': []}}}))
    monkeypatch.setenv('ASHARE_HERMES_CONFIG', '/private/hermes.json')
    monkeypatch.setattr(advisor.subprocess, 'run', run)
    assert advisor.tool('account.current', {}) == {'codes': []}
    assert seen[0][0][:5] == ['ashare', '--config', '/private/hermes.json', 'call', 'account.current']
    assert seen[0][1]['timeout'] == 40 and seen[0][1]['stdin'] == subprocess.DEVNULL
    monkeypatch.setattr(advisor.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='private', stderr='secret'))
    with pytest.raises(advisor.AdvisorError) as exc:
        advisor.tool('account.current', {})
    assert 'private' not in str(exc.value) and 'secret' not in str(exc.value)


@pytest.mark.parametrize('response', [{}, {'codes': [], 'source': {}}, {'codes': ['600001.SH'], 'source': {'mode': 'ledger'}}])
def test_invalid_holdings_never_clear_subscriptions(advisor, monkeypatch, response):
    monkeypatch.setattr(advisor, 'tool', lambda *args: response)
    calls = []
    def call(method, path, body=None, **kwargs):
        calls.append(path)
        assert path != '/api/subscriptions', 'unknown holdings must never clear subscriptions'
        if path.startswith('/api/scan-requests/'):
            return dict(version='research-v1', request_id=path.rsplit('/', 1)[-1], scan_id=1)
        return dict(version='research-v1', id=1, status='done', summary={}, notify=[],
                    requested_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
    monkeypatch.setattr(advisor, 'call', call)
    output = advisor.scan()
    assert '/api/scans/1' in calls and '未同步订阅' in output and '检查未完成' in output


def test_scan_refreshes_holdings_at_delivery(advisor, monkeypatch):
    codes = iter([['688578'], []])
    monkeypatch.setattr(advisor, 'holdings', lambda: next(codes))
    def call(method, path, body=None, **kwargs):
        if path == '/api/subscriptions':
            assert body == {'codes': ['688578']}
            return {}
        if path.startswith('/api/scan-requests/'):
            return {'version': 'research-v1', 'request_id': path.rsplit('/', 1)[-1], 'scan_id': 1}
        return {'version': 'research-v1', 'id': 1, 'status': 'done', 'notify': [{}], 'summary': {},
                'requested_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
    monkeypatch.setattr(advisor, 'call', call)
    monkeypatch.setattr(advisor, 'render_scan', lambda result, held, base: 'closed' if held == set() else 'wrong')
    monkeypatch.setattr(advisor, 'opportunities', lambda base: '')
    assert advisor.scan() == 'closed'


def test_real_prompt_dedup_migrates_legacy_keys_and_allows_lapsed_return(advisor, monkeypatch, prompts):
    assert len(prompts) == 1
    advisor.OPPORTUNITY_STATE.write_text(json.dumps([advisor.opportunity_key(prompts[0])]))
    _, calls = transport(advisor, monkeypatch, prompts)
    assert advisor.opportunities() == ''
    assert advisor.read_opportunity_state()['pending'] is None
    original = deepcopy(prompts)
    prompts.clear()
    assert advisor.opportunities() == ''
    prompts.extend(original)
    assert '【建仓机会】' in advisor.opportunities()
    assert advisor.opportunities() == ''
    requests = [args['request_id'] for name, args in calls if name == 'policy.monitor']
    assert len(set(requests)) == 4


def test_lost_response_retries_same_request_without_new_policy_lookup(advisor, monkeypatch, prompts):
    call, calls = transport(advisor, monkeypatch, prompts)
    def ambiguous(name, args):
        result = call(name, args)
        if name == 'policy.monitor': raise advisor.AdvisorError('response lost')
        return result
    monkeypatch.setattr(advisor, 'tool', ambiguous)
    assert '检查未完成' in advisor.opportunities()
    pending = advisor.read_opportunity_state()['pending']
    monkeypatch.setattr(advisor, 'tool', call)
    assert '【建仓机会】' in advisor.opportunities()
    assert calls == [('policy.current', {}), ('policy.monitor', pending), ('policy.monitor', pending)]


@pytest.mark.parametrize('variant', ['replay', 'request', 'activation', 'missing-prompts'])
def test_incompatible_receipt_preserves_keys_and_pending(advisor, monkeypatch, prompts, variant):
    advisor.OPPORTUNITY_STATE.write_text('["previous-key"]')
    call, _ = transport(advisor, monkeypatch, prompts)
    def corrupt(name, args):
        result = call(name, args)
        if name == 'policy.monitor':
            if variant == 'replay': result['mode'] = 'supplied_input_replay'
            if variant == 'request': result['request_id'] = 'another'
            if variant == 'activation': result['activation_id'] = 8
            if variant == 'missing-prompts': result['result'] = {}
        return result
    monkeypatch.setattr(advisor, 'tool', corrupt)
    assert '检查未完成' in advisor.opportunities()
    assert advisor.read_opportunity_state()['seen'] == ['previous-key']
    assert advisor.read_opportunity_state()['pending'] is not None


def test_recovered_old_receipt_is_not_presented_as_today(advisor, monkeypatch, prompts):
    advisor.OPPORTUNITY_STATE.write_text('["previous-key"]')
    call, _ = transport(advisor, monkeypatch, prompts)
    def old(name, args):
        result = call(name, args)
        if name == 'policy.monitor':
            result['capture_context']['capture_finished_at'] = (datetime.now(ZoneInfo('Asia/Shanghai')) - timedelta(days=1)).isoformat()
        return result
    monkeypatch.setattr(advisor, 'tool', old)
    assert '未作为今日机会推送' in advisor.opportunities()
    assert advisor.read_opportunity_state() == dict(schema_version=1, seen=['previous-key'], pending=None)


def test_concurrent_and_corrupt_state_do_not_execute_tools(advisor, monkeypatch):
    monkeypatch.setattr(advisor, 'tool', lambda *args: pytest.fail('must not acquire'))
    with advisor.OPPORTUNITY_STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert '已有检查' in advisor.opportunities()
    advisor.OPPORTUNITY_STATE.write_text('{')
    assert '检查未完成' in advisor.opportunities()
    assert advisor.OPPORTUNITY_STATE.read_text() == '{'


@pytest.mark.parametrize(('reason', 'label'), [
    ('checkpoint_deviated', '披露偏离预测区间'), ('add_trigger_missed', '加仓条件未达到'),
])
@pytest.mark.parametrize('held', [True, False])
def test_disclosure_notice_shows_detail_and_review_action(advisor, reason, label, held):
    capture = json.loads((ROOT / 'tests/data/disclosure-checks.json').read_text())
    observation, = disclosure_checks.normalize_forecast(capture['tushare']['forecast']['603960.SH'], '603960')
    bounds = observation['values']['T.net_profit']
    # Rendering-only envelope around the captured disclosure, not a company judgment.
    detail = (f"归母净利润 {observation['period']}：披露区间 {bounds['low']}–{bounds['high']} 亿元；"
              f"业绩预告 {observation['announced']}")
    item = dict(code='603960', reason=reason, detail=detail, kind='disclosure_check')
    text = advisor.notice_text(item, {'603960'} if held else set())
    assert f'【{label}】' in text
    assert f'发生了什么：{detail}' in text
    if reason == 'add_trigger_missed' and held:
        assert '对你：你仍持仓：加仓条件未达到，复核是否减仓。' in text
    if not held:
        assert '当前未持仓：无需处理持仓' in text
        assert '复核是否减仓' not in text
    assert '%' not in text and '卖出比例' not in text


def test_opportunity_rebind_preserves_seen_until_success(advisor, monkeypatch, prompts):
    original = dict(request_id='advisor-before-release', expected_activation_id=3)
    advisor.OPPORTUNITY_STATE.write_text(json.dumps(dict(schema_version=1, seen=['old-key'], pending=original)))
    calls = []
    def tool(name, args):
        calls.append((name, deepcopy(args)))
        if name == 'policy.current': return dict(installed=True, activation={'id': 4})
        assert advisor.read_opportunity_state()['seen'] == ['old-key']
        assert advisor.read_opportunity_state()['pending'] == args
        if args['request_id'] == original['request_id']:
            raise advisor.AdvisorError('certain', code='policy_activation_changed')
        return dict(mode='service_monitor_capture', request_id=args['request_id'], activation_id=4,
            capture_context={'capture_finished_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()},
            result={'review_prompts': deepcopy(prompts)})
    monkeypatch.setattr(advisor, 'tool', tool)
    assert advisor.opportunities()
    assert len(calls) == 3 and calls[-1][1]['expected_activation_id'] == 4
    assert advisor.read_opportunity_state()['pending'] is None


def test_disclosure_scan_still_finishes_when_tools_opportunity_phase_is_paused(advisor, monkeypatch):
    original = dict(request_id='opportunity-before-pause', expected_activation_id=7)
    advisor.OPPORTUNITY_STATE.write_text(json.dumps(dict(schema_version=1, seen=['keep-seen'], pending=original)))
    web_calls = []
    monkeypatch.setattr(advisor, 'holdings', lambda: ['688578'])
    def call(method, path, body=None, **kwargs):
        web_calls.append(path)
        if path == '/api/subscriptions': return {}
        if path.startswith('/api/scan-requests/'):
            return dict(version='research-v1', request_id=path.rsplit('/',1)[-1], scan_id=1)
        return dict(version='research-v1', id=1, status='done', notify=[{}], summary={},
                    requested_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
    monkeypatch.setattr(advisor, 'call', call)
    monkeypatch.setattr(advisor, 'render_scan', lambda *a: 'disclosure-scan-result')
    def paused(*a): raise advisor.AdvisorError('工具服务已暂停；原请求保留')
    monkeypatch.setattr(advisor, 'tool', paused)
    output = advisor.scan()
    assert 'disclosure-scan-result' in output and '检查未完成' in output and '暂停' in output
    assert '/api/scans/1' in web_calls and advisor.read_scan_state()['pending'] is None
    assert advisor.read_opportunity_state() == dict(schema_version=1, seen=['keep-seen'], pending=original)


@pytest.mark.parametrize('paused_at', ['submit', 'delivery'])
def test_real_holdings_failure_never_blocks_web_scan_or_claims_empty_account(advisor, monkeypatch, paused_at):
    original = dict(request_id='opportunity-pending', expected_activation_id=7)
    advisor.OPPORTUNITY_STATE.write_text(json.dumps(dict(schema_version=1, seen=['keep-seen'], pending=original)))
    calls, account_reads = [], []
    subscriptions = ['688001']
    def call(method, path, body=None, **kwargs):
        calls.append(path)
        if path == '/api/subscriptions':
            subscriptions[:] = body['codes']
            return {}
        if path.startswith('/api/scan-requests/'):
            return dict(version='research-v1', request_id=path.rsplit('/', 1)[-1], scan_id=1)
        return dict(version='research-v1', id=1, status='done', summary={},
                    notify=[dict(code='688578', name='isolated notice', reason='major_risk', card=None,
                                 detail='isolated disclosure', path='/stock/688578')],
                    requested_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
    def tools(name, args):
        if name == 'account.current':
            account_reads.append(name)
            if paused_at == 'delivery' and len(account_reads) == 1:
                return dict(codes=['688578'], source=dict(mode='ledger'))
        raise advisor.AdvisorError('工具服务已暂停；原请求保留')
    monkeypatch.setattr(advisor, 'call', call)
    monkeypatch.setattr(advisor, 'tool', tools)  # Do not mock holdings() or render_scan().
    output = advisor.scan()
    assert '/api/scans/1' in calls and len(account_reads) == 2
    assert subscriptions == (['688001'] if paused_at == 'submit' else ['688578'])
    assert ('/api/subscriptions' in calls) is (paused_at == 'delivery')
    assert '发现风险公告' in output and '持仓状态未知' in output and '检查未完成' in output
    assert '当前未持仓' not in output and '你仍持仓' not in output
    assert advisor.read_scan_state()['pending'] is None
    assert advisor.read_opportunity_state() == dict(schema_version=1, seen=['keep-seen'], pending=original)



def test_scan_and_weekly_show_check_failures_gaps_and_open_events(advisor, conn, disclosure_replay):  # noqa: F811
    from a_share_research import db, scans, service
    from test_scans import AT, CapturedSources, run_scan
    code, _, source = disclosure_replay

    class Failure(CapturedSources):
        def valuation(self, requested):
            return super().valuation('603606') | {'ts_code': requested}  # Identity variant, as in test_scans.

        def disclosures(self, code, periods):
            raise RuntimeError('披露取数暂不可用')
    first = run_scan(conn, Failure(source.items))
    run_id, = first['summary']['queued_runs']
    conn.execute("UPDATE scans SET started_at='2026-09-20T18:00:00+08:00' WHERE id=?", (first['id'],))  # Outage variant.
    db.update_run(conn, run_id, status='failed', error='变体：研究失败', finished_at='2026-10-30T19:00:00+08:00')
    late = run_scan(conn, Failure(source.items))
    text = advisor.render_scan(late, set())
    assert '披露核对未完成' in text and f'{code} disclosure_checks：披露取数暂不可用' in text
    assert f'{code} 2026-09-20 至 2026-09-30' in text and '不能视为连续覆盖' in text
    weekly = advisor.render_weekly(json.loads(json.dumps(scans.weekly(conn, at=AT), default=str)))
    assert f'最后成功扫描：{late["started_at"][:16]}' in weekly
    assert '未覆盖区间' in weekly and f'其中披露核对失败 2 次（{code}）' in weekly
    assert '已纳入研究不等于风险排除' in weekly
    assert f'研究失败，可重试；重试任务 #{run_id}' in weekly
    service.retry_run(conn, run_id)
    assert '研究进行中' in advisor.render_weekly(json.loads(json.dumps(scans.weekly(conn, at=AT), default=str)))
    # Explicit association-missing variant: a projection failure has no report ID.
    unlinked = deepcopy(late)
    for failure in unlinked['summary']['check_failures']:
        failure['report_id'] = None
    assert '报告 #pending' in advisor.render_scan(unlinked, set())
    unlinked_week = scans.weekly(conn, at=AT)
    for failure in unlinked_week['failures']:
        if failure.get('stage') == 'disclosure_checks':
            failure['report_id'] = None
    assert '报告 #pending' in advisor.render_weekly(unlinked_week)
