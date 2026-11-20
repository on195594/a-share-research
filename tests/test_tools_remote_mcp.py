"""Public MCP contracts over ASGI, with local grants and captured research only."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY, SERVER_INFO_META_KEY
from requests import ConnectionError

from a_share_research import api_keys, db, oauth_store, service
from a_share_research.account import evidence, ledger
from a_share_research.account.reconciliation import Opening
from a_share_research.tools import http_json, remote_mcp
from a_share_research.tools.control import ControlError, Gate
from a_share_research.tools.registry import TOOLS
from a_share_research.tools.server import create_app
from support.continuation import load

ORIGIN = 'https://stock.example:8443'
RESOURCE = ORIGIN + '/mcp'
MODERN = '2026-07-28'
LEGACY = '2025-11-25'


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('CACHE_DB_PATH', str(tmp_path / 'cache.db'))
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)
    path = tmp_path / 'app.db'
    with closing(db.connect(path)) as conn:
        tokens = {role: api_keys.create(conn, role, role=role) for role in ('research', 'assistant', 'owner')}
    return path, tokens


@pytest.fixture
def account_enabled():
    return True


@pytest.fixture
def remote(database, account_enabled):
    path, tokens = database
    app = create_app(path, gate=Gate(limit=1), account_shadow_enabled=account_enabled)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, tokens


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


def call(client, token, name, arguments=None, *, version=MODERN):
    response = rpc(client, token, 'tools/call', {'name': name, 'arguments': arguments or {}}, version=version)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('application/json')
    return response.json()['result']


def assert_unauthorized(response):
    assert response.status_code == 401
    assert response.headers['www-authenticate'] == f'Bearer resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"'


def test_public_auth_rechecks_each_credential_and_its_audience(remote, database):
    client, tokens = remote
    path, _ = database
    params = {'name': 'research_cards', 'arguments': {}}
    with closing(db.connect(path)) as conn:
        oauth_store.register_client(conn, 'browser', 'Browser', ['https://client.example/callback'])
        grant = oauth_store.create_grant(conn, 'browser')
        expiry = time.time() + 300
        conn.execute('INSERT INTO sessions VALUES (?,?)', (hashlib.sha256(b'browser-cookie').hexdigest(), expiry))
        for token, kind, resource, until in [('access', 'access', RESOURCE, expiry), ('refresh', 'refresh', RESOURCE, expiry),
                                             ('wrong', 'access', ORIGIN + '/other', expiry), ('expired', 'access', RESOURCE, 0)]:
            oauth_store.save_token(conn, token, kind=kind, resource=resource, expires_at=until, grant_id=grant)

        for token in [None, 'refresh', 'wrong', 'expired', tokens['owner']]:
            assert_unauthorized(rpc(client, token, 'tools/call', params))
        assert_unauthorized(rpc(client, None, 'tools/list', headers={'Cookie': 'session=browser-cookie'}))

        for token in [tokens['assistant'], 'access']:
            assert not call(client, token, 'research_cards').get('isError')
        scan_args = {'request_id': 'scan-with-token-rotation'}
        scan = call(client, 'access', 'research_scan_request', scan_args)['structuredContent']['data']
        conn.execute("UPDATE scans SET status='done' WHERE id=?", (scan['scan_id'],))
        assert oauth_store.revoke_token(conn, 'access')
        assert api_keys.revoke(conn, api_keys.authenticate(conn, tokens['assistant']).key_id)
        for token in ['access', tokens['assistant']]:
            assert_unauthorized(rpc(client, token, 'tools/call', params))

        oauth_store.save_token(conn, 'next-access', kind='access', resource=RESOURCE, expires_at=expiry, grant_id=grant)
        replay = call(client, 'next-access', 'research_scan_request', scan_args)['structuredContent']['data']
        assert (replay['scan_id'], replay['status']) == (scan['scan_id'], 'done')
        assert api_keys.revoke(conn, grant)
        assert_unauthorized(rpc(client, 'next-access', 'tools/list'))


@pytest.mark.parametrize('account_enabled', [False, True])
def test_catalog_matches_rest_visibility_schemas_and_operation_semantics(remote, account_enabled):
    client, tokens = remote
    for role in ('research', 'assistant'):
        token = tokens[role]
        rest = client.get('/tools', headers={'Authorization': 'Bearer ' + token}).json()['tools']
        response = rpc(client, token, 'tools/list')
        assert response.status_code == 200
        catalog = {tool['name']: tool for tool in response.json()['result']['tools']}
        assert set(catalog) == {tool['name'] for tool in rest}
        for tool in rest:
            actual = catalog[tool['name']]
            assert actual['description'] == tool['description']
            assert actual['inputSchema'] == tool['input_schema'] and actual['outputSchema'] == tool['output_schema']
            assert actual['annotations']['readOnlyHint'] == (tool['access'] == 'read')
            assert actual['annotations']['idempotentHint'] == tool['idempotent']
        visible = account_enabled and role == 'assistant'
        assert ('account_evidence_recent' in catalog) == visible
        if visible:
            assert catalog['research_subscriptions_replace']['annotations']['destructiveHint'] is True
            assert catalog['account_submit']['annotations']['destructiveHint'] is True
            assert catalog['account_evidence']['annotations']['destructiveHint'] is False
        else:
            denied = rpc(client, token, 'tools/call', {'name': 'account_evidence_recent', 'arguments': {}}).json()
            assert denied.get('error') or denied['result']['isError']


def test_legacy_initialize_and_call_share_the_registry_result(remote, database):
    client, tokens = remote
    with closing(db.connect(database[0])) as conn:
        load(conn)
    token = tokens['research']
    instructions = rpc(client, token, 'server/discover').json()['result']['instructions']
    skill = Path(__file__).resolve().parents[1] / 'skill/a-share/SKILL.md'
    assert hashlib.sha256(skill.read_bytes()).hexdigest()[:12] in instructions
    for version in ('2025-03-26', '2025-06-18', LEGACY):
        handshake = rpc(client, token, 'initialize', {'protocolVersion': version, 'capabilities': {},
                        'clientInfo': {'name': 'asgi-test', 'version': '1'}}, version=version)
        assert 'mcp-session-id' not in handshake.headers
        negotiated = handshake.json()['result']
        assert negotiated['protocolVersion'] == version and negotiated['instructions'] == instructions
    for version in ('2025-06-18', LEGACY, MODERN):
        result = call(client, token, 'research_report', {'report_id': 39}, version=version)
        assert not result.get('isError')
        assert result['structuredContent']['data']['id'] == 39
        text, = result['content']
        assert text['type'] == 'text' and json.loads(text['text']) == result['structuredContent']


def test_recent_evidence_returns_the_hash_from_the_upload_receipt(remote):
    client, tokens = remote
    token = tokens['assistant']
    receipt = call(client, token, 'account_evidence', {'content_base64': 'YQ=='})['structuredContent']['data']
    recent = call(client, token, 'account_evidence_recent', {'limit': 1})['structuredContent']['data']
    item, = recent['evidence']
    assert item['sha256'] == receipt['sha256'] and item['size_bytes'] == 1


@pytest.mark.parametrize('version', [LEGACY, MODERN])
def test_invalid_json_never_records_an_account_observation(remote, database, bind_account, version):
    client, tokens = remote
    path, _ = database
    token = tokens['assistant']
    directory = path.parent / 'account-evidence'
    sha = evidence.store(directory, b'Structural account opening')
    with closing(db.connect(path)) as conn:
        ledger.initialize(conn, api_keys.authenticate(conn, token), 'test', Opening(positions={}, cash='100'),
                          datetime(2026, 11, 1), directory, sha, tolerance=Decimal(0))
        bind_account(conn, 'test')
        args = dict(evidence_hash=sha, observation={'observed_at': '2026-11-11T15:00:00+08:00', 'cash': '100'})
        for field in ['"cash": "5", "cash": "100"', '"cash": NaN', '"cash": 1e999']:
            def invalid(envelope):
                return json.dumps(envelope).replace('"cash": "100"', field)

            response = rpc(client, token, 'tools/call', {'name': 'account_observe', 'arguments': args},
                           version=version, serialize=invalid)
            assert response.status_code == 400 and response.json()['error']['code'] == -32700
        assert conn.execute('SELECT COUNT(*) FROM account_observations').fetchone()[0] == 0
        assert not call(client, token, 'account_observe', args, version=version).get('isError')
        assert conn.execute('SELECT COUNT(*) FROM account_observations').fetchone()[0] == 1


def test_gate_preserves_reads_and_replays_original_write_after_resume(remote, database, monkeypatch):
    client, tokens = remote
    token = tokens['research']
    gate = client.app.state.gate
    with closing(db.connect(database[0])) as conn:
        load(conn)
    args = dict(report_id=39, request_id='check-claim', location='summary', text='请核对原文依据。')
    with gate.slot():
        assert 'tools' in rpc(client, token, 'tools/list').json()['result']
        assert not call(client, token, 'research_cards').get('isError')
        busy = call(client, token, 'research_feedback', args)
        assert busy['isError'] and '重试' in busy['content'][0]['text']
    gate.pause()
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', lambda *a, **kw: pytest.fail('Paused requests must not open SQLite'))
        for version in (LEGACY, MODERN):
            paused = rpc(client, token, 'tools/list', version=version)
            assert paused.status_code == 503 and paused.headers['Retry-After'] == '60'
            message = paused.json()['error']['message']
            assert '发布' in message and '原请求重试' in message

            response = rpc(client, token, 'tools/call', {'name': 'research_feedback', 'arguments': args}, version=version)
            assert response.status_code == 200
            reply = response.json()
            assert reply['jsonrpc'] == '2.0' and reply['id'] == 1
            result = reply['result']
            assert result['isError'] is True and result.get('structuredContent') is None
            text, = result['content']
            assert text['type'] == 'text'
            assert json.loads(text['text']) == {'error': {'code': 'paused', 'message': '发布中，约 1 分钟后用原请求重试'}}
            if version == MODERN:
                assert result['resultType'] == 'complete'
                assert result['_meta'][SERVER_INFO_META_KEY]['name'] == 'a-share'
            else:
                assert 'resultType' not in result and '_meta' not in result
    gate.resume()
    receipt = call(client, token, 'research_feedback', args)['structuredContent']['data']
    assert receipt['path'] == f"/runs/{receipt['run_id']}"
    assert call(client, token, 'research_feedback', args)['structuredContent']['data']['run_id'] == receipt['run_id']


def test_chunked_json_over_limit_is_rejected_by_both_transports(remote, monkeypatch):
    client, tokens = remote
    monkeypatch.setattr(http_json, 'MAX_BODY_SIZE', 256)
    headers = {'Authorization': 'Bearer ' + tokens['research'], 'Content-Type': 'application/json'}
    for path in ['/mcp', '/tools/research_cards']:
        response = client.post(path, headers=headers, content=iter([b'{}', b' ' * 255]))
        assert response.status_code == 413


def test_pause_cannot_finish_while_authentication_is_reading(remote, monkeypatch):
    client, tokens = remote
    gate = client.app.state.gate
    authenticate = api_keys.authenticate

    def pause_during_authentication(conn, token):
        with pytest.raises(ControlError, match='drain timed out'):
            gate.pause(timeout=0)
        return authenticate(conn, token)

    monkeypatch.setattr(api_keys, 'authenticate', pause_during_authentication)
    response = rpc(client, tokens['assistant'], 'tools/list')
    assert '发布' in response.json()['error']['message']
    assert gate.pause(timeout=0) == {'paused': True, 'in_flight': 0}


def test_tool_errors_keep_field_paths_and_hide_upstream_exceptions(remote, monkeypatch):
    client, tokens = remote
    token = tokens['research']
    legacy = client.post('/tools/research.cards', headers={'Authorization': 'Bearer ' + token}, json={})
    assert legacy.status_code == 404 and legacy.json()['error']['code'] == 'not_found'
    for name in ['missing', 'research.cards']:
        error = rpc(client, token, 'tools/call', {'name': name, 'arguments': {}}).json()['error']
        assert error['code'] == -32602
    malformed = rpc(client, token, 'tools/call', {'name': 'research_report', 'arguments': ['wrong-shape']}).json()['error']
    assert malformed['code'] == -32602 and 'arguments' in malformed['message']
    invalid = call(client, token, 'research_report', {'report_id': 'wrong-type'})
    assert invalid['isError'] and 'report_id' in invalid['content'][0]['text']
    assert invalid.get('structuredContent') is None
    assert json.loads(invalid['content'][0]['text'])['error']['code'] == 'invalid_arguments'
    absent = call(client, token, 'research_report', {'report_id': 99})
    assert absent['isError'] and '报告' in absent['content'][0]['text']

    def upstream_unavailable(conn):
        raise ConnectionError('private upstream URL')

    monkeypatch.setattr(service, 'company_cards', upstream_unavailable)
    failure = call(client, token, 'research_cards')
    assert failure['isError'] and '重试' in failure['content'][0]['text']
    assert 'private upstream URL' not in json.dumps(failure)


def test_legacy_list_and_protocol_failures_do_not_disclose_internal_errors(remote, monkeypatch, caplog):
    client, tokens = remote

    def unavailable(*args, **kwargs):
        raise RuntimeError('internal failure detail')

    for method, owner, attribute in [('tools/list', remote_mcp, 'visible_tools'),
                                     ('ping', remote_mcp.Server, 'get_request_handler')]:
        caplog.clear()
        with monkeypatch.context() as patch:
            patch.setattr(owner, attribute, unavailable)
            error = rpc(client, tokens['assistant'], method, version=LEGACY).json()['error']
        assert error['code'] == -32603 and '重试' in error['message']
        assert 'internal failure detail' not in json.dumps(error)
        assert any(r.name == remote_mcp.__name__ and r.exc_info for r in caplog.records)


def test_exact_http_endpoint_validates_host_and_never_opens_a_stream(remote, monkeypatch):
    client, tokens = remote
    token = tokens['assistant']
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', lambda *a, **kw: pytest.fail('Invalid host/origin must not open SQLite'))
        for header in [{'Host': 'other.example'}, {'Origin': 'https://other.example'}]:
            for method in ['GET', 'POST']:
                assert client.request(method, '/mcp', headers={'Authorization': 'Bearer ' + token} | header).status_code == 421
    for method in ('GET', 'DELETE'):
        response = client.request(method, '/mcp', headers={'Authorization': 'Bearer ' + token,
                                  'Accept': 'text/event-stream', 'MCP-Protocol-Version': LEGACY})
        assert response.status_code == 405


def test_public_resource_with_explicit_default_port_accepts_canonical_host(database, monkeypatch):
    path, tokens = database
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', 'https://Stock.Example:443/mcp')
    with TestClient(create_app(path), base_url='https://stock.example') as client:
        for host in ['Stock.EXAMPLE', 'Stock.EXAMPLE:443']:
            response = rpc(client, tokens['assistant'], 'server/discover',
                           headers={'Host': host, 'Origin': 'https://' + host})
            assert response.status_code == 200
            assert 'https://stock.example 加回执' in response.json()['result']['instructions']
        assert rpc(client, None, 'tools/list').headers['www-authenticate'] == (
            'Bearer resource_metadata="https://stock.example/.well-known/oauth-protected-resource/mcp"')
        assert rpc(client, tokens['assistant'], 'tools/list', headers={'Host': 'stock.example:444'}).status_code == 421


def test_endpoint_disabled_without_public_url(database, monkeypatch):
    path, tokens = database
    monkeypatch.delenv('ASHARE_PUBLIC_MCP_URL')
    with TestClient(create_app(path), base_url=ORIGIN) as client:
        assert rpc(client, tokens['research'], 'tools/list').status_code == 404
        assert client.get('/tools', headers={'Authorization': 'Bearer ' + tokens['research']}).status_code == 200
        assert client.get('/ready').status_code == 200


@pytest.fixture
def oauth_access_tokens(database):
    tokens = ('first-access', 'second-access')
    with closing(db.connect(database[0])) as conn:
        oauth_store.register_client(conn, 'browser', 'Browser', ['https://client.example/callback'])
        for token in tokens:
            grant = oauth_store.create_grant(conn, 'browser')
            oauth_store.save_token(conn, token, kind='access', resource=RESOURCE,
                                   expires_at=time.time() + 300, grant_id=grant)
    return tokens


def test_distinct_oauth_grants_do_not_reuse_scan_request(remote, database, oauth_access_tokens):
    client, _ = remote
    first, second = oauth_access_tokens
    args = {'request_id': 'same-request-id'}
    original = call(client, first, 'research_scan_request', args)['structuredContent']['data']['scan_id']
    # Finish the scan to distinguish request replay from sharing the single active scan.
    with closing(db.connect(database[0])) as conn:
        conn.execute("UPDATE scans SET status='done' WHERE id=?", (original,))
    fresh = call(client, second, 'research_scan_request', args)['structuredContent']['data']['scan_id']
    assert fresh != original
    assert call(client, first, 'research_scan_request', args)['structuredContent']['data']['scan_id'] == original


def test_revoked_oauth_client_loses_access_immediately(remote, database, oauth_access_tokens):
    client, _ = remote
    token = oauth_access_tokens[0]
    assert 'tools' in rpc(client, token, 'tools/list').json()['result']
    with closing(db.connect(database[0])) as conn:
        assert oauth_store.revoke_client(conn, 'browser')
    assert_unauthorized(rpc(client, token, 'tools/list'))


def test_collecting_read_requires_gate_slot(remote, monkeypatch):
    client, tokens = remote
    # Isolate collection from write access using the same read-tool variant as the REST tests.
    tool = TOOLS['research_cards']
    monkeypatch.setitem(TOOLS, tool.name, replace(tool, collects=True))
    with client.app.state.gate.slot():
        busy = call(client, tokens['research'], 'research_cards')
        assert busy['isError'] and json.loads(busy['content'][0]['text'])['error']['code'] == 'busy'
    assert not call(client, tokens['research'], 'research_cards').get('isError')
