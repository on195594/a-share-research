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


REDIRECT = "https://client.example/callback"
OAUTH_RESOURCE = "https://stock.example/mcp"
BINDING = dict(client_id="browser", redirect_uri=REDIRECT, resource=OAUTH_RESOURCE,
               code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")  # RFC 7636 Appendix B
