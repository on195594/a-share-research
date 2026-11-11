"""Credential isolation and physical read-only behavior of the new tools boundary."""
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db
from a_share_research.tools.registry import Context, ToolError, invoke
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
        assert ('policy.v0.read' in names) == (role == 'assistant')
        response = client.post('/tools/account.research_coverage', json={}, headers=header(token))
        assert response.status_code == (200 if role == 'assistant' else 403)
    conn = db.connect(path)
    conn.execute('DELETE FROM api_keys WHERE role=?', ('research',))
    conn.close()
    assert client.get('/tools', headers=header(tokens['research'])).status_code == 401


def test_input_rejection_and_no_write_commands(setup):
    _, tokens, client = setup
    headers = header(tokens['assistant'])
    for args in ({'command': 'buy-holding'}, {'command': 'holdings', 'argv': ['--confirm-write']},
                 {'command': 'holdings; touch /tmp/injection'}):
        response = client.post('/tools/policy.v0.read', json=args, headers=headers)
        assert response.status_code == 422
        assert response.json()['error']['code'] == 'invalid_arguments'
    assert client.post('/tools/research.report', json={'report_id': True}, headers=headers).status_code == 422
    assert client.post('/tools/missing', json={}, headers=headers).status_code == 404
    assert client.post('/tools/research.report', json={'report_id': 999}, headers=headers).status_code == 404


def test_policy_subprocess_matches_direct_cli_and_does_not_write(setup):
    path, tokens, client = setup
    before = path.read_bytes()
    direct = subprocess.run([sys.executable, '-m', 'a_share_research.policy.v0.cache', 'holdings', '--json'],
                            env={'POLICY_V0_DB': str(path)}, capture_output=True, text=True, timeout=30)
    response = client.post('/tools/policy.v0.read', json={'command': 'holdings'},
                           headers=header(tokens['assistant']))
    assert response.status_code == 200
    assert response.json()['result'] == {'exit_code': direct.returncode, 'stdout': direct.stdout,
                                         'stderr': direct.stderr}
    assert direct.returncode == 0
    assert path.read_bytes() == before


def test_service_connection_is_physically_readonly(setup, monkeypatch):
    _, tokens, client = setup
    def attempt(ctx, name, arguments):
        ctx.connection.execute("DELETE FROM api_keys")
    monkeypatch.setattr('a_share_research.tools.server.invoke', attempt)
    response = client.post('/tools/research.cards', json={}, headers=header(tokens['research']))
    assert response.status_code == 503
    assert client.get('/tools', headers=header(tokens['research'])).status_code == 200


def test_in_process_dispatch_cannot_bypass_permissions(setup):
    path, tokens, _ = setup
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctx = Context(conn, path, api_keys.authenticate(conn, tokens['research']))
        with pytest.raises(ToolError, match='凭据'):
            invoke(ctx, 'policy.v0.read', {'command': 'holdings'})
    finally:
        conn.close()
