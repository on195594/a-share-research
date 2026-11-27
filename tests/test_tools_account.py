"""Transport/authorization tests use explicitly structural account inputs, not broker evidence."""
import base64
import json
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
from support.mcp import ORIGIN, RESOURCE, protocol_error, rpc, tool_data, tool_error


@pytest.fixture(autouse=True)
def public_mcp(monkeypatch):
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)


def mcp(path, **options):
    return TestClient(create_app(path, **options), base_url=ORIGIN)


@pytest.mark.parametrize('damage', ['missing', 'unsafe'])
def test_recent_evidence_distinguishes_invalid_limit_from_storage_failure(tmp_path, damage):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'test', role='assistant')
    conn.close()
    directory = tmp_path / 'account-evidence'
    with mcp(path, account_shadow_enabled=True) as client:
        upload = tool_data(client, token, 'account_evidence', {'content_base64': 'YQ=='})
        target = directory / upload['data']['sha256']
        if damage == 'missing':
            target.unlink()
        else:
            target.chmod(0o644)
        assert tool_error(client, token, 'account_evidence_recent', {'limit': 0})['code'] == 'invalid_arguments'
        unavailable = tool_error(client, token, 'account_evidence_recent', {'limit': 1})
        assert unavailable['code'] == 'unavailable' and '核对证据目录' in unavailable['message']


def test_recent_uses_latest_upload_metadata_and_excludes_internal_evidence(tmp_path):
    path = tmp_path / 'app.db'
    directory = tmp_path / 'account-evidence'
    index_capture = (Path(__file__).parent / 'data/account-nav-h00906.json').read_bytes()
    evidence.store(directory, index_capture)
    with closing(db.connect(path)) as conn, mcp(path, account_shadow_enabled=True) as client:
        token = api_keys.create(conn, 'uploads', role='assistant')
        key_id = api_keys.authenticate(conn, token).key_id
        payload = {'content_base64': 'YQ==', 'filename': 'original.png', 'label': '第一张'}
        invalid = tool_error(client, token, 'account_evidence', payload | {'label': '长' * 81})
        assert invalid['code'] == 'invalid_arguments' and invalid['message'].startswith('label:')
        original = tool_data(client, token, 'account_evidence', payload)['data']
        sha = original['sha256']
        other = evidence.store(directory, b'original')
        evidence.record_upload(conn, other, key_id, filename='/tmp/\n')
        latest = payload | {'filename': 'C:\\fakepath\\sub/交割\t单.png', 'label': '成交明细'}
        assert tool_data(client, token, 'account_evidence', latest)['data'] == original

        rows = conn.execute('SELECT key_id, label, uploaded_at FROM account_evidence_uploads WHERE sha256=? ORDER BY id',
                            (sha,)).fetchall()
        assert [(row['key_id'], row['label']) for row in rows] == [(key_id, '第一张'), (key_id, '成交明细')]
        recent = tool_data(client, token, 'account_evidence_recent')['data']
        first, second = recent['evidence']
        assert first == dict(sha256=sha, uploaded_at=rows[-1]['uploaded_at'], filename='交割单.png',
                             label='成交明细', size_bytes=1)
        assert (second['sha256'], second['filename'], second['label']) == (other, None, None)


def test_account_scope_observation_and_duplicate_json_rejection(tmp_path, bind_account):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, role, role=role) for role in ('research', 'assistant')}
    conn.close()
    payload = {'content_base64': base64.b64encode(b'Structural transport evidence, no financial assertions').decode()}
    with mcp(path) as client:
        assert protocol_error(client, tokens['assistant'], 'account_evidence', payload)['code'] == -32602
    with mcp(path, account_shadow_enabled=True) as client:
        assert tool_error(client, tokens['research'], 'account_evidence', payload)['code'] == 'forbidden'
        sha = tool_data(client, tokens['assistant'], 'account_evidence', payload)['data']['sha256']
        assert evidence.verify(tmp_path / 'account-evidence', sha)
        with db.connect(path) as conn:
            principal = api_keys.authenticate(conn, tokens['assistant'])
            ledger.initialize(conn, principal, 'test', Opening(positions={}, cash='100'), datetime(2026, 11, 1),
                              tmp_path / 'account-evidence', sha, tolerance=Decimal(0))
            bind_account(conn, 'test')
        args = dict(evidence_hash=sha,
                    observation={'observed_at':'2026-11-11T15:00:00+08:00','cash':'100'})
        result = tool_data(client, tokens['assistant'], 'account_observe', args)
        assert result['data']['trades_recorded'] is False
        assert tool_data(client, tokens['assistant'], 'account_observe', args | {'account_id': 'test'}) == result
        wrong = tool_error(client, tokens['assistant'], 'account_observe', args | {'account_id': 'other'})
        assert wrong['code'] == 'conflict' and 'account_id' in wrong['message']
        for body in ('{"account_id":"a","account_id":"b"}',
                     '{"account_id":"a","observation":{"cash":1,"cash":2}}',
                     '{"account_id":"a","observation":{"cash":NaN}}'):
            def raw(envelope, body=body):
                return json.dumps(envelope).replace('"arguments": {}', '"arguments": ' + body)

            rejected = rpc(client, tokens['assistant'], 'tools/call', {'name': 'account_observe', 'arguments': {}},
                           serialize=raw)
            assert rejected.status_code == 400 and rejected.json()['error']['code'] == -32700
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
    with mcp(path, account_shadow_enabled=True) as client:
        unbound = tool_error(client, token, 'account_state', {'account_id': 'test'})
        assert unbound['code'] == 'conflict' and '绑定' in unbound['message']
        bind_account(conn, 'test')
        assert tool_data(client, token, 'account_state') == tool_data(client, token, 'account_state', {'account_id': 'test'})
        # Explicit corruption of the v24 singleton constraint exercises ambiguous bindings.
        conn.execute('PRAGMA ignore_check_constraints=ON')
        conn.execute('INSERT INTO account_source SELECT 2,mode,source_sha256,updated_at,watermark FROM account_source')
        ambiguous = tool_error(client, token, 'account_state')
        assert ambiguous['code'] == 'conflict' and '唯一' in ambiguous['message']
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
    with mcp(path, account_shadow_enabled=True) as client:
        snapshot = args['batch']['snapshot'] | {'positions': {'600519': 100}}
        incomplete = args | {'batch': args['batch'] | {'snapshot': snapshot}}
        assert tool_error(client, token, 'account_submit', incomplete) == {
            'code': 'invalid_arguments', 'message': 'batch.snapshot.market_values: 缺 600519 市值'}
        failed = tool_data(client, token, 'account_submit', args)['data']
        assert failed['status'] == 'service_failure'
        read = tool_data(client, token, 'account_receipt', {'account_id': 'test', 'receipt_id': failed['receipt_id']})
        assert read['data'] == failed
        assert conn.execute("SELECT count(*) FROM account_batches WHERE status='posted'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM account_journal').fetchone()[0] == 0
        conn.execute('DROP TRIGGER fail_journal')
        success = tool_data(client, token, 'account_submit', args)
        assert success['data']['status'] == 'posted'
        assert tool_data(client, token, 'account_submit', args) == success
    conn.close()
