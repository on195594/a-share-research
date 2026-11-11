"""Structural request/transport states; no invented financial or investment evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import time
import urllib.error
from zoneinfo import ZoneInfo

import pytest

from test_ops_hermes import load


@pytest.fixture
def advisor(tmp_path, monkeypatch):
    module = load('a-share-advisor')
    monkeypatch.setattr(module, 'SCAN_STATE', tmp_path / 'scan.json')
    monkeypatch.setattr(module, 'OPPORTUNITY_STATE', tmp_path / 'opportunities.json')
    monkeypatch.setattr(module, 'settings', lambda: ('http://127.0.0.1:8620', 'structural-token', 'stock.test'))
    monkeypatch.setattr(module, 'holdings', lambda: [])
    monkeypatch.setattr(module, 'opportunities', lambda base: '')
    return module


def response(scan_id=7, status='done'):
    return dict(version='research-v1', id=scan_id, status=status,
                requested_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), summary={}, notify=[])


def transport(advisor, monkeypatch):
    calls, mapping = [], {}
    result = response()
    def call(method, path, body=None, **options):
        calls.append((method, path, deepcopy(body)))
        assert options['connection'] == advisor.settings()
        assert options['deadline'] > time.monotonic()
        if path == '/api/subscriptions':
            assert method == 'PUT' and body == {'codes': []}
            return {}
        if path.startswith('/api/scan-requests/'):
            assert method == 'PUT'
            key = path.rsplit('/', 1)[-1]
            saved = advisor.read_scan_state()['pending']
            assert saved['request_id'] == key
            assert saved['binding'] == advisor.service_binding(options['connection'])
            assert advisor.SCAN_STATE.stat().st_mode & 0o777 == 0o600
            mapping.setdefault(key, 7)
            return dict(version='research-v1', request_id=key, scan_id=mapping[key])
        assert method == 'GET' and path == '/api/scans/7'
        return deepcopy(result)
    monkeypatch.setattr(advisor, 'call', call)
    return call, calls, mapping, result


def test_lost_submit_response_recovers_same_terminal_scan_without_resubscribing(advisor, monkeypatch):
    call, calls, mapping, result = transport(advisor, monkeypatch)
    def lose(*args, **kwargs):
        value = call(*args, **kwargs)
        if args[1].startswith('/api/scan-requests/'):
            raise urllib.error.URLError('structural lost response')
        return value
    monkeypatch.setattr(advisor, 'call', lose)
    with pytest.raises(urllib.error.URLError):
        advisor.scan()
    pending = deepcopy(advisor.read_scan_state()['pending'])
    assert pending['scan_id'] is None and len(mapping) == 1
    result['status'] = 'done'  # Server finished before the next client invocation.
    monkeypatch.setattr(advisor, 'call', call)
    assert advisor.scan() == ''
    assert advisor.read_scan_state()['pending'] is None
    submissions = [path for _, path, _ in calls if path.startswith('/api/scan-requests/')]
    assert submissions == ['/api/scan-requests/' + pending['request_id']] * 2
    assert sum(path == '/api/subscriptions' for _, path, _ in calls) == 1
    assert all(method != 'POST' for method, _, _ in calls)


def test_soft_deadline_retains_scan_id_for_next_invocation(advisor, monkeypatch):
    _, calls, mapping, result = transport(advisor, monkeypatch)
    assert '下次继续查询' in advisor.scan(time.monotonic() + .5)
    assert advisor.read_scan_state()['pending']['scan_id'] == 7
    assert not any(method == 'GET' for method, _, _ in calls)
    assert advisor.scan() == '' and len(mapping) == 1
    assert advisor.read_scan_state()['pending'] is None


@pytest.mark.parametrize('field,value', [('version', 'old'), ('request_id', 'another'),
                                         ('scan_id', True), ('scan_id', 0)])
def test_bad_submit_receipt_never_clears_pending(advisor, monkeypatch, field, value):
    call, _, _, _ = transport(advisor, monkeypatch)
    def bad(method, path, *args, **kwargs):
        result = call(method, path, *args, **kwargs)
        if path.startswith('/api/scan-requests/'):
            result[field] = value
        return result
    monkeypatch.setattr(advisor, 'call', bad)
    with pytest.raises(advisor.AdvisorError, match='提交回执'):
        advisor.scan()
    assert advisor.read_scan_state()['pending']['scan_id'] is None


@pytest.mark.parametrize('mutation', [{'id': 8}, {'id': True}, {'version': 'old'}, {'status': 'unknown'},
                                      {'summary': []}, {'notify': [1]}, {'requested_at': '2026-11-11'},
                                      {'requested_at': (datetime.now(ZoneInfo('Asia/Shanghai'))+timedelta(days=1)).isoformat()}])
def test_bad_poll_receipt_retains_known_scan_id(advisor, monkeypatch, mutation):
    _, _, _, result = transport(advisor, monkeypatch)
    result.update(mutation)
    with pytest.raises(advisor.AdvisorError, match='查询回执'):
        advisor.scan()
    assert advisor.read_scan_state()['pending']['scan_id'] == 7


def test_server_cannot_reassign_an_already_saved_scan(advisor, monkeypatch):
    call, _, _, _ = transport(advisor, monkeypatch)
    advisor.scan(time.monotonic() + .5)
    before = advisor.SCAN_STATE.read_bytes()
    def changed(method, path, *args, **kwargs):
        value = call(method, path, *args, **kwargs)
        if path.startswith('/api/scan-requests/'):
            value['scan_id'] = 8
        return value
    monkeypatch.setattr(advisor, 'call', changed)
    with pytest.raises(advisor.AdvisorError):
        advisor.scan()
    assert advisor.SCAN_STATE.read_bytes() == before


def test_old_service_404_keeps_request_and_never_falls_back(advisor, monkeypatch):
    call, calls, _, _ = transport(advisor, monkeypatch)
    def old(method, path, *args, **kwargs):
        if path.startswith('/api/scan-requests/'):
            raise urllib.error.HTTPError('structural-url', 404, 'not found', {}, None)
        return call(method, path, *args, **kwargs)
    monkeypatch.setattr(advisor, 'call', old)
    with pytest.raises(advisor.AdvisorError, match='未回落'):
        advisor.scan()
    assert advisor.read_scan_state()['pending']['scan_id'] is None
    assert all(method != 'POST' for method, _, _ in calls)


@pytest.mark.parametrize('settings', [('http://127.0.0.1:8620', 'rotated-token', 'stock.test'),
                                     ('http://127.0.0.1:9999', 'structural-token', 'stock.test'),
                                     ('http://127.0.0.1:8620', 'structural-token', 'other.test')])
def test_binding_change_refuses_before_network_or_holdings(advisor, monkeypatch, settings):
    transport(advisor, monkeypatch)
    advisor.scan(time.monotonic() + .5)
    before = advisor.SCAN_STATE.read_bytes()
    monkeypatch.setattr(advisor, 'settings', lambda: settings)
    monkeypatch.setattr(advisor, 'call', lambda *a, **k: pytest.fail('must not contact changed service'))
    monkeypatch.setattr(advisor, 'holdings', lambda: pytest.fail('must not resubscribe'))
    with pytest.raises(advisor.AdvisorError, match='凭据已变化'):
        advisor.scan()
    assert advisor.SCAN_STATE.read_bytes() == before


def test_old_scan_is_labelled_and_refreshes_current_holdings(advisor, monkeypatch):
    _, _, _, result = transport(advisor, monkeypatch)
    result['requested_at'] = (datetime.now(ZoneInfo('Asia/Shanghai'))-timedelta(days=1)).isoformat()
    result['notify'] = [{}]  # Explicit transport marker; rendering is isolated below.
    holdings_calls = []
    def holdings():
        holdings_calls.append(True)
        return []
    monkeypatch.setattr(advisor, 'holdings', holdings)
    monkeypatch.setattr(advisor, 'render_scan', lambda data, held, base: 'structural historical notice')
    assert '历史回执，不代表今日观察' in advisor.scan()
    assert len(holdings_calls) == 2
    assert advisor.read_scan_state()['pending'] is None


def test_render_or_opportunity_exception_keeps_original_request(advisor, monkeypatch):
    _, calls, _, _ = transport(advisor, monkeypatch)
    def interrupted(base):
        raise OSError('structural interruption')
    monkeypatch.setattr(advisor, 'opportunities', interrupted)
    with pytest.raises(OSError):
        advisor.scan()
    assert advisor.read_scan_state()['pending']['scan_id'] == 7
    monkeypatch.setattr(advisor, 'opportunities', lambda base: '')
    assert advisor.scan() == ''
    assert sum(path == '/api/subscriptions' for _, path, _ in calls) == 1


def test_malformed_notification_cannot_finish_pending_scan(advisor, monkeypatch):
    _, calls, _, result = transport(advisor, monkeypatch)
    result['notify'] = [{}]  # Deliberately missing the documented notice fields.
    with pytest.raises(KeyError):
        advisor.scan()
    assert advisor.read_scan_state()['pending']['scan_id'] == 7
    result['notify'] = []
    assert advisor.scan() == ''
    assert sum(path == '/api/subscriptions' for _, path, _ in calls) == 1


@pytest.mark.parametrize('raw', ['{', '{"schema_version":1,"schema_version":1,"pending":null}',
                                '{"schema_version":true,"pending":null}', '{"schema_version":1,"pending":{}}'])
def test_corrupt_state_is_not_overwritten_or_used(advisor, monkeypatch, raw):
    advisor.SCAN_STATE.write_text(raw)
    advisor.SCAN_STATE.chmod(0o600)
    monkeypatch.setattr(advisor, 'call', lambda *a, **k: pytest.fail('no network'))
    with pytest.raises(advisor.AdvisorError):
        advisor.scan()
    assert advisor.SCAN_STATE.read_text() == raw


def test_concurrent_scan_is_silent(advisor, monkeypatch):
    monkeypatch.setattr(advisor, 'settings', lambda: pytest.fail('busy invocation must be silent'))
    with advisor.SCAN_STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert advisor.scan() == ''


def test_expired_budget_does_not_start_transport_or_holdings(advisor, monkeypatch):
    monkeypatch.setattr(advisor, 'holdings', lambda: pytest.fail('expired budget must not start work'))
    monkeypatch.setattr(advisor, 'call', lambda *a, **k: pytest.fail('expired budget must not start work'))
    with pytest.raises(advisor.AdvisorError, match='时限已到'):
        advisor.scan(time.monotonic()-1)
    assert not advisor.SCAN_STATE.exists()


def test_pending_timestamp_and_file_permissions_are_checked(advisor, monkeypatch):
    transport(advisor, monkeypatch)
    advisor.scan(time.monotonic()+.5)
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


def test_main_never_prints_transport_credentials(advisor, monkeypatch, capsys):
    monkeypatch.setattr(advisor.sys, 'argv', ['a-share-advisor.py', 'scan'])
    monkeypatch.setattr(advisor, 'run_bounded', lambda callback, **kwargs: callback(time.monotonic()+10))
    def fail(deadline):
        raise urllib.error.URLError('private token at private URL')
    monkeypatch.setattr(advisor, '_scan', fail)
    advisor.main()
    output = capsys.readouterr()
    assert '服务或回执不可用' in output.out and 'private' not in output.out and output.err == ''


def test_weekly_is_also_inside_the_shared_budget(advisor, monkeypatch, capsys):
    monkeypatch.setattr(advisor.sys, 'argv', ['a-share-advisor.py', 'weekly'])
    def bounded(callback, seconds):
        assert seconds == 540
        return callback(123)
    monkeypatch.setattr(advisor, 'run_bounded', bounded)
    monkeypatch.setattr(advisor, 'weekly', lambda deadline: 'weekly' if deadline == 123 else 'wrong')
    advisor.main()
    assert capsys.readouterr().out == 'weekly\n'
