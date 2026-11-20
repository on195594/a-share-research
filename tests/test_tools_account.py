"""Transport/authorization tests use explicitly structural account inputs, not broker evidence."""
import base64
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db
from a_share_research.account import evidence, ledger
from a_share_research.account.reconciliation import Opening
from a_share_research.tools.server import create_app


@pytest.mark.parametrize('damage', ['missing', 'unsafe'])
def test_recent_evidence_distinguishes_invalid_limit_from_storage_failure(tmp_path, damage):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'test', role='assistant')
    conn.close()
    directory = tmp_path / 'account-evidence'
    headers = {'Authorization': 'Bearer ' + token}
    with TestClient(create_app(path, account_shadow_enabled=True)) as client:
        upload = client.post('/tools/account_evidence', headers=headers, json={'content_base64': 'YQ=='})
        target = directory / upload.json()['result']['data']['sha256']
        if damage == 'missing':
            target.unlink()
        else:
            target.chmod(0o644)
        invalid = client.post('/tools/account_evidence_recent', headers=headers, json={'limit': 0})
        assert invalid.status_code == 422 and invalid.json()['error']['code'] == 'invalid_arguments'
        unavailable = client.post('/tools/account_evidence_recent', headers=headers, json={'limit': 1})
        assert unavailable.status_code == 503 and unavailable.json()['error']['code'] == 'unavailable'
        assert '核对证据目录' in unavailable.json()['error']['message']


def test_recent_uses_latest_upload_metadata_and_excludes_internal_evidence(tmp_path):
    path = tmp_path / 'app.db'
    directory = tmp_path / 'account-evidence'
    index_capture = (Path(__file__).parent / 'data/account-nav-h00906.json').read_bytes()
    evidence.store(directory, index_capture)
    with closing(db.connect(path)) as conn, TestClient(create_app(path, account_shadow_enabled=True)) as client:
        token = api_keys.create(conn, 'uploads', role='assistant')
        key_id = api_keys.authenticate(conn, token).key_id
        headers = {'Authorization': 'Bearer ' + token}
        payload = {'content_base64': 'YQ==', 'filename': 'original.png', 'label': '第一张'}
        invalid = client.post('/tools/account_evidence', headers=headers, json=payload | {'label': '长' * 81})
        assert invalid.status_code == 422 and invalid.json()['error']['code'] == 'invalid_arguments'
        assert invalid.json()['error']['message'].startswith('label:')
        original = client.post('/tools/account_evidence', headers=headers, json=payload).json()['result']['data']
        sha = original['sha256']
        other = evidence.store(directory, b'original')
        evidence.record_upload(conn, other, key_id, filename='/tmp/\n')
        latest = payload | {'filename': 'C:\\fakepath\\sub/交割\t单.png', 'label': '成交明细'}
        assert client.post('/tools/account_evidence', headers=headers, json=latest).json()['result']['data'] == original

        rows = conn.execute('SELECT key_id, label, uploaded_at FROM account_evidence_uploads WHERE sha256=? ORDER BY id',
                            (sha,)).fetchall()
        assert [(row['key_id'], row['label']) for row in rows] == [(key_id, '第一张'), (key_id, '成交明细')]
        recent = client.post('/tools/account_evidence_recent', headers=headers, json={}).json()['result']['data']
        first, second = recent['evidence']
        assert first == dict(sha256=sha, uploaded_at=rows[-1]['uploaded_at'], filename='交割单.png',
                             label='成交明细', size_bytes=1)
        assert (second['sha256'], second['filename'], second['label']) == (other, None, None)


def test_account_scope_observation_and_duplicate_json_rejection(tmp_path, bind_account):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, role, role=role) for role in ('research', 'assistant')}
    conn.close()
    def headers(role):
        return {'Authorization': 'Bearer ' + tokens[role]}
    payload = {'content_base64': base64.b64encode(b'Structural transport evidence, no financial assertions').decode()}
    with TestClient(create_app(path)) as client:
        assert client.post('/tools/account_evidence', headers=headers('assistant'), json=payload).status_code == 404
    with TestClient(create_app(path, account_shadow_enabled=True)) as client:
        assert client.post('/tools/account_evidence', headers=headers('research'), json=payload).status_code == 403
        response = client.post('/tools/account_evidence', headers=headers('assistant'), json=payload)
        assert response.status_code == 200
        sha = response.json()['result']['data']['sha256']
        assert evidence.verify(tmp_path / 'account-evidence', sha)
        with db.connect(path) as conn:
            principal = api_keys.authenticate(conn, tokens['assistant'])
            ledger.initialize(conn, principal, 'test', Opening(positions={}, cash='100'), datetime(2026, 11, 1),
                              tmp_path / 'account-evidence', sha, tolerance=Decimal(0))
            bind_account(conn, 'test')
        args = dict(evidence_hash=sha,
                    observation={'observed_at':'2026-11-11T15:00:00+08:00','cash':'100'})
        result = client.post('/tools/account_observe', headers=headers('assistant'), json=args)
        assert result.status_code == 200 and result.json()['result']['data']['trades_recorded'] is False
        assert client.post('/tools/account_observe', headers=headers('assistant'),
                           json=args | {'account_id': 'test'}).json() == result.json()
        wrong = client.post('/tools/account_observe', headers=headers('assistant'), json=args | {'account_id': 'other'})
        assert wrong.status_code == 409 and 'account_id' in wrong.json()['error']['message']
        for body in ('{"account_id":"a","account_id":"b"}',
                     '{"account_id":"a","observation":{"cash":1,"cash":2}}',
                     '{"account_id":"a","observation":{"cash":NaN}}'):
            rejected = client.post('/tools/account_observe', headers=headers('assistant'), content=body)
            assert rejected.status_code == 422 and rejected.json()['error']['code'] == 'invalid_arguments'
    conn = db.connect(path)
    assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0] == 0
    conn.close()


def test_account_state_requires_a_unique_binding_even_with_explicit_id(tmp_path, bind_account):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'binding', role='assistant')
    principal = api_keys.authenticate(conn, token)
    directory = tmp_path / 'account-evidence'
    sha = evidence.store(directory, b'Structural account opening')
    ledger.initialize(conn, principal, 'test', Opening(positions={}, cash='100'), datetime(2026, 11, 1),
                      directory, sha, tolerance=Decimal(0))
    with TestClient(create_app(path, account_shadow_enabled=True)) as client:
        def state(arguments):
            return client.post('/tools/account_state', headers={'Authorization': 'Bearer ' + token}, json=arguments)

        unbound = state({'account_id': 'test'})
        assert unbound.status_code == 409 and '绑定' in unbound.json()['error']['message']
        bind_account(conn, 'test')
        assert state({}).json()['result'] == state({'account_id': 'test'}).json()['result']
        # Explicit corruption of the v24 singleton constraint exercises ambiguous bindings.
        conn.execute('PRAGMA ignore_check_constraints=ON')
        conn.execute('INSERT INTO account_source SELECT 2,mode,source_sha256,updated_at,watermark FROM account_source')
        ambiguous = state({})
        assert ambiguous.status_code == 409 and '唯一' in ambiguous.json()['error']['message']
    conn.close()


def test_database_failure_returns_service_failure_and_original_request_can_retry(tmp_path, bind_account):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'test', role='assistant')
    principal = api_keys.authenticate(conn, token)
    sha = evidence.store(tmp_path / 'account-evidence', b'Structural database rollback evidence')
    ledger.initialize(conn, principal, 'test', Opening(positions={},cash='100'), datetime(2026,11,1),
                      tmp_path / 'account-evidence', sha, tolerance=Decimal(0))
    bind_account(conn, 'test')
    conn.execute("CREATE TRIGGER fail_journal BEFORE INSERT ON account_journal BEGIN SELECT RAISE(ABORT,'test'); END")
    args = dict(account_id='test', batch=dict(expected_version=0,evidence_hashes=[sha],
                events=[dict(kind='deposit',cash='5',occurred_at='2026-11-11T15:00:00+08:00')],
                snapshot=dict(positions={},cash='105',market_values={},total_assets='105')))
    headers = {'Authorization':'Bearer '+token}
    with TestClient(create_app(path, account_shadow_enabled=True)) as client:
        snapshot = args['batch']['snapshot'] | {'positions': {'600519': 100}}
        incomplete = args | {'batch': args['batch'] | {'snapshot': snapshot}}
        invalid = client.post('/tools/account_submit', headers=headers, json=incomplete)
        assert invalid.status_code == 422
        assert invalid.json()['error'] == {'code': 'invalid_arguments',
                                           'message': 'batch.snapshot.market_values: 缺 600519 市值'}
        failed = client.post('/tools/account_submit',headers=headers,json=args)
        assert failed.status_code == 200 and failed.json()['result']['data']['status'] == 'service_failure'
        rid = failed.json()['result']['data']['receipt_id']
        read = client.post('/tools/account_receipt',headers=headers,json={'account_id':'test','receipt_id':rid})
        assert read.json()['result']['data'] == failed.json()['result']['data']
        assert conn.execute("SELECT count(*) FROM account_batches WHERE status='posted'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0] == 0
        conn.execute('DROP TRIGGER fail_journal')
        success = client.post('/tools/account_submit',headers=headers,json=args)
        assert success.json()['result']['data']['status'] == 'posted'
        assert client.post('/tools/account_submit',headers=headers,json=args).json() == success.json()
    conn.close()
