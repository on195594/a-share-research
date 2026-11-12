"""CLI transport boundaries; no listening port or live credential is required."""
import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest

spec = importlib.util.spec_from_file_location('ashare_cli', Path(__file__).parents[1] / 'scripts/ashare.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.mark.parametrize('url', ['https://example.com', 'http://localhost:8621',
    'http://127.0.0.1@evil.test', 'http://127.0.0.1:8621/path', 'http://127.0.0.1/?token=x',
    'http://127.0.0.1:0', 'http://[::ffff:192.168.1.1]'])
def test_rejects_non_loopback_or_ambiguous_target(url):
    with pytest.raises(cli.ClientError):
        cli.base_url(url)


def test_call_wire_contract_and_proxy_redirect_protection(monkeypatch, capsys):
    monkeypatch.setenv('ASHARE_TOKEN', 'asr_test-secret')
    monkeypatch.delenv('ASHARE_URL', raising=False)
    seen = []

    class Opener:
        def open(self, req, timeout):
            seen.append((req, timeout))
            return io.BytesIO(b'{"ok":true,"result":{"value":3}}')

    def build(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], cli.NoRedirect)
        return Opener()

    monkeypatch.setattr(cli.urllib.request, 'build_opener', build)
    assert cli.main(['call', 'research.card', '--json', '{"code":"600519"}']) == 0
    req, timeout = seen[0]
    assert req.full_url == 'http://127.0.0.1:8621/tools/research.card'
    assert req.get_method() == 'POST' and timeout == 30
    assert json.loads(req.data) == {'code': '600519'}
    assert req.get_header('Authorization') == 'Bearer asr_test-secret'
    assert json.loads(capsys.readouterr().out)['result'] == {'value': 3}
    assert cli.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test') is None


def test_error_never_echoes_server_body_or_token(monkeypatch, capsys):
    monkeypatch.setenv('ASHARE_TOKEN', 'asr_secret')

    class Opener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, 'asr_secret', {}, io.BytesIO(b'asr_secret'))

    monkeypatch.setattr(cli.urllib.request, 'build_opener', lambda *args: Opener())
    assert cli.main(['tools']) == 1
    output = capsys.readouterr()
    assert output.out == '' and '403' in output.err and 'asr_secret' not in output.err


@pytest.mark.parametrize(('status', 'body', 'expected'), [
    (503, b'{"ok":false,"error":{"code":"paused","message":"asr_secret"}}', '暂停'),
    (503, b'{"ok":false,"error":{"code":"busy","message":"asr_secret"}}', '繁忙'),
    (503, b'{"error":{"code":"asr_secret","message":"asr_secret"}}', '暂不可用'),
    (503, b'{"error":{"code":["paused"]}}', '暂不可用'),
    (503, b'{"error":{"code":"paused","code":"busy"}}', '暂不可用'),
    (503, b'asr_secret', '暂不可用'),
    (503, b' ' * 4097 + b'asr_secret', '暂不可用'),
    (500, b'{"error":{"code":"paused","message":"asr_secret"}}', '暂不可用'),
])
def test_service_error_guidance_is_safe_and_does_not_retry(monkeypatch, status, body, expected):
    monkeypatch.setenv('ASHARE_TOKEN', 'asr_secret')
    calls = []

    class BoundedBody(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 4097
            return super().read(size)

    stream = BoundedBody(body)

    class Opener:
        def open(self, req, timeout):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, status, 'asr_secret', {}, stream)

    monkeypatch.setattr(cli.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(cli.ClientError) as caught:
        cli.request('/tools/research.report', {'report_id': 157})
    message = str(caught.value)
    assert expected in message and str(status) in message
    assert '未自动重试' in message and '凭据' not in message
    assert 'asr_secret' not in message and 'http://' not in message
    assert caught.value.status == status and len(calls) == 1
    assert stream.closed


@pytest.mark.parametrize('args', [['call', '../escape'], ['call', 'x', '--json', '[]'],
    ['call', 'x', '--json', '{"x":NaN}']])
def test_invalid_call_fails_before_transport(args, monkeypatch):
    monkeypatch.setattr(cli, 'request', lambda *args: pytest.fail('must not send'))
    assert cli.main(args) == 1


def private_config(tmp_path, **changes):
    path=tmp_path/'client.json'
    path.write_text(json.dumps({'url':'http://127.0.0.1:8621','token':'asr_private-fixture'}|changes))
    path.chmod(0o600)
    return path


def test_private_config_overrides_only_this_invocation(tmp_path,monkeypatch):
    path=private_config(tmp_path)
    monkeypatch.setenv('ASHARE_TOKEN','original-token')
    monkeypatch.setenv('ASHARE_URL','http://127.0.0.1:1')
    seen=[]
    def transport(route,payload=None):
        seen.append((cli.os.environ['ASHARE_TOKEN'],cli.os.environ['ASHARE_URL'],route))
        return {'tools':[]}
    monkeypatch.setattr(cli,'request',transport)
    assert cli.main(['--config',str(path),'tools'])==0
    assert seen==[('asr_private-fixture','http://127.0.0.1:8621','/tools')]
    assert cli.os.environ['ASHARE_TOKEN']=='original-token'
    assert cli.os.environ['ASHARE_URL']=='http://127.0.0.1:1'


def test_private_config_mcp_startup_and_error_restore_environment(tmp_path,monkeypatch):
    path=private_config(tmp_path)
    monkeypatch.delenv('ASHARE_TOKEN',raising=False)
    monkeypatch.delenv('ASHARE_URL',raising=False)
    def fail():
        assert cli.os.environ['ASHARE_TOKEN']=='asr_private-fixture'
        raise cli.ClientError('fixture failure')
    monkeypatch.setattr(cli,'serve_mcp',fail)
    assert cli.main(['--config',str(path),'mcp'])==1
    assert 'ASHARE_TOKEN' not in cli.os.environ and 'ASHARE_URL' not in cli.os.environ


@pytest.mark.parametrize('changes',[{'url':'https://external.test'},{'url':123},{'token':None},
    {'token':'has whitespace'},{'token':'秘密'},{'extra':'ignored'}])
def test_invalid_config_never_reaches_transport(tmp_path,monkeypatch,capsys,changes):
    path=private_config(tmp_path,**changes)
    monkeypatch.setattr(cli,'request',lambda *args:pytest.fail('must not connect'))
    assert cli.main(['--config',str(path),'tools'])==1
    output=capsys.readouterr()
    assert not output.out and 'asr_private-fixture' not in output.err and 'external.test' not in output.err


@pytest.mark.parametrize('variant',['public','symlink','symlink-parent','fifo','directory','missing','relative','duplicate','oversized','malformed'])
def test_unsafe_config_files_fail_without_echoing_content(tmp_path,monkeypatch,capsys,variant):
    path=private_config(tmp_path)
    if variant=='public': path.chmod(0o644)
    elif variant=='symlink':
        target=path;path=tmp_path/'link.json';path.symlink_to(target)
    elif variant=='symlink-parent':
        parent=tmp_path/'linked';parent.symlink_to(tmp_path,target_is_directory=True);path=parent/'client.json'
    elif variant=='fifo':
        path=tmp_path/'fifo';cli.os.mkfifo(path,0o600)
    elif variant=='directory': path=tmp_path
    elif variant=='missing': path=tmp_path/'missing'
    elif variant=='relative': path='relative-client.json'
    elif variant=='duplicate': path.write_text('{"token":"SECRET","token":"two","url":"http://127.0.0.1:8621"}')
    elif variant=='oversized': path.write_text('SECRET'+' '*16384)
    elif variant=='malformed': path.write_text('SECRET invalid json')
    monkeypatch.setattr(cli,'request',lambda *args:pytest.fail('must not connect'))
    assert cli.main(['--config',str(path),'tools'])==1
    output=capsys.readouterr()
    assert not output.out and 'SECRET' not in output.err and 'asr_private-fixture' not in output.err


@pytest.mark.parametrize('payload',['{"x":1e999}','{"x":1,"x":2}'])
def test_cli_rejects_overflow_and_duplicate_arguments(payload,monkeypatch):
    monkeypatch.setattr(cli,'request',lambda *args:pytest.fail('must not connect'))
    assert cli.main(['call','fixture.read','--json',payload])==1
