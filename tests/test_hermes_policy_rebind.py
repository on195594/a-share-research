"""Only a bounded, explicit non-write rejection can retire a pending request."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'ops/hermes'))
import ashare_tools as helper


@pytest.mark.parametrize('kind', ['stop', 'daily', 'opportunity'])
@pytest.mark.parametrize('outcome', ['success', 'second-change', 'paused', 'timeout', 'generic-409', 'bad-response'])
def test_rebind_conserves_state_and_new_uuid_before_collection(tmp_path, kind, outcome):
    pending = dict(request_id='frozen-old-request', expected_activation_id=3)
    saved = dict(schema_version=1, pending=pending)
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
    if outcome == 'success':
        assert helper.policy_receipt('policy.' + kind, saved, path, call=call) == {'receipt': 'new'}
    else:
        with pytest.raises(helper.ToolsError): helper.policy_receipt('policy.' + kind, saved, path, call=call)
    if outcome in ('success', 'second-change'):
        assert len(calls) == 3 and saved['pending']['expected_activation_id'] == 4
        archive, = list(tmp_path.glob('*.rejected-*.json'))
        assert json.loads(archive.read_text()) == dict(reason='policy_activation_changed', request=original['pending'])
        assert {k:v for k,v in saved.items() if k != 'pending'} == {k:v for k,v in original.items() if k != 'pending'}
    else:
        assert len(calls) == 1 and saved == original
        assert list(tmp_path.glob('*.rejected-*.json')) == []


@pytest.mark.parametrize('current', [dict(installed=False, activation={'id': 4}),
    dict(installed=True, activation={'id': 3}), dict(installed=True, activation=None)])
def test_no_valid_new_pointer_keeps_original(tmp_path, current):
    path = tmp_path/'state.json'
    saved = dict(pending=dict(request_id='old', expected_activation_id=3), state={'levels': {}})
    helper.save_json(path, saved)
    def call(name, args):
        if name == 'policy_current': return current
        raise helper.ToolsError('certain', code='policy_activation_changed')
    with pytest.raises(helper.ToolsError): helper.policy_receipt('policy_stop_alert', saved, path, call=call)
    assert json.loads(path.read_text()) == saved and saved['pending']['request_id'] == 'old'


@pytest.mark.parametrize('status', [1, 20, 127])
def test_cli_protocol_does_not_parse_chinese_or_leak_raw_errors(monkeypatch, status):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=status,
        stdout='raw-owner-secret', stderr='policy_activation_changed 生效政策已变化 raw-owner-secret'))
    with pytest.raises(helper.ToolsError) as caught: helper.tool('policy_monitor', {})
    assert caught.value.code == ('policy_activation_changed' if status == 20 else None)
    assert 'raw-owner-secret' not in str(caught.value)
