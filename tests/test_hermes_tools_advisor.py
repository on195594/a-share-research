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
from test_ops_hermes import ROOT, load


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
    monkeypatch.setattr(advisor, 'call', lambda *args: pytest.fail('invalid holdings must not update subscriptions'))
    with pytest.raises(advisor.AdvisorError): advisor.scan()


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
