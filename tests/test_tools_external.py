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
