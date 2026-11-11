"""External submission is opt-in and cannot change the read-only tools boundary."""
import json

from fastapi.testclient import TestClient

from a_share_research import api_keys, db
from a_share_research.tools.server import create_app
from support.reports import MATERIAL, report


def test_staging_disabled_by_default_and_ownership_is_enforced(tmp_path):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'external')
    other = api_keys.create(conn, 'other', role='assistant')
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    conn.close()
    headers = {'Authorization': 'Bearer ' + token}
    with TestClient(create_app(path)) as client:
        assert all(not t['name'].startswith('external.') for t in client.get('/tools', headers=headers).json()['tools'])
        assert client.post('/tools/external.start', headers=headers,
                           json={'request_id': 'x', 'source_run_id': source}).status_code == 404
    with TestClient(create_app(path, external_enabled=True)) as client:
        response = client.post('/tools/external.start', headers=headers,
                               json={'request_id': 'x', 'source_run_id': source})
        assert response.status_code == 200
        eid = response.json()['result']['data']['id']
        assert client.post('/tools/external.material', headers={'Authorization': 'Bearer ' + other},
                           json={'external_id': eid}).status_code == 404
        # A caller cannot upload its own fake material or self-assert a blind identity.
        bad = client.post('/tools/external.submit', headers=headers,
                          json={'external_id': eid, 'expected_version': 0, 'draft': report(), 'material': {}})
        assert bad.status_code == 422
        response = client.post('/tools/external.submit', headers=headers,
                               json={'external_id': eid, 'expected_version': 0, 'draft': report()})
        assert response.status_code == 200
        receipt = response.json()['result']['data']
        assert receipt['accepted'] and receipt['review_status'] == 'not_queued'
        malformed = client.post('/tools/external.submit', headers=headers, content='not JSON')
        assert malformed.status_code == 422 and malformed.json()['error']['code'] == 'invalid_arguments'
    conn = db.connect(path)
    assert conn.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM reports').fetchone()[0] == 0
    assert json.loads(conn.execute('SELECT material FROM external_research').fetchone()[0]) == json.loads(json.dumps(MATERIAL.snapshot()))
    conn.close()


def test_review_queue_requires_separate_enable_and_preserves_request_identity(tmp_path):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'external')
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    conn.close()
    headers = {'Authorization': 'Bearer ' + token}
    with TestClient(create_app(path, external_enabled=True)) as client:
        item = client.post('/tools/external.start', headers=headers,
                           json={'request_id': 'queue', 'source_run_id': source}).json()['result']['data']
        receipt = client.post('/tools/external.submit', headers=headers,
                              json={'external_id': item['id'], 'expected_version': 0, 'draft': report()}).json()['result']['data']
        args = {'submission_id': receipt['submission_id']}
        assert client.post('/tools/external.review', headers=headers, json=args).status_code == 409
    with TestClient(create_app(path, external_enabled=True, external_review_enabled=True)) as client:
        queued = client.post('/tools/external.review', headers=headers, json=args)
        assert queued.status_code == 200
        assert queued.json()['result']['data']['status'] == 'submitted'
        assert client.post('/tools/external.review', headers=headers, json=args).json() == queued.json()
        status = client.post('/tools/external.status', headers=headers, json={'external_id': item['id']})
        assert status.json()['result']['data']['receipts'][0]['review']['run_id'] == queued.json()['result']['data']['run_id']


def test_acquisition_transport_optin_roles_ownership_retry_and_typed_arguments(tmp_path):
    """Transport contracts only; this test queues work and performs no network acquisition."""
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, 'structural-' + role, role=role)
              for role in ('research', 'assistant', 'owner')}
    tokens['other'] = api_keys.create(conn, 'structural-other')
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    def headers(role='research'):
        return {'Authorization': 'Bearer ' + tokens[role]}
    args = {'request_id': 'fresh', 'code': '600519'}
    with TestClient(create_app(path)) as client:
        for name, body in (('open', args), ('operation', {'operation_id': 1}), ('tools', {})):
            assert client.post('/tools/external.' + name, headers=headers(), json=body).status_code == 404
    with TestClient(create_app(path, external_enabled=True)) as client:
        catalog = client.post('/tools/external.tools', headers=headers(), json={})
        assert catalog.status_code == 200
        assert any(t['name'] == 'calculate' for t in catalog.json()['result']['data']['tools'])
        assert client.post('/tools/external.open', headers=headers('owner'), json=args).status_code == 403
        opened = client.post('/tools/external.open', headers=headers(), json=args)
        assert opened.status_code == 200
        operation = opened.json()['result']['data']
        assert operation['status'] == 'queued' and not operation['account_aware']
        assert client.post('/tools/external.open', headers=headers(), json=args).json() == opened.json()
        assert client.post('/tools/external.open', headers=headers(), json=args | {'code': '600938'}).status_code == 409
        assert client.post('/tools/external.start', headers=headers(),
                           json={'request_id': 'fresh', 'source_run_id': source}).status_code == 409
        for role in ('other', 'assistant'):
            assert client.post('/tools/external.operation', headers=headers(role),
                               json={'operation_id': operation['id']}).status_code == 404
        assert client.post('/tools/external.operation', headers=headers(),
                           json={'operation_id': operation['id']}).json() == opened.json()
        aware = client.post('/tools/external.open', headers=headers('assistant'), json=args)
        assert aware.status_code == 200 and aware.json()['result']['data']['account_aware'] is True
        for malformed in (args | {'code': 600519}, args | {'code': '600519.SH'}, args | {'material': {}},
                          args | {'account_aware': False}):
            assert client.post('/tools/external.open', headers=headers(), json=malformed).status_code == 422
        assert client.post('/tools/external.operation', headers=headers(), json={'operation_id': True}).status_code == 422
        key_id = api_keys.authenticate(conn, tokens['research']).key_id
        conn.execute('DELETE FROM api_keys WHERE id=?', (key_id,))
        assert client.post('/tools/external.operation', headers=headers(),
                           json={'operation_id': operation['id']}).status_code == 401
    assert conn.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM calls').fetchone()[0] == 0
    conn.close()


def test_acquisition_transport_busy_and_terminal_failure_require_explicit_new_key(tmp_path):
    """Captured held material; interrupted worker state below is explicitly structural."""
    from a_share_research import external_operations
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'structural-transport')
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    headers = {'Authorization': 'Bearer ' + token}
    with TestClient(create_app(path, external_enabled=True)) as client:
        item = client.post('/tools/external.start', headers=headers,
                           json={'request_id': 'held', 'source_run_id': source}).json()['result']['data']
        assert client.post('/tools/external.open', headers=headers,
                           json={'request_id': 'held', 'code': '600519'}).status_code == 409
        args = {'request_id': 'read', 'external_id': item['id'], 'base_revision': 0,
                'tool': 'read_disclosure', 'arguments': {'source': 'D0', 'page': 9, 'count': 1}}
        for malformed in (args | {'base_revision': True}, args | {'tool': 'shell'},
                          args | {'arguments': {'source': 'D0', 'count': True}},
                          args | {'arguments': {'source': 'D0', 'path': '/tmp/secret'}}):
            assert client.post('/tools/external.acquire', headers=headers, json=malformed).status_code == 422
        pending = client.post('/tools/external.acquire', headers=headers, json=args)
        assert pending.status_code == 200
        assert client.post('/tools/external.acquire', headers=headers, json=args).json() == pending.json()
        assert client.post('/tools/external.acquire', headers=headers,
                           json=args | {'request_id': 'concurrent'}).status_code == 409
        assert client.post('/tools/external.submit', headers=headers,
                           json={'external_id': item['id'], 'expected_version': 0,
                                 'material_revision': 0, 'draft': report()}).status_code == 409
        operation_id = pending.json()['result']['data']['id']
        conn.execute("UPDATE external_operations SET status='running',started_at=?,deadline=? WHERE id=?",
                     (db.now(), '2099-01-01T00:00:00+08:00', operation_id))
        external_operations.recover(conn)
        failed = client.post('/tools/external.acquire', headers=headers, json=args)
        assert failed.status_code == 200
        result = failed.json()['result']['data']
        assert result['id'] == operation_id and result['status'] == 'failed' and result['error'] == 'interrupted'
        assert client.post('/tools/external.acquire', headers=headers, json=args).json() == failed.json()
        retry = client.post('/tools/external.acquire', headers=headers, json=args | {'request_id': 'explicit-retry'})
        assert retry.status_code == 200
        assert retry.json()['result']['data']['status'] == 'queued'
        assert retry.json()['result']['data']['id'] != operation_id
    assert conn.execute('SELECT count(*) FROM external_materials').fetchone()[0] == 1
    conn.close()
