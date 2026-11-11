"""Daily client retry/presentation contracts over an explicit historical-boundary input."""
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl

import pytest

from a_share_research.policy import daily_evaluation
from test_daily_evaluation import bundle as daily_bundle
from test_ops_hermes import load


@pytest.fixture
def source(tmp_path, monkeypatch):
    return daily_bundle.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def client(tmp_path, monkeypatch):
    module = load('a-share-daily-brief')
    monkeypatch.setattr(module, 'STATE', tmp_path / 'daily-state.json')
    class Clock(datetime):
        current = datetime.fromisoformat('2026-10-22T15:35:00+08:00')
        @classmethod
        def now(cls, tz=None): return cls.current.astimezone(tz)
    monkeypatch.setattr(module, 'datetime', Clock)
    return module


def transport(client, monkeypatch, source):
    calls = []
    def call(name, args):
        calls.append((name, deepcopy(args)))
        if name == 'policy.current': return dict(installed=True, activation={'id': 7})
        assert name == 'policy.daily_brief' and client.load_state()['pending'] == args
        value = deepcopy(source)
        return dict(mode='service_daily_brief_capture', request_id=args['request_id'], activation_id=7,
                    input=value, result=daily_evaluation.evaluate(value, value['risk_policy']))
    monkeypatch.setattr(client, 'tool', call)
    return call, calls


def test_brief_preserves_dividend_findings_and_provider_failures(client, source, monkeypatch):
    transport(client, monkeypatch, source)
    out, err, status = client.check()
    assert status == 0 and err == '' and '应收280.00元' in out
    assert '通过 a-share 核对券商流水' in out and 'dividend-check' not in out
    assert client.load_state()['pending'] is None
    source['dividends'][0]['provider'].update(rows=None, error_code='TIMEOUT', status='failed')
    out, err, status = client.check()
    assert '分红未核对：数据获取失败' in out and '应收280.00元' not in out


def test_response_loss_keeps_same_request_without_second_policy_lookup(client, source, monkeypatch):
    call, calls = transport(client, monkeypatch, source)
    receipt = {}
    def lost(name, args):
        value = call(name, args)
        if name == 'policy.daily_brief':
            receipt.update(value)
            raise client.ToolsError('lost response')
        return value
    monkeypatch.setattr(client, 'tool', lost)
    with pytest.raises(client.ToolsError): client.check()
    pending = client.load_state()['pending']
    def recover(name, args):
        assert name == 'policy.daily_brief' and args == pending
        return receipt
    monkeypatch.setattr(client, 'tool', recover)
    assert '应收280.00元' in client.check()[0]
    assert len(calls) == 2 and client.load_state()['pending'] is None


def test_old_day_is_resolved_without_current_day_delivery(client, source, monkeypatch):
    transport(client, monkeypatch, source)
    client.datetime.current += timedelta(days=1)
    out, err, status = client.check()
    assert status == 1 and out == '' and '其他日期' in err
    assert client.load_state()['pending'] is None


@pytest.mark.parametrize('variant', ['mode', 'activation', 'request', 'result', 'future', 'day'])
def test_bad_receipt_cannot_clear_pending(client, source, monkeypatch, variant):
    call, _ = transport(client, monkeypatch, source)
    def corrupt(name, args):
        value = call(name, args)
        if name == 'policy.daily_brief':
            if variant == 'mode': value['mode'] = 'service_monitor_capture'
            if variant == 'activation': value['activation_id'] = 9
            if variant == 'request': value['request_id'] = 'wrong'
            if variant == 'result': value['result'] = {}
            if variant == 'future': value['input']['now'] = '2099-01-01T00:00:00+08:00'
            if variant == 'day': value['input']['brief_today'] = '2026-02-30'
        return value
    monkeypatch.setattr(client, 'tool', corrupt)
    with pytest.raises(client.ToolsError, match='回执不兼容'): client.check()
    assert client.load_state()['pending'] is not None


def test_concurrent_or_broken_state_does_not_call_service(client, monkeypatch):
    monkeypatch.setattr(client, 'tool', lambda *args: pytest.fail('no service call'))
    with client.STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert client.check() == ('', '', 0)
    client.STATE.write_text('{')
    with pytest.raises(ValueError): client.check()
    assert client.STATE.read_text() == '{'


def test_state_write_failure_does_not_send_brief(client, source, monkeypatch, capsys):
    transport(client, monkeypatch, source)
    save = client.save_json
    def broken(path, value):
        if value['pending'] is None: raise OSError('private-file')
        return save(path, value)
    monkeypatch.setattr(client, 'save_json', broken)
    monkeypatch.setattr(client.sys, 'argv', ['a-share-daily-brief.py'])
    assert client.main() == 1
    captured = capsys.readouterr()
    assert captured.out == '' and 'private-file' not in captured.err
    assert client.load_state()['pending'] is not None
