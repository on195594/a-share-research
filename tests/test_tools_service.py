"""Credential isolation and physical database access modes at the tool boundary."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from requests import ConnectionError as UpstreamConnectionError

from a_share_research import api_keys, db, service
from a_share_research.tools.registry import Arguments, Context, Payload, TOOLS, ToolError, invoke
from a_share_research.tools.server import create_app


@pytest.fixture
def account_enabled():
    return False


@pytest.fixture
def setup(tmp_path, account_enabled):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, role, role=role) for role in ('research', 'assistant', 'owner')}
    conn.close()
    with TestClient(create_app(path, account_shadow_enabled=account_enabled)) as client:
        yield path, tokens, client


def header(token):
    return {'Authorization': 'Bearer ' + token}


@pytest.mark.parametrize('account_enabled', [False, True])
def test_discovery_and_call_enforce_same_scope(setup, account_enabled):
    path, tokens, client = setup
    assert client.get('/tools').status_code == 401
    for role, token in tokens.items():
        names = {t['name'] for t in client.get('/tools', headers=header(token)).json()['tools']}
        for name in ('research.cards', 'research.methods'):
            assert (name in names) == (role != 'owner')
        for name in ('account.research_coverage', 'account.portfolio_overview', 'research.company_overview'):
            assert (name in names) == (account_enabled and role == 'assistant')
        response = client.post('/tools/account.research_coverage', json={}, headers=header(token))
        expected = (409 if role == 'assistant' else 403) if account_enabled else 404
        assert response.status_code == expected
        if expected == 409:
            assert response.json()['error']['code'] == 'conflict'
    conn = db.connect(path)
    conn.execute('DELETE FROM api_keys WHERE role=?', ('research',))
    conn.close()
    assert client.get('/tools', headers=header(tokens['research'])).status_code == 401


def test_method_search_refuses_owner_and_blank_query(setup):
    _, tokens, client = setup
    endpoint = '/tools/research.methods'
    assert client.post(endpoint, json={'query': '低PE'}, headers=header(tokens['owner'])).status_code == 403
    invalid = client.post(endpoint, json={'query': '  '}, headers=header(tokens['research']))
    assert invalid.status_code == 422
    assert 'query' in invalid.json()['error']['message']


def test_validation_errors_identify_field_and_reason(setup):
    _, tokens, client = setup
    headers = header(tokens['assistant'])
    for payload, path, reason in (({'report_id': True}, 'report_id', 'integer'),
                                  ([], 'body', 'JSON 对象')):
        response = client.post('/tools/research.report', json=payload, headers=headers)
        assert response.status_code == 422
        error = response.json()['error']
        assert error['code'] == 'invalid_arguments'
        assert path in error['message'] and reason in error['message']
    assert client.post('/tools/missing', json={}, headers=headers).status_code == 404
    assert client.post('/tools/research.report', json={'report_id': 999}, headers=headers).status_code == 404


def test_service_connection_enforces_declared_access_with_account_disabled(setup, monkeypatch):
    _, tokens, client = setup
    class Submission(Arguments):
        question: str

    def submit(ctx, args):
        run_id = service.submit(ctx.connection, '600519', 'lost-http-response', question=args.question)
        return Payload(data={'run_id': run_id})

    tool = replace(TOOLS['research.cards'], name='test.submit', arguments=Submission, handler=submit)
    monkeypatch.setitem(TOOLS, tool.name, tool)
    headers = header(tokens['research'])
    args = {'question': '现金流'}
    response = client.post('/tools/test.submit', json=args, headers=headers)
    assert response.status_code == 503
    monkeypatch.setitem(TOOLS, tool.name, replace(tool, access='write'))
    saved = client.post('/tools/test.submit', json=args, headers=headers)
    assert saved.status_code == 200
    assert client.post('/tools/test.submit', json=args, headers=headers).json() == saved.json()
    conflict = client.post('/tools/test.submit', json={'question': '估值'}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'conflict'


def test_in_process_dispatch_cannot_bypass_permissions(setup):
    path, tokens, _ = setup
    conn = db.connect(path)
    try:
        ctx = Context(conn, path, api_keys.authenticate(conn, tokens['research']), account_shadow_enabled=True)
        with pytest.raises(ToolError, match='凭据'):
            invoke(ctx, 'account.research_coverage', {})
        revoked = api_keys.authenticate(conn, tokens['assistant'])
        conn.execute('DELETE FROM api_keys WHERE id=?', (revoked.key_id,))
        with pytest.raises(ToolError, match='撤销') as error:
            invoke(Context(conn, path, revoked, account_shadow_enabled=True), 'account.research_coverage', {})
        assert error.value.status == 403
    finally:
        conn.close()


@pytest.mark.parametrize('error', [service.RequestError('暂时无法连接巨潮资讯，请稍后重试', 503),
                                   UpstreamConnectionError('connection unavailable')])
def test_upstream_failure_is_retryable(setup, monkeypatch, error):
    _, tokens, client = setup

    def unavailable(conn):
        raise error

    monkeypatch.setattr(service, 'company_cards', unavailable)
    response = client.post('/tools/research.cards', headers=header(tokens['research']), json={})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'unavailable'
    assert '重试' in response.json()['error']['message']


def test_invoke_rechecks_grant_revoked_after_authentication(setup):
    path, tokens, _ = setup
    conn = db.connect(path)
    try:
        principal = api_keys.authenticate(conn, tokens['research'])
        api_keys.revoke(conn, principal.key_id)
        with pytest.raises(ToolError, match='撤销') as error:
            invoke(Context(conn, path, principal), 'research.cards', {})
        assert error.value.status == 403
    finally:
        conn.close()
