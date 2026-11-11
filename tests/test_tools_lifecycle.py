"""Structural operations/HTTP contracts only; not broker or investment evidence."""
from concurrent.futures import ThreadPoolExecutor
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
    ({'external_enabled': True}, 'external-staging'),
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


@pytest.mark.parametrize('damage', ['missing', 'version', 'api_keys', 'account_schema', 'scan_requests'])
def test_not_ready_never_creates_or_repairs_database(database, damage):
    path, _ = database
    if damage == 'missing':
        path.unlink()
    else:
        with sqlite3.connect(path) as conn:
            if damage == 'version':
                conn.execute(f'PRAGMA user_version={len(db.MIGRATIONS)-1}')
            elif damage == 'api_keys':
                conn.execute('DROP TABLE api_keys')
            elif damage == 'scan_requests':
                conn.execute('DROP TABLE scan_requests')
            else:
                conn.execute('DROP TABLE account_journal')
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
            response = client.post('/tools/research.cards', json={}, headers=header(tokens['research']))
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
            return client.post('/tools/research.cards', json={}, headers=header(tokens['research']))
        pending = pool.submit(request)
        try:
            assert entered.wait(3)
            assert client.get('/ready').json()['in_flight'] == 1
            with pytest.raises(ControlError):
                gate.pause(timeout=0)
            assert gate.status() == dict(paused=True, in_flight=1)
            draining = pool.submit(gate.pause, timeout=4)
            assert not draining.done()
            for method, endpoint in (('get', '/tools'), ('post', '/tools/research.cards')):
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
            client.post('/tools/research.cards', json={}, headers=header(tokens['research']))
        assert app.state.gate.status()['in_flight'] == 0
        assert client.get('/ready').status_code == 200


@pytest.mark.parametrize('table,options', [
    ('external_submissions', dict(external_enabled=True)),
    ('external_reviews', dict(external_enabled=True, external_review_enabled=True)),
    ('account_analysis_history', dict(account_shadow_enabled=True)),
])
def test_ready_requires_schema_for_each_enabled_tool_family(database, table, options):
    path, _ = database
    with sqlite3.connect(path) as conn:
        conn.execute(f'DROP TABLE {table}')
    before = path.read_bytes()
    with TestClient(server.create_app(path, **options)) as client:
        response = client.get('/ready')
        assert response.status_code == 503
        assert response.json()['status'] == 'not-ready'
    assert path.read_bytes() == before


@pytest.mark.parametrize('flag', ['ASHARE_TOOLS_EXTERNAL_ENABLED', 'ASHARE_TOOLS_EXTERNAL_REVIEW_ENABLED',
                                  'ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED'])
def test_main_rejects_invalid_runtime_flags_before_opening_control_socket(tmp_path, flag):
    import os
    import subprocess
    import sys
    environment = {k: v for k, v in os.environ.items() if not k.startswith('ASHARE_TOOLS_')}
    environment.update(DATA_DIR=str(tmp_path), **{flag: 'true'})
    socket_path = tmp_path / 'control.sock'
    result = subprocess.run([sys.executable, '-m', 'a_share_research.tools.server',
        '--control-socket', str(socket_path)], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and flag in result.stderr
    assert not socket_path.exists() and not (tmp_path / 'app.db').exists()


def test_main_rejects_review_without_external_mode_before_serving(tmp_path):
    import os
    import subprocess
    import sys
    environment = {k: v for k, v in os.environ.items() if not k.startswith('ASHARE_TOOLS_')}
    environment.update(DATA_DIR=str(tmp_path), ASHARE_TOOLS_EXTERNAL_REVIEW_ENABLED='1')
    socket_path = tmp_path / 'control.sock'
    result = subprocess.run([sys.executable, '-m', 'a_share_research.tools.server',
        '--control-socket', str(socket_path)], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and '外部复核' in result.stderr
    assert not socket_path.exists() and not (tmp_path / 'app.db').exists()
