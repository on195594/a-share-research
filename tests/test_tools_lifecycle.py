"""Structural operations/HTTP contracts only; not broker or investment evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db
from a_share_research.tools import server


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, 'structural-lifecycle-' + role, role=role)
              for role in ('research', 'assistant')}
    conn.close()
    return path, tokens


def header(token):
    return {'Authorization': 'Bearer ' + token}


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
                                    'scan_requests', 'oauth_tokens', 'account_nav_refs'])
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
    monkeypatch.setattr(server, 'invoke', fail)
    with TestClient(app) as client:
        for _ in range(4):
            response = client.post('/tools/research_cards', json={}, headers=header(tokens['research']))
            assert response.status_code == 503
            assert response.json()['error']['code'] == 'unavailable'
            assert client.get('/ready').json()['in_flight'] == 0
            assert client.get('/tools', headers=header(tokens['research'])).status_code == 200
        assert client.get('/tools', headers=header('invalid')).status_code == 401
        assert client.get('/ready').json()['in_flight'] == 0


def test_research_key_has_no_http_lifecycle_control_routes(database):
    path, tokens = database
    with TestClient(server.create_app(path)) as client:
        for endpoint in ('/pause', '/resume', '/drain', '/control', '/control/pause', '/control/resume'):
            assert client.post(endpoint, json={}, headers=header(tokens['research'])).status_code == 404
        assert client.get('/ready').status_code == 200


def test_full_gate_admits_reads_and_discovery_but_rejects_writes_and_collectors(database, monkeypatch):
    path, tokens = database
    app = server.create_app(path, account_shadow_enabled=True)
    headers = header(tokens['assistant'])
    writing, release = threading.Barrier(3), threading.Event()
    evidence = server.TOOLS['account_evidence']

    def slow_write(ctx, args):
        writing.wait(timeout=5)
        assert release.wait(5)
        return evidence.handler(ctx, args)

    monkeypatch.setitem(server.TOOLS, evidence.name, replace(evidence, handler=slow_write))
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        writes = [pool.submit(client.post, '/tools/account_evidence', headers=headers,
                              json={'content_base64': content}) for content in ('YQ==', 'Yg==')]
        try:
            writing.wait(timeout=5)
            assert app.state.gate.status()['in_flight'] == 2
            for token, status in (('invalid', 401), (tokens['research'], 403)):
                rejected = client.post('/tools/account_evidence', headers=header(token),
                                       json={'content_base64': 'Yw=='})
                assert rejected.status_code == status
            assert client.get('/tools', headers=headers).status_code == 200
            assert client.post('/tools/research_cards', json={}, headers=headers).status_code == 200
            collecting = replace(server.TOOLS['research_cards'], collects=True)
            monkeypatch.setitem(server.TOOLS, collecting.name, collecting)
            for name in (evidence.name, collecting.name):
                response = client.post('/tools/' + name, json={}, headers=headers)
                assert response.status_code == 503
                assert response.json()['error']['code'] == 'busy'
        finally:
            release.set()
        assert all(write.result().status_code == 200 for write in writes)


@pytest.mark.parametrize('request_kind', ['discovery', 'call'])
def test_pause_rejects_new_work_and_drain_waits_for_inflight_completion(database, monkeypatch, request_kind):
    from a_share_research.tools.control import Gate, ControlError
    path, tokens = database
    gate = Gate()
    app = server.create_app(path, gate=gate)
    assert app.state.gate is gate
    entered, release = threading.Event(), threading.Event()
    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Structural blocked request did not receive release'
        return [] if request_kind == 'discovery' else {'structural': 'finished'}
    monkeypatch.setattr(server, 'discover' if request_kind == 'discovery' else 'invoke', blocking)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        def request():
            if request_kind == 'discovery':
                return client.get('/tools', headers=header(tokens['research']))
            return client.post('/tools/research_cards', json={}, headers=header(tokens['research']))
        pending = pool.submit(request)
        try:
            assert entered.wait(3)
            assert client.get('/ready').json()['in_flight'] == 1
            with pytest.raises(ControlError):
                gate.pause(timeout=0)
            assert gate.status() == dict(paused=True, in_flight=1)
            draining = pool.submit(gate.pause, timeout=4)
            assert not draining.done()
            for method, endpoint in (('get', '/tools'), ('post', '/tools/research_cards')):
                kwargs = dict(headers=header(tokens['research']))
                if method == 'post':
                    kwargs['json'] = {}
                response = getattr(client, method)(endpoint, **kwargs)
                assert response.status_code == 503
                assert response.json()['error']['code'] == 'paused'
            readiness = client.get('/ready')
            assert readiness.status_code == 503 and readiness.json()['status'] == 'not-ready'
            assert readiness.json()['paused'] is True
            assert client.get('/health').status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 200
        assert draining.result(timeout=3) == dict(paused=True, in_flight=0)
        assert gate.resume() == dict(paused=False, in_flight=0)
        assert client.get('/ready').status_code == 200
        assert request().status_code == 200
        assert gate.status()['in_flight'] == 0


def test_unhandled_tool_failure_also_releases_gate(database, monkeypatch):
    path, tokens = database
    app = server.create_app(path)
    def fail(*args, **kwargs):
        raise RuntimeError('Injected structural unexpected failure')
    monkeypatch.setattr(server, 'invoke', fail)
    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match='structural'):
            client.post('/tools/research_cards', json={}, headers=header(tokens['research']))
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
