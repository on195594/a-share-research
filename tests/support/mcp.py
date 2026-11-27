"""Request helpers and bindings for local MCP and OAuth tests."""

import json

from mcp.types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

ORIGIN = 'https://stock.example:8443'
RESOURCE = ORIGIN + '/mcp'
MODERN = '2026-07-28'


def rpc(client, token, method, params=None, *, version=MODERN, headers=None, serialize=json.dumps):
    fields = {} if token is None else {'Authorization': 'Bearer ' + token}
    params = dict(params or {})
    if method != 'initialize':
        fields['MCP-Protocol-Version'] = version
    if version == MODERN:
        fields['Mcp-Method'] = method
        if method == 'tools/call':
            fields['Mcp-Name'] = params['name']
        params['_meta'] = {PROTOCOL_VERSION_META_KEY: version, CLIENT_CAPABILITIES_META_KEY: {}}
    fields['Content-Type'] = 'application/json'
    return client.post('/mcp', headers=fields | (headers or {}),
                       content=serialize({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}))


def list_tools(client, token, **kwargs) -> dict[str, dict]:
    """The tools/list catalog keyed by tool name."""
    response = rpc(client, token, 'tools/list', **kwargs)
    assert response.status_code == 200, response.text
    return {tool['name']: tool for tool in response.json()['result']['tools']}


def call_tool(client, token, name, arguments=None, **kwargs) -> dict:
    """The JSON-RPC body of one tools/call that passed authentication."""
    response = rpc(client, token, 'tools/call', {'name': name, 'arguments': arguments or {}}, **kwargs)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('application/json')
    return response.json()


def tool_data(client, token, name, arguments=None, **kwargs) -> dict:
    """The structured registry result of a successful call."""
    result = call_tool(client, token, name, arguments, **kwargs)['result']
    assert not result.get('isError'), result['content'][0]['text']
    return result['structuredContent']


def tool_error(client, token, name, arguments=None, **kwargs) -> dict:
    """The `{code, message}` of a tool-level failure."""
    result = call_tool(client, token, name, arguments, **kwargs)['result']
    assert result['isError'] is True and result.get('structuredContent') is None, result
    return json.loads(result['content'][0]['text'])['error']


def protocol_error(client, token, name, arguments=None, **kwargs) -> dict:
    """The JSON-RPC error of a call rejected before dispatch, e.g. an unknown tool."""
    body = rpc(client, token, 'tools/call', {'name': name, 'arguments': arguments or {}}, **kwargs).json()
    assert 'result' not in body, body
    return body['error']


REDIRECT = "https://client.example/callback"
OAUTH_RESOURCE = "https://stock.example/mcp"
BINDING = dict(client_id="browser", redirect_uri=REDIRECT, resource=OAUTH_RESOURCE,
               code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")  # RFC 7636 Appendix B
