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


@pytest.mark.parametrize('args', [['call', '../escape'], ['call', 'x', '--json', '[]'],
    ['call', 'x', '--json', '{"x":NaN}']])
def test_invalid_call_fails_before_transport(args, monkeypatch):
    monkeypatch.setattr(cli, 'request', lambda *args: pytest.fail('must not send'))
    assert cli.main(args) == 1
