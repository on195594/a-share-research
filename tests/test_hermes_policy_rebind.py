"""Only a bounded, explicit non-write rejection can retire a pending request."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'ops/hermes'))
import ashare_tools as helper


@pytest.mark.parametrize('kind', ['stop', 'daily', 'opportunity'])
@pytest.mark.parametrize('outcome', ['success', 'second-change', 'paused', 'timeout', 'generic-409', 'bad-response'])
def test_rebind_conserves_state_and_new_uuid_before_collection(tmp_path, ashare_cli, kind, outcome):
    pending = dict(request_id='frozen-old-request', expected_activation_id=3)
    saved = dict(schema_version=1, pending=pending, binding=helper.client_identity()[0])
    if kind == 'stop':
        saved['state'] = {'date': '2026-07-08', 'levels': {'600519': 'stop1'}}
        pending['previous_state'] = deepcopy(saved['state'])
    if kind == 'opportunity': saved['seen'] = ['old-opportunity-key']
    path = tmp_path / 'state.json'
    helper.save_json(path, saved)
    original = deepcopy(saved)
    calls = []
    def call(name, args):
        calls.append((name, deepcopy(args)))
        assert json.loads(path.read_text()) == saved
        if len(calls) == 1:
            if outcome in ('paused', 'timeout', 'generic-409', 'bad-response'):
                raise helper.ToolsError('uncertain, never parse this text')
            raise helper.ToolsError('safe certain rejection', code='policy_activation_changed')
        if name == 'policy_current': return dict(installed=True, activation={'id': 4})
        assert args['request_id'] != original['pending']['request_id']
        assert args['expected_activation_id'] == 4
        if kind == 'stop': assert args['previous_state'] == original['state']
        if outcome == 'second-change':
            raise helper.ToolsError('safe certain rejection', code='policy_activation_changed')
        return {'receipt': 'new'}
    name = {'stop': 'policy_stop_alert', 'daily': 'policy_daily_brief', 'opportunity': 'policy_monitor'}[kind]
    if outcome == 'success':
        assert helper.policy_receipt(name, saved, path, call=call) == {'receipt': 'new'}
    else:
        with pytest.raises(helper.ToolsError): helper.policy_receipt(name, saved, path, call=call)
    if outcome in ('success', 'second-change'):
        assert len(calls) == 3 and saved['pending']['expected_activation_id'] == 4
        archive, = list(tmp_path.glob('*.rejected-*.json'))
        assert json.loads(archive.read_text()) == dict(reason='policy_activation_changed', request=original['pending'])
        assert saved == original | {'pending': calls[-1][1]}
    else:
        assert len(calls) == 1 and saved == original
        assert list(tmp_path.glob('*.rejected-*.json')) == []


@pytest.mark.parametrize('current', [dict(installed=False, activation={'id': 4}),
    dict(installed=True, activation={'id': 3}), dict(installed=True, activation=None)])
def test_no_valid_new_pointer_keeps_original(tmp_path, ashare_cli, current):
    path = tmp_path/'state.json'
    saved = dict(binding=helper.client_identity()[0], pending=dict(request_id='old', expected_activation_id=3), state={'levels': {}})
    helper.save_json(path, saved)
    def call(name, args):
        if name == 'policy_current': return current
        raise helper.ToolsError('certain', code='policy_activation_changed')
    with pytest.raises(helper.ToolsError): helper.policy_receipt('policy_stop_alert', saved, path, call=call)
    assert json.loads(path.read_text()) == saved and saved['pending']['request_id'] == 'old'


@pytest.mark.parametrize(('status', 'code'), [
    (1, 'failed'), (20, 'policy_activation_changed'), (21, 'service_unavailable'),
    (22, 'outcome_unknown'), (127, 'failed'),
])
def test_cli_protocol_does_not_parse_chinese_or_leak_raw_errors(ashare_cli, status, code):
    ashare_cli.reply('policy_monitor', code=status, stderr='policy_activation_changed 生效政策已变化 raw-owner-secret')
    with pytest.raises(helper.ToolsError) as caught:
        helper.tool('policy_monitor', {})
    assert caught.value.code == code
    assert 'raw-owner-secret' not in str(caught.value)


@pytest.mark.parametrize(('script', 'name'), [
    ('a-share-stop-alert', 'policy_stop_alert'), ('a-share-daily-brief', 'policy_daily_brief'),
    ('a-share-benchmark', 'policy_benchmark'),
])
def test_unknown_result_requires_original_identity_and_arguments(
        ashare_cli, tmp_path, monkeypatch, capsys, script, name):
    from support.hermes import load

    job = load(script)
    monkeypatch.setattr(job, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(sys, 'argv', [script])
    # A completed record's stale binding must never be inherited by a new request.
    stale = job.load_state() | {'binding': 'retired'}
    helper.save_json(job.STATE, stale)
    writes = []
    def save(path, value):
        writes.append(deepcopy(value))
        helper.save_json(path, value)
    monkeypatch.setattr(job, 'save_json', save)
    ashare_cli.reply('policy_current', {'installed': True, 'activation': {'id': 7}})
    ashare_cli.reply(name, code=22)
    assert job.main() == 1 and '结果未知' in capsys.readouterr().err
    original = job.load_state()
    assert writes == [original] and original['binding'] == helper.client_identity()[0]
    assert ashare_cli.calls == [('policy_current', {}), (name, original['pending'])]

    config = helper.config_path()
    first_identity = config.read_text()
    config.write_text(json.dumps(json.loads(first_identity) | {'token': 'replacement-test-grant'}))
    assert job.main() == 1 and '原身份' in capsys.readouterr().err
    assert len(ashare_cli.calls) == 2 and job.load_state() == original
    config.write_text(first_identity)
    assert job.main() == 1 and '结果未知' in capsys.readouterr().err
    assert ashare_cli.calls[2:] == [(name, original['pending'])] and job.load_state() == original

    legacy = dict(original)
    del legacy['binding']
    helper.save_json(job.STATE, legacy)
    before = job.STATE.read_bytes()
    assert job.main() == 1 and '原 assistant 身份与旧脚本查回' in capsys.readouterr().err
    assert len(ashare_cli.calls) == 3 and job.STATE.read_bytes() == before
