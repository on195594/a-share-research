"""Real isolated HTTP/SQLite authentication lifecycle; no OAuth/provider mocks."""
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def csrf(client, url):
    page = client.get(url + '/policy')
    return re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]


def register(client, url):
    client.post(url + '/auth/dev', headers={'Origin': url})
    response = client.post(url + '/auth/remember-device', data={'csrf': csrf(client, url)},
                           headers={'Origin': url}, allow_redirects=False)
    assert response.status_code == 303
    return client.cookies.get('remember_device')


def peer(client):
    other = requests.Session()
    other.trust_env = False
    other.cookies.update(client.cookies)
    return other


def test_register_existing_session_and_restore_deep_link(server):
    conn, client, url, *_ = server
    token = register(client, url)
    row = conn.execute('SELECT * FROM remembered_devices').fetchone()
    assert row['token_hash'] == digest(token) and token not in str(tuple(row))
    deadline = row['expires_at']
    assert 30 * 86400 - 5 < deadline - time.time() <= 30 * 86400
    old = client.cookies.get('session')
    conn.execute('UPDATE sessions SET expires_at=0')
    response = client.get(url + '/policy', allow_redirects=False)
    assert response.status_code == 200 and '投资政策' in response.text
    assert client.cookies.get('session') != old
    assert conn.execute('SELECT expires_at FROM remembered_devices').fetchone()[0] == deadline
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=lax' in response.headers['set-cookie']
    assert client.get(url + '/login', allow_redirects=False).headers['location'] == '/'


def test_logout_revokes_device_and_all_its_sessions_only(server):
    conn, a, url, *_ = server
    register(a, url)
    stolen = peer(a)
    stolen.cookies.clear(); stolen.cookies.set('remember_device', a.cookies.get('remember_device'))
    assert stolen.get(url + '/policy').status_code == 200
    b = requests.Session(); b.trust_env = False
    register(b, url)
    response = a.post(url + '/logout', data={'csrf': csrf(a, url)}, headers={'Origin': url}, allow_redirects=False)
    assert response.status_code == 303
    assert len(conn.execute('SELECT * FROM remembered_devices').fetchall()) == 1
    assert stolen.get(url + '/policy', allow_redirects=False).headers['location'] == '/login'
    assert b.get(url + '/policy', allow_redirects=False).status_code == 200


def test_csrf_cross_site_and_api_are_not_bypassed(server):
    conn, client, url, *_ = server
    client.post(url + '/auth/dev', headers={'Origin': url})
    for path in ('/auth/remember-device', '/logout'):
        assert client.post(url + path, headers={'Origin': url}).status_code == 403
        assert client.post(url + path, data={'csrf': '非法'}, headers={'Origin': url}).status_code == 403
        assert client.post(url + path, data={'csrf': csrf(client, url)}, headers={'Origin': 'https://evil.example'}).status_code == 403
    register(client, url)
    conn.execute('UPDATE sessions SET expires_at=0')
    other = peer(client)
    assert other.post(url + '/auth/remember-device', headers={'Origin': url}, allow_redirects=False).headers['location'] == '/login'
    assert other.get(url + '/api/research/budget').status_code == 401
    assert other.post(url + '/logout', headers={'Origin': url}).status_code == 403


def test_expiry_owner_change_and_malformed_tokens(server):
    conn, client, url, *_ = server
    register(client, url)
    conn.execute('UPDATE remembered_devices SET expires_at=?', (time.time() - 1,))
    assert client.get(url + '/policy', allow_redirects=False).headers['location'] == '/login'
    register(client, url)
    conn.execute("UPDATE remembered_devices SET owner_id='different-owner'")
    assert client.get(url + '/policy', allow_redirects=False).headers['location'] == '/login'
    client.cookies.clear(); client.cookies.set('remember_device', 'invalid')
    assert client.get(url + '/policy', allow_redirects=False).headers['location'] == '/login'


def test_concurrent_recovery_and_logout(server):
    conn, client, url, *_ = server
    register(client, url)
    copies = [peer(client) for _ in range(8)]
    for copy in copies:
        copy.cookies.clear(); copy.cookies.set('remember_device', client.cookies.get('remember_device'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(r.status_code == 200 for r in pool.map(lambda c: c.get(url + '/policy'), copies))
    assert len(conn.execute('SELECT * FROM device_sessions').fetchall()) == 9
    valid_csrf = csrf(client, url)
    for copy in copies:
        copy.cookies.clear()
        copy.cookies.set('remember_device', client.cookies.get('remember_device'))
    with ThreadPoolExecutor(max_workers=9) as pool:
        pending = [pool.submit(c.get, url + '/policy', allow_redirects=False) for c in copies]
        response = client.post(url + '/logout', data={'csrf': valid_csrf}, headers={'Origin': url}, allow_redirects=False)
        assert response.status_code == 303
        [p.result() for p in pending]
    assert conn.execute('SELECT COUNT(*) FROM device_sessions').fetchone()[0] == 0
    assert all(c.get(url + '/policy', allow_redirects=False).status_code == 303 for c in copies)


def test_restored_session_never_outlives_device(server):
    conn, client, url, *_ = server
    register(client, url)
    deadline = time.time() + 90
    conn.execute('UPDATE remembered_devices SET expires_at=?', (deadline,))
    conn.execute('UPDATE sessions SET expires_at=0')
    assert client.get(url + '/policy').status_code == 200
    assert conn.execute('SELECT MAX(expires_at) FROM sessions').fetchone()[0] <= deadline


def test_additive_tables_reopen_without_changing_legacy_sessions(server):
    from a_share_research import db
    conn, client, url, *_ = server
    register(client, url)
    path = conn.execute('PRAGMA database_list').fetchone()['file']
    from pathlib import Path
    reopened = db.connect(Path(path))
    assert reopened.execute('PRAGMA user_version').fetchone()[0] == 22
    assert len(reopened.execute('PRAGMA table_info(sessions)').fetchall()) == 2
    reopened.execute('INSERT INTO sessions VALUES (?, ?)', ('legacy-token-hash', time.time() + 600))
    assert reopened.execute('SELECT COUNT(*) FROM remembered_devices').fetchone()[0] == 1
    reopened.close()


def test_https_cookie_attributes(tmp_path):
    # Exercise the route and real database under HTTPS config; no OAuth exchange is simulated.
    import os
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-c', '''
import secrets, time
from starlette.requests import Request
from a_share_research import web
token = secrets.token_urlsafe(32)
web.conn.execute("INSERT INTO sessions VALUES (?, ?)", (web._hash(token), time.time()+600))
request = Request({"type":"http", "method":"POST", "scheme":"https", "path":"/auth/remember-device", "headers":[(b"cookie", ("session="+token).encode())]})
r = web.remember_device(request, csrf=web.csrf_token(web._hash(token)), return_path="/")
assert r.status_code == 303
cookie = r.headers["set-cookie"]
assert all(item in cookie for item in ("Secure", "HttpOnly", "SameSite=lax", "Max-Age=2592000", "Path=/"))
assert "Domain=" not in cookie
'''], env={**os.environ, 'DATA_DIR': str(tmp_path), 'PUBLIC_BASE_URL': 'https://research.example',
           'DEV_LOGIN': '0', 'SESSION_SECRET': 'isolated-https-cookie-validation-only',
           'GITHUB_CLIENT_ID': 'isolated-test', 'GITHUB_CLIENT_SECRET': 'isolated-test',
           'GITHUB_USER_ID': '123'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_registration_returns_to_local_page_only(server):
    conn, client, url, *_ = server
    register(client, url)
    for path, expected in (('/policy', '/policy'), ('https://evil.example', '/'), ('//evil.example', '/'), ('/\\evil.example', '/'), ('/policy?next=evil', '/')):
        response = client.post(url + '/auth/remember-device', data={'csrf': csrf(client, url), 'return_path': path}, headers={'Origin': url}, allow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == expected


def test_lost_registration_response_does_not_leave_live_orphan(server):
    conn, client, url, *_ = server
    token = register(client, url)
    original_session = client.cookies.get('session')
    client.cookies.clear()
    client.cookies.set('session', original_session)
    response = client.post(url + '/auth/remember-device', data={'csrf': csrf(client, url)}, headers={'Origin': url}, allow_redirects=False)
    assert response.status_code == 303
    assert conn.execute('SELECT COUNT(*) FROM remembered_devices').fetchone()[0] == 1
    assert not conn.execute('SELECT 1 FROM remembered_devices WHERE token_hash=?', (digest(token),)).fetchone()
    assert client.get(url + '/policy', allow_redirects=False).status_code == 200


def test_concurrent_registration_tokens_all_revoked_on_logout(server):
    conn, client, url, *_ = server
    client.post(url + '/auth/dev', headers={'Origin': url})
    valid_csrf = csrf(client, url)
    copies = [peer(client) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda c: c.post(url + '/auth/remember-device', data={'csrf': valid_csrf}, headers={'Origin': url}, allow_redirects=False), copies))
    assert all(r.status_code == 303 for r in responses)
    tokens = [c.cookies.get('remember_device') for c in copies]
    assert conn.execute('SELECT COUNT(*) FROM remembered_devices').fetchone()[0] == 1
    assert client.post(url + '/logout', data={'csrf': valid_csrf}, headers={'Origin': url}, allow_redirects=False).status_code == 303
    for c, token in zip(copies, tokens):
        c.cookies.clear(); c.cookies.set('remember_device', token)
        assert c.get(url + '/policy', allow_redirects=False).headers['location'] == '/login'
