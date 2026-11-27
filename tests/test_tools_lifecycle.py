"""Structural operations/HTTP contracts only; not broker or investment evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db
from a_share_research.tools import remote_mcp, server
from support.mcp import ORIGIN, RESOURCE, call_tool, list_tools, rpc, tool_data, tool_error


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, 'structural-lifecycle-' + role, role=role)
              for role in ('research', 'assistant')}
    conn.close()
    return path, tokens


@pytest.mark.parametrize('options,mode', [({}, 'read-only'),
    ({'account_shadow_enabled': True}, 'account-shadow')])
def test_ready_checks_schema_without_writing_or_migrating(database, options, mode):
    path, _ = database
    before = path.read_bytes()
    with TestClient(server.create_app(path, **options)) as client:
        response = client.get('/ready')
        assert response.status_code == 200
        assert response.json() == dict(status='ready', mode=mode,
            schema_version=len(db.MIGRATIONS), paused=False, in_flight=0, database_ready=True)
        assert client.get('/health').status_code == 200
    assert path.read_bytes() == before


@pytest.mark.parametrize('damage', ['missing', 'version', 'api_keys', 'account_journal', 'account_analysis_history',
                                    'scan_requests', 'oauth_tokens', 'account_nav_refs', 'scheduled_runs'])
def test_not_ready_never_creates_or_repairs_database(database, damage):
    path, _ = database
    if damage == 'missing':
        path.unlink()
    else:
        with sqlite3.connect(path) as conn:
            if damage == 'version':
                conn.execute(f'PRAGMA user_version={len(db.MIGRATIONS)-1}')
            else:
                conn.execute(f'DROP TABLE {damage}')
    before = path.read_bytes() if path.exists() else None
    with TestClient(server.create_app(path, account_shadow_enabled=True)) as client:
        response = client.get('/ready')
        assert response.status_code == 503
        assert response.json()['status'] == 'not-ready'
        assert client.get('/health').status_code == 200
    assert (path.read_bytes() if path.exists() else None) == before


def test_errors_release_gate_and_capacity_slots(database, monkeypatch):
    path, tokens = database
    app = server.create_app(path)
    def fail(ctx, name, arguments):
        raise sqlite3.OperationalError('Injected structural failure')
    monkeypatch.setattr(remote_mcp, 'invoke', fail)
    with TestClient(app, base_url=ORIGIN) as client:
        for _ in range(4):
            assert tool_error(client, tokens['research'], 'research_cards')['code'] == 'unavailable'
            assert client.get('/ready').json()['in_flight'] == 0
            assert list_tools(client, tokens['research'])
        assert rpc(client, 'invalid', 'tools/list').status_code == 401
        assert client.get('/ready').json()['in_flight'] == 0


def test_research_key_has_no_http_lifecycle_control_routes(database):
    path, tokens = database
    with TestClient(server.create_app(path), base_url=ORIGIN) as client:
        for endpoint in ('/pause', '/resume', '/drain', '/control', '/control/pause', '/control/resume'):
            assert client.post(endpoint, json={}, headers={'Authorization': 'Bearer ' + tokens['research']}).status_code == 404
        assert client.get('/ready').status_code == 200


def test_full_gate_admits_reads_and_discovery_but_rejects_writes_and_collectors(database, monkeypatch):
    path, tokens = database
    app = server.create_app(path, account_shadow_enabled=True)
    token = tokens['assistant']
    writing, release = threading.Barrier(3), threading.Event()
    evidence = server.TOOLS['account_evidence']

    def slow_write(ctx, args):
        writing.wait(timeout=5)
        assert release.wait(5)
        return evidence.handler(ctx, args)

    monkeypatch.setitem(server.TOOLS, evidence.name, replace(evidence, handler=slow_write))
    with TestClient(app, base_url=ORIGIN) as client, ThreadPoolExecutor(max_workers=2) as pool:
        writes = [pool.submit(call_tool, client, token, evidence.name, {'content_base64': content})
                  for content in ('YQ==', 'Yg==')]
        try:
            writing.wait(timeout=5)
            assert app.state.gate.status()['in_flight'] == 2
            arguments = {'content_base64': 'Yw=='}
            assert rpc(client, 'invalid', 'tools/call', {'name': evidence.name, 'arguments': arguments}).status_code == 401
            assert tool_error(client, tokens['research'], evidence.name, arguments)['code'] == 'forbidden'
            assert list_tools(client, token)
            tool_data(client, token, 'research_cards')
            collecting = replace(server.TOOLS['research_cards'], collects=True)
            monkeypatch.setitem(server.TOOLS, collecting.name, collecting)
            for name in (evidence.name, collecting.name):
                assert tool_error(client, token, name)['code'] == 'busy'
        finally:
            release.set()
        assert all(not write.result()['result'].get('isError') for write in writes)


@pytest.mark.parametrize('request_kind', ['discovery', 'call'])
def test_pause_rejects_new_work_and_drain_waits_for_inflight_completion(database, monkeypatch, request_kind):
    from a_share_research.tools.control import Gate, ControlError
    path, tokens = database
    token = tokens['research']
    gate = Gate()
    app = server.create_app(path, gate=gate)
    assert app.state.gate is gate
    entered, release = threading.Event(), threading.Event()
    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Structural blocked request did not receive release'
        return [] if request_kind == 'discovery' else {'structural': 'finished'}
    monkeypatch.setattr(remote_mcp, 'visible_tools' if request_kind == 'discovery' else 'invoke', blocking)
    with TestClient(app, base_url=ORIGIN) as client, ThreadPoolExecutor(max_workers=2) as pool:
        def request():
            if request_kind == 'discovery':
                return rpc(client, token, 'tools/list')
            return rpc(client, token, 'tools/call', {'name': 'research_cards', 'arguments': {}})
        pending = pool.submit(request)
        try:
            assert entered.wait(3)
            assert client.get('/ready').json()['in_flight'] == 1
            with pytest.raises(ControlError):
                gate.pause(timeout=0)
            assert gate.status() == dict(paused=True, in_flight=1)
            draining = pool.submit(gate.pause, timeout=4)
            assert not draining.done()
            listing = rpc(client, token, 'tools/list')
            assert listing.status_code == 503 and listing.json()['error']['data']['code'] == 'paused'
            assert tool_error(client, token, 'research_cards')['code'] == 'paused'
            readiness = client.get('/ready')
            assert readiness.status_code == 503 and readiness.json()['status'] == 'not-ready'
            assert readiness.json()['paused'] is True
            assert client.get('/health').status_code == 200
        finally:
            release.set()
        finished = pending.result(timeout=3)
        assert finished.status_code == 200 and 'result' in finished.json()
        assert not finished.json()['result'].get('isError')
        assert draining.result(timeout=3) == dict(paused=True, in_flight=0)
        assert gate.resume() == dict(paused=False, in_flight=0)
        assert client.get('/ready').status_code == 200
        resumed = request()
        body = resumed.json()['result']
        assert resumed.status_code == 200 and ('tools' in body if request_kind == 'discovery' else not body.get('isError'))
        assert gate.status()['in_flight'] == 0


def test_unhandled_tool_failure_also_releases_gate(database, monkeypatch, caplog):
    path, tokens = database
    app = server.create_app(path)
    def fail(*args, **kwargs):
        raise RuntimeError('Injected structural unexpected failure')
    monkeypatch.setattr(remote_mcp, 'invoke', fail)
    with TestClient(app, base_url=ORIGIN) as client:
        error = tool_error(client, tokens['research'], 'research_cards')
        assert error['code'] == 'service_failure' and 'structural' not in error['message']
        assert any(r.name == remote_mcp.__name__ and r.exc_info for r in caplog.records)
        assert app.state.gate.status()['in_flight'] == 0
        assert client.get('/ready').status_code == 200


def test_main_rejects_invalid_account_flag_before_opening_control_socket(tmp_path):
    import os
    import subprocess
    import sys
    flag = 'ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED'
    environment = {k: v for k, v in os.environ.items() if not k.startswith('ASHARE_TOOLS_')}
    environment.update(DATA_DIR=str(tmp_path), **{flag: 'true'})
    socket_path = tmp_path / 'control.sock'
    result = subprocess.run([sys.executable, '-m', 'a_share_research.tools.server',
        '--control-socket', str(socket_path)], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and flag in result.stderr
    assert not socket_path.exists() and not (tmp_path / 'app.db').exists()
