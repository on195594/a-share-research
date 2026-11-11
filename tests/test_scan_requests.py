"""Durable scan submission identities; queue fixtures are structural, not market evidence."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import threading
import time

import pytest
import requests

from a_share_research import api_keys, db, scans, service


REQUESTER = 'key:' + 'a' * 64
OTHER_REQUESTER = 'agent:' + 'b' * 64


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'scan-requests.db')
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'failed'])
def test_retry_returns_original_scan_in_every_status(conn, status):
    scan_id = scans.submit_request(conn, REQUESTER, 'durable-1')
    conn.execute('UPDATE scans SET status=? WHERE id=?', (status, scan_id))
    before = [tuple(row) for row in conn.execute('SELECT * FROM scan_requests')]
    assert scans.submit_request(conn, REQUESTER, 'durable-1') == scan_id
    assert conn.execute('SELECT count(*) FROM scans').fetchone()[0] == 1
    assert [tuple(row) for row in conn.execute('SELECT * FROM scan_requests')] == before


def test_identity_scope_and_active_scan_reuse_are_independent(conn):
    original = scans.submit_request(conn, REQUESTER, 'same-key')
    conn.execute("UPDATE scans SET status='done' WHERE id=?", (original,))
    other = scans.submit_request(conn, OTHER_REQUESTER, 'same-key')
    assert other != original
    # An unrelated active scan must not replace the original request's terminal result.
    assert scans.submit_request(conn, REQUESTER, 'same-key') == original
    assert scans.submit_request(conn, REQUESTER, 'next-key') == other
    assert scans.submit(conn) == other  # Legacy submissions retain single-active semantics.
    assert conn.execute('SELECT count(*) FROM scans').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 3


@pytest.mark.parametrize('active', [False, True])
def test_mapping_failure_rolls_back_without_orphaning_a_scan(conn, active):
    if active:
        scans.submit(conn)
    before = [tuple(row) for row in conn.execute('SELECT * FROM scans')]
    conn.execute("CREATE TRIGGER reject_request BEFORE INSERT ON scan_requests "
                 "BEGIN SELECT RAISE(ABORT, 'injected mapping failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected mapping failure'):
        scans.submit_request(conn, REQUESTER, 'rejected')
    assert not conn.in_transaction
    assert [tuple(row) for row in conn.execute('SELECT * FROM scans')] == before
    assert conn.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 0


def test_concurrent_requests_and_restart_recover_a_completed_scan(tmp_path):
    path = tmp_path / 'concurrent.db'
    initial = db.connect(path)
    initial.close()
    barrier = threading.Barrier(2)

    def submit(_):
        connection = db.connect(path)
        try:
            barrier.wait(timeout=10)
            return scans.submit_request(connection, REQUESTER, 'lost-response')
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, range(2)))
    assert ids[0] == ids[1]
    connection = db.connect(path)
    connection.execute("UPDATE scans SET status='done',finished_at=? WHERE id=?", (db.now(), ids[0]))
    connection.close()
    # A new connection represents the next client invocation after the response was lost.
    recovered = db.connect(path)
    try:
        assert scans.submit_request(recovered, REQUESTER, 'lost-response') == ids[0]
        assert recovered.execute('SELECT count(*) FROM scans').fetchone()[0] == 1
        assert recovered.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 1
    finally:
        recovered.close()


@pytest.mark.parametrize('request_id', ['', 'x' * 129, '-first', 'has space', 'slash/key', '中文', 'line\n', None, 1])
def test_invalid_request_keys_do_not_write(conn, request_id):
    with pytest.raises(service.RequestError) as exc:
        scans.submit_request(conn, REQUESTER, request_id)
    assert exc.value.status == 400
    assert conn.execute('SELECT count(*) FROM scans').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 0


@pytest.mark.parametrize('requester', ['', 'key:' + 'a' * 63, 'key:' + 'A' * 64,
                                     'cookie:' + 'a' * 64, 'agent:' + 'g' * 64, None])
def test_invalid_requesters_do_not_write(conn, requester):
    with pytest.raises(service.RequestError) as exc:
        scans.submit_request(conn, requester, 'valid-key')
    assert exc.value.status == 400
    assert conn.execute('SELECT count(*) FROM scans').fetchone()[0] == 0


def test_request_key_boundary_and_punctuation(conn):
    key = 'A' + '._:-0' * 25 + 'xy'
    assert len(key) == 128
    scan_id = scans.submit_request(conn, OTHER_REQUESTER, key)
    assert scans.submit_request(conn, OTHER_REQUESTER, key) == scan_id


def test_api_retry_after_lost_response_and_bad_requests(server):
    connection, client, url, *_ = server
    path = url + '/api/scan-requests/durable-http'
    # Deliberately discard the first response; the server has committed the request.
    client.put(path, timeout=3).close()
    row = connection.execute('SELECT scan_id FROM scan_requests WHERE request_id=?', ('durable-http',)).fetchone()
    assert row is not None
    original = row[0]
    connection.execute("UPDATE scans SET status='done',finished_at=? WHERE id=?", (db.now(), original))
    # A fresh HTTP client has only its stable credential and persisted request key.
    with requests.Session() as retry:
        retry.trust_env = False
        retry.headers['Authorization'] = client.headers['Authorization']
        response = retry.put(path, timeout=3)
        assert response.status_code == 202
        assert response.json() == {'version': service.VERSION, 'request_id': 'durable-http', 'scan_id': original}
        legacy = retry.post(url + '/api/scans', timeout=3)
        assert legacy.status_code == 202 and legacy.json()['scan_id'] != original
        assert retry.put(path, timeout=3).json()['scan_id'] == original
        for bad in ('bad%20key', 'bad%0A', 'x' * 129):
            assert retry.put(url + '/api/scan-requests/' + bad, timeout=3).status_code == 400
    assert connection.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 1


def test_api_auth_supports_legacy_sessions_and_rejects_invalid_or_owner_keys(server):
    connection, client, url, *_ = server
    path = url + '/api/scan-requests/authenticated'
    assert client.put(path, headers={'Authorization': ''}, timeout=3).status_code == 401
    assert client.put(path, headers={'Authorization': 'Bearer invalid'}, timeout=3).status_code == 401
    owner = api_keys.create(connection, 'scan-owner-structural-test', role='owner')
    assert client.put(path, headers={'Authorization': f'Bearer {owner}'}, timeout=3).status_code == 403
    assert connection.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 0
    legacy = 'scan-legacy-session-structural-test'
    requester = 'agent:' + hashlib.sha256(legacy.encode()).hexdigest()
    connection.execute('INSERT INTO sessions VALUES (?,?)', (requester, time.time() + 60))
    response = client.put(path, headers={'Authorization': f'Bearer {legacy}'}, timeout=3)
    assert response.status_code == 202
    assert connection.execute('SELECT requester FROM scan_requests').fetchone()[0] == requester
    connection.execute('UPDATE sessions SET expires_at=0 WHERE token_hash=?', (requester,))
    assert client.put(path, headers={'Authorization': f'Bearer {legacy}'}, timeout=3).status_code == 401
