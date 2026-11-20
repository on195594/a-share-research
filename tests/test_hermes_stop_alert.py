"""Notification transport tests over v0 canonical-stop boundary fixtures, not live prices."""
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json

import pytest

from a_share_research.policy import stop_evaluation
from test_ops_hermes import load
from test_stop_evaluation import bundle


@pytest.fixture
def client(tmp_path, monkeypatch):
    module = load('a-share-stop-alert')
    monkeypatch.setattr(module, 'STATE', tmp_path / 'state.json')
    class Clock(datetime):
        current = datetime.fromisoformat('2026-10-22T10:31:05+08:00')
        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
    monkeypatch.setattr(module, 'datetime', Clock)
    return module


def transport(client, monkeypatch, price=24.0):
    calls = []
    def call(name, args):
        calls.append((name, deepcopy(args)))
        if name == 'policy_current': return {'installed': True, 'activation': {'id': 7}}
        assert name == 'policy_stop_alert'
        assert client.load_state()['pending'] == args
        value = bundle(price=price, state=args['previous_state'])
        return dict(mode='service_stop_alert_capture', request_id=args['request_id'], activation_id=7,
                    input=value, result=stop_evaluation.evaluate(value, value['risk_policy']))
    monkeypatch.setattr(client, 'tool', call)
    return call, calls


def test_old_state_import_preserves_daily_upgrade_semantics(client, monkeypatch):
    client.STATE.write_text(json.dumps({'date': '2026-10-22', 'levels': {'000963': 'near'}}))
    _, calls = transport(client, monkeypatch)
    out, err, status = client.check()
    assert status == 0 and err == '' and '已跌破第一档' in out
    assert '按 a-share 的生效政策' in out and 'a-stock-monitor' not in out
    assert client.load_state()['state'] == {'date': '2026-10-22', 'levels': {'000963': 'stop1'}}
    assert client.check() == ('', '', 0)
    requests = [args['request_id'] for name, args in calls if name == 'policy_stop_alert']
    assert len(set(requests)) == 2
    transport(client, monkeypatch, price=23.0)
    assert '已跌破第二档' in client.check()[0]


def test_ambiguous_response_reuses_original_request_then_advances_state(client, monkeypatch):
    call, calls = transport(client, monkeypatch)
    stored = {}
    def lost(name, args):
        receipt = call(name, args)
        if name == 'policy_stop_alert':
            stored['receipt'] = receipt
            raise client.ToolsError('ambiguous')
        return receipt
    monkeypatch.setattr(client, 'tool', lost)
    with pytest.raises(client.ToolsError): client.check()
    pending = client.load_state()['pending']
    assert client.load_state()['state'] == {}
    def recover(name, args):
        assert name == 'policy_stop_alert' and args == pending
        return stored['receipt']
    monkeypatch.setattr(client, 'tool', recover)
    assert '第一档' in client.check()[0]
    assert client.load_state()['pending'] is None
    assert len(calls) == 2


def test_expired_receipt_does_not_advance_levels_or_emit_alert(client, monkeypatch):
    transport(client, monkeypatch)
    client.datetime.current += timedelta(minutes=16)
    out, err, status = client.check()
    assert out == '' and status == 1 and '过期评估' in err
    assert client.load_state() == dict(schema_version=1, state={}, pending=None)


@pytest.mark.parametrize('variant', ['mode', 'request', 'activation', 'previous', 'missing-result', 'state', 'future'])
def test_wrong_receipt_keeps_pending_and_notification_state(client, monkeypatch, variant):
    call, _ = transport(client, monkeypatch)
    def corrupt(name, args):
        result = call(name, args)
        if name == 'policy_stop_alert':
            if variant == 'mode': result['mode'] = 'service_monitor_capture'
            if variant == 'request': result['request_id'] = 'other-request'
            if variant == 'activation': result['activation_id'] = 99
            if variant == 'previous': result['input']['previous_state'] = {'date': '2026-10-21'}
            if variant == 'missing-result': result['result'] = {}
            if variant == 'state': result['result']['state'] = {'date': '2026-02-30'}
            if variant == 'future': result['input']['now'] = '2026-10-23T10:00:00+08:00'
        return result
    monkeypatch.setattr(client, 'tool', corrupt)
    with pytest.raises(client.ToolsError, match='回执不兼容'): client.check()
    assert client.load_state()['state'] == {} and client.load_state()['pending'] is not None


def test_unavailable_quotes_clear_successful_request_without_marking_notified(client, monkeypatch):
    call, _ = transport(client, monkeypatch)
    def unavailable(name, args):
        result = call(name, args)
        if name == 'policy_stop_alert':
            result['input']['quotes'] = {}
            result['result'] = stop_evaluation.evaluate(result['input'], result['input']['risk_policy'])
        return result
    monkeypatch.setattr(client, 'tool', unavailable)
    out, err, status = client.check()
    assert out == '' and '行情无效或过期' in err and status == 1
    assert client.load_state() == dict(schema_version=1, state={}, pending=None)


def test_corrupt_state_and_concurrency_never_acquire(client, monkeypatch):
    monkeypatch.setattr(client, 'tool', lambda *args: pytest.fail('no transport'))
    with client.STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert client.check() == ('', '', 0)
    client.STATE.write_text('{')
    with pytest.raises(ValueError): client.check()
    assert client.STATE.read_text() == '{'


def test_failed_state_commit_does_not_print_or_lose_pending(client, monkeypatch, capsys):
    transport(client, monkeypatch)
    save = client.save_json
    def broken(path, value):
        if value['pending'] is None: raise OSError('private-path-must-not-echo')
        save(path, value)
    monkeypatch.setattr(client, 'save_json', broken)
    monkeypatch.setattr(client.sys, 'argv', ['a-share-stop-alert.py'])
    assert client.main() == 1
    output = capsys.readouterr()
    assert output.out == '' and 'private-path' not in output.err
    assert client.load_state()['pending'] is not None


def test_certain_activation_change_preserves_levels_and_uses_durable_replacement(client, monkeypatch):
    original = dict(request_id='before-release', expected_activation_id=3,
                    previous_state=dict(date='2026-11-04', levels={'600519': 'near'}))
    client.STATE.write_text(json.dumps(dict(schema_version=1, state=original['previous_state'], pending=original)))
    calls = []
    def tool(name, args):
        calls.append((name, deepcopy(args)))
        if name == 'policy_current': return dict(installed=True, activation={'id': 4})
        assert client.load_state()['pending'] == args
        assert args['previous_state'] == original['previous_state']
        if args['request_id'] == original['request_id']:
            raise client.ToolsError('certain', code='policy_activation_changed')
        return dict(mode='service_stop_alert_capture', request_id=args['request_id'], activation_id=4,
                    input={'previous_state': args['previous_state'], 'now': client.datetime.current.isoformat()},
                    result=dict(status='no_alerts', exit_code=0, stdout='', stderr='', state=args['previous_state'], alerts=[]))
    monkeypatch.setattr(client, 'tool', tool)
    assert client.check() == ('', '', 0)
    assert len(calls) == 3 and calls[-1][1]['expected_activation_id'] == 4
    assert client.load_state()['state'] == original['previous_state']
    assert len(list(client.STATE.parent.glob('*.rejected-*.json'))) == 1


def test_release_pause_is_silent_within_grace_then_reuses_pending(client, monkeypatch):
    call, calls = transport(client, monkeypatch)
    def paused(name, args):
        if name == 'policy_stop_alert':
            raise client.ToolsError('paused', code='service_unavailable')
        return call(name, args)
    monkeypatch.setattr(client, 'tool', paused)
    assert client.check() == ('', '', 0)
    pending = client.load_state()['pending']
    client.datetime.current += timedelta(minutes=5)
    assert client.check() == ('', '', 0)
    assert client.load_state()['pending'] == pending
    def resumed(name, args):
        assert name == 'policy_stop_alert' and args == pending
        return call(name, args)
    monkeypatch.setattr(client, 'tool', resumed)
    client.datetime.current += timedelta(minutes=4)
    assert '第一档' in client.check()[0]
    assert not client.STATE.with_name('state.json.unavailable-since').exists()


def test_unavailable_beyond_grace_is_reported_and_keeps_pending(client, monkeypatch):
    call, _ = transport(client, monkeypatch)
    def paused(name, args):
        if name == 'policy_stop_alert':
            raise client.ToolsError('paused', code='service_unavailable')
        return call(name, args)
    monkeypatch.setattr(client, 'tool', paused)
    assert client.check() == ('', '', 0)
    pending = client.load_state()['pending']
    client.datetime.current += timedelta(minutes=10)
    with pytest.raises(client.ToolsError):
        client.check()
    assert client.load_state()['pending'] == pending and client.load_state()['state'] == {}
    def other(name, args):
        raise client.ToolsError('policy missing')
    monkeypatch.setattr(client, 'tool', other)
    with pytest.raises(client.ToolsError):
        client.check()
    assert not client.STATE.with_name('state.json.unavailable-since').exists()
    assert client.load_state()['pending'] == pending
    # After a non-unavailable failure, a later release pause gets a fresh grace window.
    monkeypatch.setattr(client, 'tool', paused)
    assert client.check() == ('', '', 0)
    assert client.STATE.with_name('state.json.unavailable-since').exists()


@pytest.mark.parametrize('payload', [
    '{}',
    'not-json',
    '[]',
    json.dumps({'since': '2026-10-22T10:31:05'}),  # naive timestamp without timezone
])
def test_corrupt_unavailable_since_fails_safely_and_keeps_pending(client, monkeypatch, capsys, payload):
    call, _ = transport(client, monkeypatch)
    def paused(name, args):
        if name == 'policy_stop_alert':
            raise client.ToolsError('paused', code='service_unavailable')
        return call(name, args)
    monkeypatch.setattr(client, 'tool', paused)
    assert client.check() == ('', '', 0)
    pending = client.load_state()['pending']
    client.STATE.with_name('state.json.unavailable-since').write_text(payload)
    monkeypatch.setattr(client.sys, 'argv', ['a-share-stop-alert.py'])
    assert client.main() == 1
    assert '检查未完成' in capsys.readouterr().err
    assert client.load_state()['pending'] == pending
