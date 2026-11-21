"""Durable scan requests across Hermes CLI invocations; no live transport."""
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import pytest
from support.hermes import RECEIPT, load, response


@pytest.fixture
def advisor(tmp_path, ashare_cli, monkeypatch):
    module = load('a-share-advisor')
    monkeypatch.setattr(module, 'SCAN_STATE', tmp_path / 'scan.json')
    monkeypatch.setattr(module, 'OPPORTUNITY_STATE', tmp_path / 'opportunities.json')
    ashare_cli.reply('research_scan_request', RECEIPT)
    ashare_cli.reply('research_scan_read', response())
    ashare_cli.reply('policy_current', dict(installed=True, activation={'id': 3}))
    ashare_cli.reply('policy_monitor', dict(mode='service_monitor_capture', request_id='$request_id',
            activation_id='$expected_activation_id', result={'review_prompts': []},
            capture_context={'capture_finished_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}))
    return module


@pytest.mark.parametrize(('status', 'code', 'notice'), [
    (20, 'policy_activation_changed', '政策异常'), (21, 'service_unavailable', '发布暂停'),
    (22, 'outcome_unknown', '原身份与原请求'), (1, 'failed', '凭据'),
])
def test_recover_same_request_without_syncing_subscriptions(advisor, ashare_cli, status, code, notice):
    ashare_cli.reply('research_scan_request', code=status, stderr='private diagnostic')
    with pytest.raises(advisor.AdvisorError, match=notice) as caught:
        advisor.scan()
    assert caught.value.code == code and 'private' not in str(caught.value)
    pending = advisor.read_scan_state()['pending']
    assert pending['scan_id'] is None and advisor.SCAN_STATE.stat().st_mode & 0o777 == 0o600
    args = dict(request_id=pending['request_id'])
    assert ashare_cli.calls == [('research_scan_request', args)]
    ashare_cli.reply('research_scan_request', RECEIPT)
    assert advisor.scan() == ''
    assert advisor.read_scan_state()['pending'] is None
    assert ashare_cli.calls[:3] == [('research_scan_request', args)] * 2 + [
        ('research_scan_read', {'scan_id': 7, 'compact': False})]
    assert [name for name, _ in ashare_cli.calls[3:]] == ['policy_current', 'policy_monitor']


def test_soft_deadline_retains_scan_id_for_next_invocation(advisor, ashare_cli):
    assert '下次继续查询' in advisor.scan(time.monotonic() + .8)
    pending = advisor.read_scan_state()['pending']
    assert pending['scan_id'] == 7
    assert ashare_cli.calls == [('research_scan_request', {'request_id': pending['request_id']})]
    assert advisor.scan() == '' and advisor.read_scan_state()['pending'] is None


@pytest.mark.parametrize('field,value', [('version', 'old'), ('request_id', 'another'),
                                         ('scan_id', True), ('scan_id', 0), ('id', 8)])
def test_bad_submit_receipt_never_clears_pending(advisor, ashare_cli, field, value):
    ashare_cli.reply('research_scan_request', RECEIPT | {field: value})
    with pytest.raises(advisor.AdvisorError, match='提交回执'):
        advisor.scan()
    assert advisor.read_scan_state()['pending']['scan_id'] is None


@pytest.mark.parametrize('mutation', [{'id': 8}, {'id': True}, {'version': 'old'}, {'status': 'unknown'},
                                      {'summary': []}, {'notify': [1]}, {'requested_at': '2026-11-11'},
                                      {'requested_at': (datetime.now(ZoneInfo('Asia/Shanghai'))+timedelta(days=1)).isoformat()}])
def test_bad_poll_receipt_retains_known_scan_id(advisor, ashare_cli, mutation):
    ashare_cli.reply('research_scan_read', response(**mutation))
    with pytest.raises(advisor.AdvisorError, match='查询回执'):
        advisor.scan()
    assert advisor.read_scan_state()['pending']['scan_id'] == 7


def test_server_cannot_reassign_an_already_saved_scan(advisor, ashare_cli):
    advisor.scan(time.monotonic() + .8)
    before = advisor.SCAN_STATE.read_bytes()
    ashare_cli.reply('research_scan_request', RECEIPT | {'id': 8, 'scan_id': 8})
    with pytest.raises(advisor.AdvisorError, match='提交回执'):
        advisor.scan()
    assert advisor.SCAN_STATE.read_bytes() == before


@pytest.mark.parametrize('field,value', [('token', 'rotated'), ('url', 'https://other.test/mcp')])
def test_binding_change_stops_before_cli(advisor, ashare_cli, field, value):
    ashare_cli.reply('research_scan_request', code=22)
    with pytest.raises(advisor.AdvisorError, match='结果未知'):
        advisor.scan()
    before = advisor.SCAN_STATE.read_bytes()
    config = Path(advisor.os.environ['ASHARE_HERMES_CONFIG'])
    config.write_text(json.dumps(json.loads(config.read_text()) | {field: value}))
    with pytest.raises(advisor.AdvisorError, match='凭据已变化'):
        advisor.scan()
    assert len(ashare_cli.calls) == 1 and advisor.SCAN_STATE.read_bytes() == before


def test_legacy_pending_stops_and_legacy_empty_can_start(advisor, ashare_cli):
    pending = dict(request_id='hermes-scan-' + 'a' * 32, binding='b' * 64,
                   created_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), scan_id=None)
    advisor.save_json(advisor.SCAN_STATE, dict(schema_version=1, pending=pending))
    before = advisor.SCAN_STATE.read_bytes()
    with pytest.raises(advisor.AdvisorError, match='旧 research key.*原凭据.*pending 为空后再换发'):
        advisor.scan()
    assert advisor.SCAN_STATE.read_bytes() == before and ashare_cli.calls == []
    advisor.save_json(advisor.SCAN_STATE, dict(schema_version=1, pending=None))
    assert advisor.scan() == ''
    assert advisor.read_scan_state() == dict(schema_version=1, pending=None)


def test_old_scan_is_labelled_and_refreshes_current_holdings(advisor, ashare_cli):
    ashare_cli.reply('research_scan_read', response(
        requested_at=(datetime.now(ZoneInfo('Asia/Shanghai')) - timedelta(days=1)).isoformat(),
        notify=[dict(code='600519', reason='pending_review')]))
    ashare_cli.reply('account_current', dict(codes=[], source={'mode': 'ledger'}))
    output = advisor.scan()
    assert '历史回执，不代表今日观察' in output and '当前未持仓' in output
    assert [name for name, _ in ashare_cli.calls] == [
        'research_scan_request', 'research_scan_read', 'account_current', 'policy_current', 'policy_monitor']


def test_interrupted_opportunity_phase_resumes_original_scan(advisor, ashare_cli):
    lock = advisor.OPPORTUNITY_STATE.with_suffix('.lock')
    lock.mkdir()  # Local state is unavailable after the terminal scan receipt.
    with pytest.raises(OSError):
        advisor.scan()
    pending = advisor.read_scan_state()['pending']
    assert pending['scan_id'] == 7
    lock.rmdir()
    assert advisor.scan() == ''
    assert [args for name, args in ashare_cli.calls if name == 'research_scan_request'] == [
        {'request_id': pending['request_id']}] * 2


def test_malformed_notification_cannot_finish_pending_scan(advisor, ashare_cli):
    ashare_cli.reply('research_scan_read', response(notify=[{}]))
    with pytest.raises(KeyError):
        advisor.scan()
    pending = advisor.read_scan_state()['pending']
    assert pending['scan_id'] == 7
    ashare_cli.reply('research_scan_read', response())
    assert advisor.scan() == ''
    assert [args for name, args in ashare_cli.calls if name == 'research_scan_request'] == [
        {'request_id': pending['request_id']}] * 2


@pytest.mark.parametrize('raw', ['{', '{"schema_version":1,"schema_version":1,"pending":null}',
                                '{"schema_version":true,"pending":null}', '{"schema_version":1,"pending":{}}'])
def test_corrupt_state_is_not_overwritten_or_used(advisor, ashare_cli, raw):
    advisor.SCAN_STATE.write_text(raw)
    advisor.SCAN_STATE.chmod(0o600)
    with pytest.raises(advisor.AdvisorError):
        advisor.scan()
    assert advisor.SCAN_STATE.read_text() == raw and ashare_cli.calls == []


def test_concurrent_scan_is_silent(advisor, ashare_cli):
    with advisor.SCAN_STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert advisor.scan() == '' and ashare_cli.calls == []


def test_expired_budget_does_not_start_transport(advisor, ashare_cli):
    with pytest.raises(advisor.AdvisorError, match='时限已到'):
        advisor.scan(time.monotonic()-1)
    assert not advisor.SCAN_STATE.exists() and ashare_cli.calls == []


def test_pending_timestamp_and_file_permissions_are_checked(advisor):
    advisor.scan(time.monotonic()+.8)
    state = advisor.read_scan_state()
    state['pending']['created_at'] = (datetime.now(ZoneInfo('Asia/Shanghai'))+timedelta(days=1)).isoformat()
    advisor.save_json(advisor.SCAN_STATE, state)
    before = advisor.SCAN_STATE.read_bytes()
    with pytest.raises(advisor.AdvisorError):
        advisor.scan()
    assert advisor.SCAN_STATE.read_bytes() == before
    state['pending']['created_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    advisor.save_json(advisor.SCAN_STATE, state)
    advisor.SCAN_STATE.chmod(0o644)
    with pytest.raises(advisor.AdvisorError):
        advisor.scan()
    assert advisor.SCAN_STATE.stat().st_mode & 0o777 == 0o644


def test_main_never_prints_transport_credentials(advisor, ashare_cli, monkeypatch, capsys):
    monkeypatch.setattr(advisor.sys, 'argv', ['a-share-advisor.py', 'scan'])
    ashare_cli.reply('research_scan_request', code=1, stderr='private token at private URL')
    advisor.main()
    output = capsys.readouterr()
    assert '凭据' in output.out and 'private' not in output.out and output.err == ''


def test_weekly_is_also_inside_the_shared_budget(advisor, monkeypatch, capsys):
    monkeypatch.setattr(advisor.sys, 'argv', ['a-share-advisor.py', 'weekly'])
    def bounded(callback, seconds):
        assert seconds == 540
        return callback(123)
    monkeypatch.setattr(advisor, 'run_bounded', bounded)
    monkeypatch.setattr(advisor, 'weekly', lambda deadline: 'weekly' if deadline == 123 else 'wrong')
    advisor.main()
    assert capsys.readouterr().out == 'weekly\n'
