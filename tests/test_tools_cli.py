"""CLI contracts against the in-process MCP endpoint and bounded transport failures."""
import base64
import hashlib
import http.client
import io
import json
import os
import sqlite3
import urllib.error
import urllib.request
import urllib.response
from contextlib import closing
from email.message import Message
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

from a_share_research import api_keys, db, gate, scans
from a_share_research.policy import registry
from a_share_research.tools.control import Gate
from a_share_research.tools.registry import discover
from a_share_research.tools.server import create_app
from scripts import ashare as cli
from support.continuation import load
from test_policy_monitor import setup as policy_setup  # noqa: F401
from test_tools_remote_mcp import MODERN, ORIGIN, RESOURCE


def private_config(tmp_path, **changes):
    path = tmp_path / 'client.json'
    path.write_text(json.dumps({'url': RESOURCE, 'token': 'asr_private-fixture'} | changes))
    path.chmod(0o600)
    return path


def command(path, *args):
    return cli.main(['--config', str(path), *args])


@pytest.fixture
def asgi(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('CACHE_DB_PATH', str(tmp_path / 'cache.db'))
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)
    database = tmp_path / 'app.db'
    with closing(db.connect(database)) as conn:
        token = api_keys.create(conn, 'cli-test', role='assistant')
    gate = Gate(limit=1)
    requests = []
    with TestClient(create_app(database, gate=gate, account_shadow_enabled=True), base_url=ORIGIN) as client:
        class Opener:
            def open(self, req, timeout):
                assert timeout == 30 and req.get_method() == 'POST'
                requests.append(req)
                response = client.post(req.full_url, headers=dict(req.header_items()), content=req.data)
                headers = Message()
                for key, value in response.headers.items():
                    headers[key] = value
                body = io.BytesIO(response.content)
                if response.is_error:
                    raise urllib.error.HTTPError(req.full_url, response.status_code, 'HTTP error', headers, body)
                return urllib.response.addinfourl(body, headers, req.full_url, response.status_code)

        def build(*handlers):
            # Keep the grant on the selected route even if the host exports a proxy.
            assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
            redirect, = (h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
            assert redirect.redirect_request(None, None, 307, '', {}, 'https://other.example') is None
            return Opener()

        monkeypatch.setattr(urllib.request, 'build_opener', build)
        yield token, gate, requests


@pytest.mark.parametrize('connect', [None, 'http://127.0.0.1:8621', 'http://[::1]:8621'])
def test_modern_list_and_call_use_public_identity(asgi, tmp_path, connect, capsys):
    token, _, requests = asgi
    config = private_config(tmp_path, token=token, **({'connect': connect} if connect else {}))
    assert command(config, 'tools') == 0
    catalog = json.loads(capsys.readouterr().out)
    with closing(db.connect(tmp_path / 'app.db')) as conn:
        principal = api_keys.authenticate(conn, token)
        assert catalog == {'tools': discover(principal, account_shadow_enabled=True)}
        load(conn)
    assert command(config, 'call', 'research_report', '--json', '{"report_id":39}') == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['ok'] is True and receipt['result']['data']['id'] == 39
    assert len(requests) == 2
    for req, method in zip(requests, ('tools/list', 'tools/call'), strict=True):
        assert req.full_url == (connect + '/mcp' if connect else RESOURCE)
        headers = {key.lower(): value for key, value in req.header_items()}
        if connect:
            assert headers['host'] == ORIGIN.removeprefix('https://')
        assert headers['authorization'] == 'Bearer ' + token
        assert headers['mcp-protocol-version'] == MODERN
        assert headers['mcp-method'] == method
        assert 'application/json' in headers['accept']
        message = json.loads(req.data)
        assert message['method'] == method
        assert message['params']['_meta'][PROTOCOL_VERSION_META_KEY] == MODERN
        assert message['params']['_meta'][CLIENT_CAPABILITIES_META_KEY] == {}
        if method == 'tools/call':
            assert headers['mcp-name'] == message['params']['name'] == 'research_report'
            assert message['params']['arguments'] == {'report_id': 39}


def test_paused_busy_and_unauthorized_have_distinct_recovery(asgi, tmp_path, capsys):
    token, gate, requests = asgi
    config = private_config(tmp_path, token=token)
    with gate.slot():
        assert command(config, 'call', 'research_scan_request', '--json', '{"request_id":"cli-busy"}') == 21
    busy = capsys.readouterr()
    assert not busy.out and '原请求' in busy.err
    gate.pause()
    for args in [('tools',), ('call', 'research_cards')]:
        assert command(config, *args) == 21
        paused = capsys.readouterr()
        assert not paused.out and '发布暂停' in paused.err
    gate.resume()
    bad = 'asr_revoked-fixture'
    assert command(private_config(tmp_path, token=bad), 'tools') == 1
    auth = capsys.readouterr()
    assert not auth.out and '凭据失效' in auth.err and bad not in auth.err
    assert len(requests) == 4


def test_only_verified_stale_activation_receives_exit_20(asgi, policy_setup, tmp_path, capsys):  # noqa: F811
    token, _, _ = asgi
    config = private_config(tmp_path, token=token)
    _, conn, principals, manifest, previous, _, captured = policy_setup
    registry.activate(conn, principals['owner'], manifest['identity'], expected_id=previous['id'],
                      request_id='activate-for-cli', reason='exercise stale activation recovery')
    for activation, expected in [(previous['id'], 20), (999, 1)]:
        args = {'expected_activation_id': activation, 'request_id': 'cli-stale-pointer'}
        assert command(config, 'call', 'policy_monitor', '--json', json.dumps(args)) == expected
        output = capsys.readouterr()
        assert not output.out and token not in output.err
        if expected == 20:
            assert '政策异常' in output.err
    assert conn.execute('SELECT count(*) FROM policy_evaluations').fetchone()[0] == 0
    assert not captured


def test_cli_reads_large_monitor_receipt(asgi, policy_setup, tmp_path, capsys):  # noqa: F811
    token, _, _ = asgi
    _, conn, _, _, activation, bundle, _ = policy_setup
    sample = Path(__file__).parent / 'policy_v0/fixtures/monitor/research-card-stance.json'
    card = json.loads(sample.read_text())['card']
    # Repeat a captured card only to exercise the transport near the collector's 8 MiB limit.
    count = 7 * 1024**2 // len(gate.canonical(card).encode())
    bundle['research_result'] = {'status': 'ok', 'cards': [card] * count}
    assert 6 * 1024**2 < len(gate.canonical(bundle).encode()) < 8 * 1024**2
    args = {'request_id': 'large-cli-receipt', 'expected_activation_id': activation['id']}
    code = command(private_config(tmp_path, token=token), 'call', 'policy_monitor', '--json', json.dumps(args))
    assert conn.execute('SELECT count(*) FROM policy_evaluations WHERE request_id=?', (args['request_id'],)).fetchone()[0] == 1
    assert code == 0
    receipt = json.loads(capsys.readouterr().out)['result']['data']
    assert receipt['input_hash'] == gate.digest(receipt['input']) == gate.digest(bundle)


def test_cli_uploads_pdf_via_stdin(asgi, tmp_path, monkeypatch, capsys):
    token, _, _ = asgi
    document = (Path(__file__).parent / 'data/601138-buyback-announcement.pdf').read_bytes()
    payload = json.dumps({'content_base64': base64.b64encode(document).decode('ascii')}).encode()
    assert len(payload) > 128 * 1024
    monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(io.BytesIO(payload)))
    assert command(private_config(tmp_path, token=token), 'call', 'account_evidence', '--json', '-') == 0
    assert json.loads(capsys.readouterr().out)['result']['data']['sha256'] == hashlib.sha256(document).hexdigest()


def test_oversized_stdin_is_bounded_before_transport(tmp_path, monkeypatch, offline, capsys):
    monkeypatch.setattr(cli, '_INPUT_LIMIT', 64)
    stream = io.BytesIO(b'{' + b' ' * 1000 + b'}')
    monkeypatch.setattr(cli.sys, 'stdin', io.TextIOWrapper(stream))
    assert command(private_config(tmp_path), 'call', 'account_evidence', '--json', '-') == 1
    assert stream.tell() == 65 and '标准输入' in capsys.readouterr().err


def test_missing_receipt_after_commit_requires_original_identity_and_request(asgi, tmp_path, monkeypatch, capsys):
    token, _, requests = asgi
    config = private_config(tmp_path, token=token)
    args = ('call', 'research_scan_request', '--json', '{"request_id":"cli-lost-receipt"}')

    def fail_after_commit(*args, **kwargs):
        raise sqlite3.OperationalError('private upstream detail')

    with monkeypatch.context() as patch:
        patch.setattr(scans, 'view', fail_after_commit)
        assert command(config, *args) == 22
    output = capsys.readouterr()
    assert not output.out and '结果未知' in output.err and 'private upstream detail' not in output.err
    assert len(requests) == 1
    with closing(db.connect(tmp_path / 'app.db')) as conn:
        scan_id = conn.execute("SELECT scan_id FROM scan_requests WHERE request_id='cli-lost-receipt'").fetchone()[0]
    assert command(config, *args) == 0
    assert json.loads(capsys.readouterr().out)['result']['data']['scan_id'] == scan_id


def test_real_rpc_and_tool_argument_errors(asgi, tmp_path, capsys):
    token, _, _ = asgi
    config = private_config(tmp_path, token=token)
    for name, args in [('missing_tool', '{}'), ('research_report', '{"report_id":"bad"}')]:
        assert command(config, 'call', name, '--json', args) == 1
        output = capsys.readouterr()
        assert not output.out and '参数错误' in output.err and token not in output.err
        field = 'report_id' if name == 'research_report' else 'name'
        assert field in output.err


def test_research_cli_visibility_and_account_denial(asgi, tmp_path, capsys):
    with closing(db.connect(tmp_path / 'app.db')) as conn:
        token = api_keys.create(conn, 'research-cli', role='research')
    config = private_config(tmp_path, token=token)
    assert command(config, 'tools') == 0
    names = [tool['name'] for tool in json.loads(capsys.readouterr().out)['tools']]
    assert 'research_report' in names
    assert not any(name.startswith(('account_', 'policy_')) for name in names)
    assert command(config, 'call', 'account_current') == 1
    output = capsys.readouterr()
    assert not output.out and '权限' in output.err


@pytest.mark.parametrize('status', [503, 502])
def test_gateway_failure_without_receipt_is_unknown(status):
    error = urllib.error.HTTPError(RESOURCE, status, 'gateway failure', {}, io.BytesIO(b'<html>private detail</html>'))
    result = cli.http_error(error, 'private-token')
    assert result.exit_code == 22 and '结果未知' in str(result) and 'private detail' not in str(result)


def test_auth_database_failure_is_rejected_before_dispatch(asgi, tmp_path, monkeypatch, capsys):
    token, _, _ = asgi
    config = private_config(tmp_path, token=token)

    def unavailable(*args):
        raise sqlite3.OperationalError('private auth detail')

    monkeypatch.setattr(api_keys, 'authenticate', unavailable)
    assert command(config, 'call', 'research_scan_request', '--json', '{"request_id":"auth-unavailable"}') == 21
    output = capsys.readouterr()
    assert not output.out and '原请求重试' in output.err and 'private auth detail' not in output.err
    with closing(db.connect(tmp_path / 'app.db')) as conn:
        assert conn.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 0


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: pytest.fail('must not send a request'))


@pytest.mark.parametrize('connect', [
    'https://127.0.0.1:8621', 'http://localhost:8621', 'http://127.0.0.2:8621',
    'http://127.0.0.1', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
    'http://127.0.0.1:8621/mcp', 'http://127.0.0.1@evil.example:80',
    'http://[::1]:8621?x=1', 'http://[::1]:8621#x',
])
def test_connect_only_accepts_literal_loopback_origins(connect, tmp_path, offline, capsys):
    assert command(private_config(tmp_path, connect=connect), 'tools') == 1
    output = capsys.readouterr()
    assert not output.out and 'asr_private-fixture' not in output.err


@pytest.mark.parametrize('url', ['http://127.0.0.1:8621', 'http://127.0.0.1:8621/tools'])
def test_old_rest_config_requires_explicit_upgrade(tmp_path, offline, capsys, url):
    assert command(private_config(tmp_path, url=url), 'tools') == 1
    output = capsys.readouterr()
    assert not output.out and '升级' in output.err and '/mcp' in output.err


@pytest.mark.parametrize('changes', [
    {'url': 'https://external.test'}, {'url': 'http://external.test/mcp'}, {'url': 123},
    {'url': 'https://user@external.test/mcp'}, {'url': RESOURCE + '?token=secret'},
    {'token': None}, {'token': 'has whitespace'}, {'token': '秘密'}, {'extra': 'ignored'},
])
def test_invalid_config_never_reaches_transport(tmp_path, offline, capsys, changes):
    assert command(private_config(tmp_path, **changes), 'tools') == 1
    output = capsys.readouterr()
    assert not output.out and 'asr_private-fixture' not in output.err and 'external.test' not in output.err


@pytest.mark.parametrize('variant', [
    'public', 'symlink', 'symlink-parent', 'fifo', 'directory', 'missing', 'relative',
    'duplicate', 'oversized', 'malformed',
])
def test_unsafe_config_files_fail_without_echoing_content(tmp_path, offline, capsys, variant):
    path = private_config(tmp_path)
    if variant == 'public':
        path.chmod(0o644)
    elif variant == 'symlink':
        target = path
        path = tmp_path / 'link.json'
        path.symlink_to(target)
    elif variant == 'symlink-parent':
        parent = tmp_path / 'linked'
        parent.symlink_to(tmp_path, target_is_directory=True)
        path = parent / path.name
    elif variant == 'fifo':
        path = tmp_path / 'fifo'
        os.mkfifo(path, 0o600)
    elif variant == 'directory':
        path = tmp_path
    elif variant == 'missing':
        path = tmp_path / 'missing'
    elif variant == 'relative':
        path = 'relative-client.json'
    elif variant == 'duplicate':
        path.write_text('{"token":"SECRET","token":"two","url":"' + RESOURCE + '"}')
    elif variant == 'oversized':
        path.write_text('SECRET' + ' ' * 16384)
    elif variant == 'malformed':
        path.write_text('SECRET invalid json')
    assert command(path, 'tools') == 1
    output = capsys.readouterr()
    assert not output.out and 'SECRET' not in output.err and 'asr_private-fixture' not in output.err


@pytest.mark.parametrize('args', [
    ('call', '../escape'), ('call', 'x', '--json', '[]'), ('call', 'x', '--json', '{"x":NaN}'),
    ('call', 'x', '--json', '{"x":1e999}'), ('call', 'x', '--json', '{"x":1,"x":2}'),
])
def test_invalid_call_rejected_before_transport(args, tmp_path, offline):
    assert command(private_config(tmp_path), *args) == 1


@pytest.mark.parametrize(('failure', 'exit_code'), [
    ('refused', 21), ('timeout', 22), ('reset', 22), ('truncated', 22),
    ('invalid-json', 22), ('wrong-id', 22), ('forbidden', 1), ('oversized', 23), ('oversized-length', 23),
])
def test_transport_outcomes_never_echo_credentials_or_retry(failure, exit_code, tmp_path, monkeypatch, capsys):
    token = 'asr_private-fixture'
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append(req)
            if failure == 'refused':
                raise urllib.error.URLError(ConnectionRefusedError(token))
            if failure == 'timeout':
                raise urllib.error.URLError(TimeoutError(token))
            if failure == 'reset':
                raise ConnectionResetError(token)
            if failure == 'forbidden':
                raise urllib.error.HTTPError(req.full_url, 403, token, {}, io.BytesIO(token.encode()))
            request_id = json.loads(req.data)['id']
            reply_id = 'mismatched' if failure == 'wrong-id' else request_id
            body = json.dumps({'jsonrpc': '2.0', 'id': reply_id, 'result': {'tools': []}}).encode()
            if failure == 'invalid-json':
                body = body[:len(body) // 2]
            if failure == 'oversized':
                monkeypatch.setattr(cli, '_RESPONSE_LIMIT', len(body) - 1)
            # A bounded HTTPResponse.read(size) can return complete JSON despite a premature EOF.
            length = len(body) + (50 if failure == 'truncated' else 0)
            if failure == 'oversized-length':
                length = cli._RESPONSE_LIMIT + 1
            header = '' if failure == 'oversized' else f'Content-Length: {length}\r\n'
            wire = f'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n{header}\r\n'.encode()
            class Socket:
                def makefile(self, mode):
                    return io.BytesIO(wire + body)
            response = http.client.HTTPResponse(Socket())
            response.begin()
            return response

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: Opener())
    assert command(private_config(tmp_path, token=token), 'tools') == exit_code
    output = capsys.readouterr()
    assert len(calls) == 1 and not output.out and token not in output.err
    if exit_code == 22:
        assert '结果未知' in output.err
    if exit_code == 23:
        assert '回执超限' in output.err and '原请求' in output.err
    if failure == 'forbidden':
        assert '凭据' in output.err or '权限' in output.err
