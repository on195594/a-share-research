"""Durable scan submission identities; queue fixtures are structural, not market evidence."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db, scans, service
from a_share_research.tools.server import create_app
from support.mcp import ORIGIN, RESOURCE, rpc, tool_data, tool_error


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
                                     'cookie:' + 'a' * 64, 'agent:' + 'g' * 64, 'grant:0', 'grant:01', None])
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


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Account-enabled /mcp on a fresh database; each `open_client()` is a new process-level client."""
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)
    path = tmp_path / 'app.db'
    connection = db.connect(path)
    token = api_keys.create(connection, 'hermes-resume-test', role='assistant')

    def open_client():
        return TestClient(create_app(path, account_shadow_enabled=True), base_url=ORIGIN)

    try:
        yield connection, token, open_client
    finally:
        connection.close()


def test_tools_resume_scan_with_same_credential_after_another_scan_starts(tools):
    connection, token, open_client = tools
    key = 'lost-scan-response'
    with open_client() as client:
        scan_id = tool_data(client, token, 'research_scan_request', {'request_id': key})['data']['scan_id']
    connection.execute("UPDATE scans SET status='done',finished_at=? WHERE id=?", (db.now(), scan_id))
    assert scans.submit(connection) != scan_id

    with open_client() as client:
        result = tool_data(client, token, 'research_scan_request', {'request_id': key})['data']
    assert (result['scan_id'], result['request_id'], result['status'], result['path']) == (
        scan_id, key, 'done', f'/scans/{scan_id}')
    mapping, = connection.execute('SELECT requester,scan_id FROM scan_requests')
    assert tuple(mapping) == ('key:' + hashlib.sha256(token.encode()).hexdigest(), scan_id)


def test_retry_after_lost_response_and_bad_requests(tools):
    connection, token, open_client = tools
    args = {'request_id': 'durable-http'}
    with open_client() as client:
        # Deliberately discard the first response; the server has committed the request.
        rpc(client, token, 'tools/call', {'name': 'research_scan_request', 'arguments': args}).close()
    row = connection.execute('SELECT scan_id FROM scan_requests WHERE request_id=?', ('durable-http',)).fetchone()
    assert row is not None
    original = row[0]
    connection.execute("UPDATE scans SET status='done',finished_at=? WHERE id=?", (db.now(), original))
    # A fresh client has only its stable credential and persisted request key.
    with open_client() as retry:
        assert tool_data(retry, token, 'research_scan_request', args)['data']['scan_id'] == original
        assert scans.submit(connection) != original
        assert tool_data(retry, token, 'research_scan_request', args)['data']['scan_id'] == original
        for bad in ('bad key', 'bad\n', 'x' * 129):
            refused = tool_error(retry, token, 'research_scan_request', {'request_id': bad})
            assert refused['code'] == 'invalid_arguments' and 'request_id' in refused['message']
    assert connection.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 1


def test_mcp_rejects_invalid_or_owner_keys_and_legacy_sessions_while_history_replays(tools):
    connection, _, open_client = tools
    call = {'name': 'research_scan_request', 'arguments': {'request_id': 'authenticated'}}
    owner = api_keys.create(connection, 'scan-owner-structural-test', role='owner')
    legacy = 'scan-legacy-session-structural-test'
    requester = 'agent:' + hashlib.sha256(legacy.encode()).hexdigest()
    connection.execute('INSERT INTO sessions VALUES (?,?)', (requester, time.time() + 60))
    with open_client() as client:
        for token in (None, 'invalid', owner, legacy):
            assert rpc(client, token, 'tools/call', call).status_code == 401
    assert connection.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 0
    # Mappings recorded under retired agent sessions still replay their scan.
    scan_id = scans.submit_request(connection, requester, 'authenticated')
    assert scans.submit_request(connection, requester, 'authenticated') == scan_id
    assert connection.execute('SELECT requester FROM scan_requests').fetchone()[0] == requester
