"""Credential isolation and physical database access modes at the tool boundary."""
from dataclasses import replace
import sqlite3

import pytest
from fastapi.testclient import TestClient
from requests import ConnectionError as UpstreamConnectionError

from a_share_research import api_keys, db, service
from a_share_research.tools.registry import Arguments, Context, Payload, TOOLS, ToolError, invoke
from a_share_research.tools.server import create_app


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, role, role=role) for role in ('research', 'assistant', 'owner')}
    conn.close()
    with TestClient(create_app(path)) as client:
        yield path, tokens, client


def header(token):
    return {'Authorization': 'Bearer ' + token}


def test_discovery_and_call_enforce_same_scope(setup):
    path, tokens, client = setup
    assert client.get('/tools').status_code == 401
    for role, token in tokens.items():
        names = {t['name'] for t in client.get('/tools', headers=header(token)).json()['tools']}
        assert ('research.cards' in names) == (role != 'owner')
        assert 'account.research_coverage' not in names
        response = client.post('/tools/account.research_coverage', json={}, headers=header(token))
        assert response.status_code == 404
    with TestClient(create_app(path, account_shadow_enabled=True)) as enabled:
        for role, token in tokens.items():
            names = {t['name'] for t in enabled.get('/tools', headers=header(token)).json()['tools']}
            assert ('account.research_coverage' in names) == (role == 'assistant')
            response = enabled.post('/tools/account.research_coverage', json={}, headers=header(token))
            assert response.status_code == (200 if role == 'assistant' else 403)
    conn = db.connect(path)
    conn.execute('DELETE FROM api_keys WHERE role=?', ('research',))
    conn.close()
    assert client.get('/tools', headers=header(tokens['research'])).status_code == 401


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
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctx = Context(conn, path, api_keys.authenticate(conn, tokens['research']), account_shadow_enabled=True)
        with pytest.raises(ToolError, match='凭据'):
            invoke(ctx, 'account.research_coverage', {})
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
