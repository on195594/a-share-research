"""Transport contracts using structural fixtures, NOT live financial evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl

import pytest

from a_share_research.policy import benchmark_evaluation
from test_benchmark_evaluation import GOLDEN, bundle
from test_ops_hermes import load


@pytest.fixture
def source():
    return bundle()


@pytest.fixture
def client(tmp_path, monkeypatch):
    module = load('a-share-benchmark')
    monkeypatch.setattr(module, 'STATE', tmp_path / 'benchmark-state.json')
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
        assert name == 'policy.benchmark' and client.load_state()['pending'] == args
        value = deepcopy(source)
        return dict(mode='service_benchmark_capture', request_id=args['request_id'], activation_id=7,
                    input=value, result=benchmark_evaluation.evaluate(value, value['risk_policy']))
    monkeypatch.setattr(client, 'tool', call)
    return call, calls


def test_ready_prefix_and_warning_and_no_monthly_dedup(client, source, monkeypatch):
    source['prices']['600900'].update(status='degraded', source_as_of='2026-10-20', freshness_days=1,
                                    fallback_reason='no-close', source='cache')
    _, calls = transport(client, monkeypatch, source)
    out, err, status = client.check()
    assert out == '📊 A股账本月度对比\n' + GOLDEN and status == 0
    assert '价格参考' in err and client.load_state()['pending'] is None
    client.check()
    assert len(calls) == 4 and calls[1][1] != calls[3][1]
    assert calls[1][1]['end'] == '2026-10-22'


@pytest.mark.parametrize('failure', ['no_events', 'unavailable'])
def test_failure_is_explicit_without_report_prefix(client, source, monkeypatch, failure):
    if failure == 'no_events': source.update(events=[], indices={}, prices={})
    else:
        source['indices']['H00300.CSI'].update(rows=None, status='failed', error_code='TIMEOUT')
        source['prices'] = {}
    transport(client, monkeypatch, source)
    out, err, status = client.check()
    assert status == 1 and out == '' and err and '📊' not in err
    assert client.load_state()['pending'] is None


def test_response_loss_retries_original_request_and_cross_day_resolution(client, source, monkeypatch):
    call, calls = transport(client, monkeypatch, source)
    receipt = {}
    def lost(name, args):
        value = call(name, args)
        if name == 'policy.benchmark':
            receipt.update(value)
            raise client.ToolsError('lost response')
        return value
    monkeypatch.setattr(client, 'tool', lost)
    with pytest.raises(client.ToolsError): client.check()
    pending = client.load_state()['pending']
    def recover(name, args):
        assert name == 'policy.benchmark' and args == pending
        return receipt
    monkeypatch.setattr(client, 'tool', recover)
    assert client.check()[0] == '📊 A股账本月度对比\n' + GOLDEN
    client.save_json(client.STATE, {'schema_version': 1, 'pending': pending})
    client.datetime.current += timedelta(days=1)
    out, err, status = client.check()
    assert status == 1 and out == '' and '其他日期' in err
    assert len(calls) == 2 and client.load_state()['pending'] is None


@pytest.mark.parametrize('variant', ['mode', 'activation', 'bool_activation', 'request', 'result',
                                    'future', 'naive', 'end', 'exit', 'stderr', 'metrics', 'failures'])
def test_bad_receipt_keeps_pending(client, source, monkeypatch, variant):
    call, _ = transport(client, monkeypatch, source)
    def corrupt(name, args):
        value = call(name, args)
        if name == 'policy.benchmark':
            if variant == 'mode': value['mode'] = 'other'
            if variant == 'activation': value['activation_id'] = 9
            if variant == 'bool_activation': value['activation_id'] = True
            if variant == 'request': value['request_id'] = 'wrong'
            if variant == 'result': value['result']['extra'] = 1
            if variant == 'future': value['input']['now'] = '2099-01-01T00:00:00+08:00'
            if variant == 'naive': value['input']['now'] = '2026-10-22T00:00:00'
            if variant == 'end': value['input']['end'] = '2026-10-21'
            if variant == 'exit': value['result']['exit_code'] = True
            if variant == 'stderr': value['result']['stderr'] = None
            if variant == 'metrics': value['result']['metrics'] = []
            if variant == 'failures': value['result']['failures'] = {}
        return value
    monkeypatch.setattr(client, 'tool', corrupt)
    with pytest.raises(client.ToolsError, match='回执不兼容'): client.check()
    assert client.load_state()['pending'] is not None


@pytest.mark.parametrize('raw', ['{', '[]', '{"schema_version":true,"pending":null}',
    '{"schema_version":1,"pending":{"request_id":"x","expected_activation_id":1,"end":"2026-02-30"}}'])
def test_lock_and_bad_state_never_call_service(client, monkeypatch, raw):
    monkeypatch.setattr(client, 'tool', lambda *args: pytest.fail('no service call'))
    with client.STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert client.check() == ('', '', 0)
    client.STATE.write_text(raw)
    with pytest.raises((ValueError, client.ToolsError)): client.check()
    assert client.STATE.read_text() == raw


@pytest.mark.parametrize('before_transport', [True, False])
def test_state_write_failure_prevents_delivery(client, source, monkeypatch, capsys, before_transport):
    _, calls = transport(client, monkeypatch, source)
    save = client.save_json
    def broken(path, value):
        if (value['pending'] is not None) == before_transport: raise OSError('private-file')
        return save(path, value)
    monkeypatch.setattr(client, 'save_json', broken)
    monkeypatch.setattr(client.sys, 'argv', ['a-share-benchmark.py'])
    assert client.main() == 1
    captured = capsys.readouterr()
    assert captured.out == '' and 'private-file' not in captured.err
    assert len(calls) == (1 if before_transport else 2)
    if not before_transport: assert client.load_state()['pending'] is not None
