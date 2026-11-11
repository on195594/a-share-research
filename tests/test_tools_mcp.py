"""MCP transport contracts using explicitly synthetic metadata, never investment evidence."""
import importlib.util
import io
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('ashare_mcp', Path(__file__).parents[1] / 'scripts/ashare.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def rpc(method, params=None, request_id=1):
    message = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        message['params'] = params
    return message


def initialize(version='2025-11-25'):
    return rpc('initialize', {'protocolVersion': version, 'capabilities': {},
                             'clientInfo': {'name': 'boundary-test', 'version': '1'}})


def ready_bridge():
    bridge = cli.MCPBridge()
    assert 'result' in bridge.handle(initialize())
    assert bridge.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None
    return bridge


def no_transport(monkeypatch):
    monkeypatch.setattr(cli, 'request', lambda *args: pytest.fail('must not send HTTP'))


@pytest.mark.parametrize('version', ['2025-11-25', '2025-06-18'])
def test_negotiates_supported_protocol_without_transport(version, monkeypatch):
    no_transport(monkeypatch)
    response = cli.MCPBridge().handle(initialize(version))
    assert response['id'] == 1
    assert response['jsonrpc'] == '2.0'
    assert response['result']['protocolVersion'] == version
    assert 'tools' in response['result']['capabilities']
    assert 'serverInfo' in response['result']


def test_unknown_protocol_offers_supported_version(monkeypatch):
    no_transport(monkeypatch)
    result = cli.MCPBridge().handle(initialize('unknown-version'))['result']
    assert result['protocolVersion'] == '2025-11-25'


def test_calls_require_initialization_and_ping_is_local(monkeypatch):
    no_transport(monkeypatch)
    bridge = cli.MCPBridge()
    assert bridge.handle(rpc('tools/list'))['error']['code'] == -32600
    assert bridge.handle(rpc('ping'))['result'] == {}
    bridge.handle(initialize())
    assert bridge.handle(rpc('tools/list'))['error']['code'] == -32600
    bridge.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert bridge.handle(initialize())['error']['code'] == -32600
    assert bridge.handle(rpc('missing/method'))['error']['code'] == -32601


@pytest.mark.parametrize('params', [{}, {'protocolVersion': 42, 'capabilities': {}, 'clientInfo': {}},
    {'protocolVersion': '2025-11-25', 'capabilities': [], 'clientInfo': {'name': 'test', 'version': '1'}},
    {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test'}}])
def test_invalid_initialize_parameters(params, monkeypatch):
    no_transport(monkeypatch)
    assert cli.MCPBridge().handle(rpc('initialize', params))['error']['code'] == -32602


def test_live_discovery_preserves_schemas_and_annotations(monkeypatch):
    # Synthetic transport metadata deliberately unrelated to any research sample.
    tool = {'name': 'fixture.read', 'description': 'Transport fixture', 'version': 1,
            'input_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}}},
            'output_schema': {'type': 'object', 'properties': {'data': {'type': 'object'}}},
            'access': 'read', 'idempotent': True, 'errors': []}
    calls = []

    def transport(path, payload=None):
        calls.append((path, payload))
        return {'tools': [tool] if len(calls) == 1 else []}

    monkeypatch.setattr(cli, 'request', transport)
    bridge = ready_bridge()
    listed = bridge.handle(rpc('tools/list'))['result']['tools']
    assert len(listed) == 1
    actual = listed[0]
    assert actual['name'] == tool['name']
    assert actual['description'] == tool['description']
    assert actual['inputSchema'] == tool['input_schema']
    assert actual['outputSchema'] == tool['output_schema']
    assert actual['annotations']['readOnlyHint'] is True
    assert actual['annotations']['idempotentHint'] is True
    assert bridge.handle(rpc('tools/list'))['result']['tools'] == []
    assert calls == [('/tools', None), ('/tools', None)]


def test_call_keeps_structured_and_text_results_equivalent(monkeypatch):
    calls = []
    payload = {'data': {'text': '中文\n第二行', 'value': 3}}

    def transport(path, arguments=None):
        calls.append((path, arguments))
        return {'ok': True, 'result': payload}

    monkeypatch.setattr(cli, 'request', transport)
    result = ready_bridge().handle(rpc('tools/call', {'name': 'fixture.read', 'arguments': {'value': 'x'}}, 'call-id'))
    assert result['id'] == 'call-id'
    assert result['result']['isError'] is False
    assert result['result']['structuredContent'] == payload
    assert json.loads(result['result']['content'][0]['text']) == payload
    assert calls == [('/tools/fixture.read', {'value': 'x'})]


def test_omitted_arguments_are_empty_object(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, 'request', lambda path, payload: calls.append((path, payload)) or {'ok': True, 'result': {}})
    ready_bridge().handle(rpc('tools/call', {'name': 'fixture.read'}))
    assert calls == [('/tools/fixture.read', {})]


@pytest.mark.parametrize('params', [{}, {'name': '../escape'}, {'name': 3},
    {'name': 'fixture.read', 'arguments': []}, {'name': 'fixture.read', 'arguments': None}])
def test_invalid_call_parameters_never_reach_service(params, monkeypatch):
    no_transport(monkeypatch)
    assert ready_bridge().handle(rpc('tools/call', params))['error']['code'] == -32602


def test_service_failure_is_tool_error_without_retry(monkeypatch):
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise cli.ClientError('HTTP 503; no retry')

    monkeypatch.setattr(cli, 'request', unavailable)
    bridge = ready_bridge()
    response = bridge.handle(rpc('tools/call', {'name': 'fixture.read'}))
    assert response['result']['isError'] is True
    assert '503' in response['result']['content'][0]['text']
    assert len(calls) == 1
    assert bridge.handle(rpc('tools/list'))['error']['code'] == -32603
    assert len(calls) == 2


@pytest.mark.parametrize('status', [401, 403, 404, 422, 503])
def test_http_failure_classification_preserves_no_retry(status, monkeypatch):
    calls = []

    def refused(*args):
        calls.append(args)
        raise cli.ClientError(f'HTTP {status}', status=status)

    monkeypatch.setattr(cli, 'request', refused)
    response = ready_bridge().handle(rpc('tools/call', {'name': 'fixture.read'}))
    if status == 404:
        assert response['error']['code'] == -32602
    else:
        assert response['result']['isError'] is True
    assert len(calls) == 1


@pytest.mark.parametrize('tool', [None, {}, {'name': 'fixture.read', 'description': 'fixture',
    'input_schema': {}, 'output_schema': {}, 'access': 'read', 'idempotent': 'yes'}])
def test_invalid_discovery_schema_is_protocol_error(tool, monkeypatch):
    monkeypatch.setattr(cli, 'request', lambda *args: {'tools': [tool]})
    assert ready_bridge().handle(rpc('tools/list'))['error']['code'] == -32603


def test_failed_policy_subprocess_remains_a_tool_failure(monkeypatch):
    payload = {'exit_code': 1, 'stdout': '', 'stderr': 'fixture subprocess failed'}
    monkeypatch.setattr(cli, 'request', lambda *args: {'ok': True, 'result': payload})
    result = ready_bridge().handle(rpc('tools/call', {'name': 'policy.v0.read'}))['result']
    assert result['isError'] is True
    assert result['structuredContent'] == payload


@pytest.mark.parametrize('method', ['notifications/initialized', 'notifications/cancelled',
                                    'unknown/notification', 'tools/call'])
def test_notifications_never_dispatch_or_reply(method, monkeypatch):
    no_transport(monkeypatch)
    assert ready_bridge().handle({'jsonrpc': '2.0', 'method': method,
                                 'params': {'name': 'fixture.write'}}) is None


@pytest.mark.parametrize('message', [[], None, 1, {'jsonrpc': '1.0', 'id': 1, 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': None, 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': True, 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': [], 'method': 'ping'},
    {'jsonrpc': '2.0', 'id': 1, 'method': 5}])
def test_invalid_envelopes_are_rejected(message, monkeypatch):
    no_transport(monkeypatch)
    assert cli.MCPBridge().handle(message)['error']['code'] == -32600


@pytest.mark.parametrize('raw', [b'{broken}\n', b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}\n', b'\xff\n',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1e999}}\n'])
def test_strict_json_lines_reject_ambiguous_input(raw, monkeypatch):
    no_transport(monkeypatch)
    output = io.BytesIO()
    cli.serve_mcp(io.BytesIO(raw), output)
    response = json.loads(output.getvalue())
    assert response['id'] is None
    assert response['error']['code'] == -32700


def test_stdio_eof_notifications_and_utf8_use_only_json_lines(monkeypatch, capsys):
    no_transport(monkeypatch)
    messages = [initialize(), {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, rpc('ping', request_id='编号')]
    data = b''.join(json.dumps(message, ensure_ascii=False).encode() + b'\n' for message in messages)
    output = io.BytesIO()
    cli.serve_mcp(io.BytesIO(data), output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 2
    assert responses[0]['result']['protocolVersion'] == '2025-11-25'
    assert responses[1] == {'jsonrpc': '2.0', 'id': '编号', 'result': {}}
    assert capsys.readouterr().out == ''
    empty = io.BytesIO()
    cli.serve_mcp(io.BytesIO(), empty)
    assert empty.getvalue() == b''


def test_oversized_line_stops_stream_without_dispatch(monkeypatch):
    no_transport(monkeypatch)
    data = io.BytesIO(b' ' * (32 * 1024 * 1024 + 1) + b'\n')
    output = io.BytesIO()
    assert cli.serve_mcp(data, output) == 1
    response = json.loads(output.getvalue())
    assert response['error']['code'] == -32700
    assert data.tell() == 32 * 1024 * 1024 + 1


def test_mcp_subcommand_uses_stdio_exit_status(monkeypatch):
    monkeypatch.setattr(cli, 'serve_mcp', lambda: 7)
    assert cli.main(['mcp']) == 7
