"""NAV boundary tests: structural accounts, genuine public H00906 response (Stage36).

The captured TuShare rows are real; the account numbers and evidence strings are
explicit structural fixtures and never establish real account performance.
"""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db, gate
from a_share_research.account import evidence, ledger, nav
from a_share_research.account.reconciliation import Event, Opening, Snapshot
from a_share_research.tools.registry import Context, ToolError, discover, invoke
from a_share_research.tools.server import create_app


@pytest.fixture
def book(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'NAV structural', role='assistant'))
    directory = tmp_path / 'account-evidence'
    sha = evidence.store(directory, b'Structural NAV fixture, NOT broker evidence or actual performance')
    ledger.initialize(conn, principal, 'a', Opening(positions={}, cash='1000'),
        datetime.fromisoformat('2026-01-01T00:00:00+08:00'), directory, sha, tolerance=Decimal('0'))
    result = dict(conn=conn, principal=principal, directory=directory, sha=sha, path=tmp_path / 'app.db')
    result['start'] = observe(result, '2026-01-05T15:00:00+08:00', '1000')
    result['end'] = observe(result, '2026-01-08T15:00:00+08:00', '1100')
    result['index'] = json.loads((Path(__file__).parent / 'data/account-nav-h00906.json').read_text())
    yield result
    conn.close()


def observe(book, stamp, value):
    receipt = ledger.observe(book['conn'], book['principal'], 'a', ledger.Observation(
        observed_at='2026-11-11T16:00:00+08:00', portfolio_value_as_of=stamp, total_assets=value),
        book['directory'], book['sha'])
    return {'kind': 'observation', 'id': receipt['observation_id'], 'reason': 'Explicit structural valuation selection'}


def capture(book):
    return nav.capture(book['conn'], book['principal'], 'a')


def request(book, **changes):
    return nav.Request.model_validate(dict(expected_state_sha256=capture(book)['state_sha256'],
        start=book['start'], end=book['end'], flows=[], coverage=dict(
            start='2026-01-05T15:00:00+08:00', end='2026-01-08T15:00:00+08:00',
            currency='CNY', scope='cash_equity_no_liabilities', complete_external_flows=True,
            no_security_transfers=True, evidence_hashes=[book['sha']], explanation='Structural complete flow declaration'),
        benchmark_evidence_hash=evidence.store(book['directory'], gate.canonical(book['index']).encode())) | changes)


def evaluate(book, value=None):
    return nav.evaluate(book['conn'], book['principal'], 'a', value or request(book), book['directory'])


def post(book, *, cash='100', kind='deposit', voids=(), balance='1100', code=None):
    event = Event(kind=kind, cash=cash, code=code, occurred_at='2026-01-06T10:00:00+08:00', broker_id='flow')
    version = ledger.state(book['conn'], book['principal'], 'a')['version']
    receipt = ledger.submit(book['conn'], book['principal'], 'a', ledger.Batch(expected_version=version,
        events=[event], void_event_ids=list(voids), evidence_hashes=[book['sha']],
        snapshot=Snapshot(positions={}, cash=balance, market_values={}, total_assets=balance)), book['directory'])
    assert receipt['status'] == 'posted'
    return book['conn'].execute('SELECT max(id) FROM account_journal').fetchone()[0]


def test_no_flow_real_index_endpoints_and_idempotent_artifact(book):
    before = capture(book)
    result = evaluate(book)
    assert Decimal(result['calculation']['account_return']) == Decimal('.1')
    expected = Decimal('7554.8611') / Decimal('7467.2048') - 1
    assert abs(Decimal(result['calculation']['benchmark_return']) - expected) < Decimal('1e-26')
    assert result['basis']['trade_history_acceptance'] == 'not_assessed'
    assert result['basis']['coverage'] == 'caller_declared_with_evidence'
    assert evaluate(book) == result
    assert capture(book) == before
    replay = nav.replay(book['conn'], book['principal'], 'a', result['artifact_sha256'], book['directory'])
    assert replay['calculation'] == result['calculation']
    assert replay['mode'] == 'saved_input_replay'


def test_index_json_decimal_amount_is_not_rounded_through_binary_float(book):
    # Deliberate precision variant, not a captured market observation.
    raw = gate.canonical(book['index']).replace('7467.2048', '7467.20480000000000000000000001')
    value = request(book, benchmark_evidence_hash=evidence.store(book['directory'], raw.encode()))
    result = evaluate(book, value)
    assert result['inputs']['benchmark_start'] == '7467.20480000000000000000000001'


def test_each_deposit_needs_exact_preflow_valuation_and_not_profit(book):
    event_id = post(book)
    with pytest.raises(ledger.LedgerError, match='每笔有效'):
        evaluate(book)
    before = observe(book, '2026-01-06T10:00:00+08:00', '1000')
    result = evaluate(book, request(book, flows=[{'event_id': event_id, 'before': before}]))
    assert Decimal(result['calculation']['account_return']) == 0
    assert Decimal(result['calculation']['end_units']) == 1100


@pytest.mark.parametrize('kind,cash,balance,end,expected', [
    ('withdrawal', '100', '900', '900', '0'),
    ('fee', '10', '990', '990', '-.01'),
    ('dividend', '100', '1100', '1100', '.1'),
])
def test_withdrawal_is_external_but_fee_and_dividend_are_returns(book, kind, cash, balance, end, expected):
    event_id = post(book, kind=kind, cash=cash, balance=balance, code='600519' if kind == 'dividend' else None)
    book['end'] = observe(book, '2026-01-08T15:00:00+08:00', end)
    refs = []
    if kind == 'withdrawal':
        refs = [{'event_id': event_id, 'before': observe(book, '2026-01-06T10:00:00+08:00', '1000')}]
    result = evaluate(book, request(book, flows=refs))
    assert Decimal(result['calculation']['account_return']) == Decimal(expected)


def test_new_observation_changes_hash_without_version_change_and_old_replay_survives(book):
    original = request(book)
    result = evaluate(book, original)
    state = capture(book)
    observe(book, '2026-01-08T15:00:00+08:00', '1200')
    changed = capture(book)
    assert state['source']['book']['version'] == changed['source']['book']['version'] == 0
    assert state['state_sha256'] != changed['state_sha256']
    with pytest.raises(ledger.LedgerError, match='资料已变化'):
        evaluate(book, original)
    replay = nav.replay(book['conn'], book['principal'], 'a', result['artifact_sha256'], book['directory'])
    assert replay['calculation'] == result['calculation']


def test_void_correction_creates_new_capture_without_reinterpreting_old_result(book):
    first = post(book)
    before = observe(book, '2026-01-06T10:00:00+08:00', '1000')
    original = evaluate(book, request(book, flows=[{'event_id': first, 'before': before}]))
    second = post(book, cash='200', voids=[first], balance='1200')
    with pytest.raises(ledger.LedgerError, match='每笔有效'):
        evaluate(book, request(book, flows=[{'event_id': first, 'before': before}]))
    corrected = evaluate(book, request(book, flows=[{'event_id': second, 'before': before}]))
    assert corrected['artifact_sha256'] != original['artifact_sha256']
    assert corrected['calculation']['account_return'] != original['calculation']['account_return']
    replay = nav.replay(book['conn'], book['principal'], 'a', original['artifact_sha256'], book['directory'])
    assert replay['calculation'] == original['calculation']


@pytest.mark.parametrize('field', ['complete_external_flows', 'no_security_transfers'])
def test_unknown_coverage_never_defaults_to_zero(book, field):
    value = request(book).model_dump(mode='json')
    value['coverage'][field] = False
    with pytest.raises(ledger.LedgerError, match='完整外部资金'):
        evaluate(book, nav.Request.model_validate(value))


def test_coverage_interval_and_missing_original_rejected(book):
    value = request(book)
    value.coverage.start = datetime.fromisoformat('2026-01-04T15:00:00+08:00')
    with pytest.raises(ledger.LedgerError, match='精确匹配'):
        evaluate(book, value)
    (book['directory'] / book['sha']).unlink()
    with pytest.raises(evidence.EvidenceError):
        evaluate(book)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_code', 'stale', 'failed', 'nonfinite'])
def test_index_failures_are_not_filled_with_nearest_close(book, mutation):
    index = book['index']
    if mutation == 'missing':
        index['rows'] = [r for r in index['rows'] if r['date'] != '2026-01-08']
    elif mutation == 'duplicate':
        index['rows'].append(index['rows'][0])
    elif mutation == 'wrong_code':
        index['code'] = 'H00300.CSI'
    elif mutation == 'stale':
        index['source_as_of'] = '2026-01-07'
    elif mutation == 'failed':
        index['status'] = 'failed'
    else:
        index['rows'][0]['close'] = 'NaN'
    with pytest.raises(ledger.LedgerError):
        evaluate(book)


def test_missing_valuation_time_does_not_use_record_time(book):
    receipt = ledger.observe(book['conn'], book['principal'], 'a', ledger.Observation(
        observed_at='2026-01-05T15:00:00+08:00', total_assets='1000'), book['directory'], book['sha'])
    book['start']['id'] = receipt['observation_id']
    with pytest.raises(ledger.LedgerError, match='不能用记录时间'):
        evaluate(book)


def test_posted_snapshot_can_be_selected_without_claiming_full_history(book):
    receipt = ledger.submit(book['conn'], book['principal'], 'a', ledger.Batch(
        expected_version=0, evidence_hashes=[book['sha']], snapshot=Snapshot(positions={}, cash='1000',
            total_assets='1000', market_values={}, portfolio_value_as_of='2026-01-05T15:00:00+08:00')), book['directory'])
    assert receipt['status'] == 'posted'
    book['start'] = {'kind': 'batch', 'id': receipt['receipt_id'], 'reason': 'Selected posted valuation'}
    assert evaluate(book)['basis']['trade_history_acceptance'] == 'not_assessed'


def test_post_flow_batch_cannot_be_used_as_pre_flow_value(book):
    event_id = post(book)
    receipt = book['conn'].execute('SELECT batch_id FROM account_journal WHERE id=?', (event_id,)).fetchone()[0]
    with pytest.raises(ledger.LedgerError, match='不能作为流前'):
        evaluate(book, request(book, flows=[{'event_id': event_id, 'before': {
            'kind': 'batch', 'id': receipt, 'reason': 'Invalid post-flow snapshot'}}]))


def test_tampered_saved_result_rejected_and_cross_account_denied(book):
    result = evaluate(book)
    raw = json.loads(evidence.read(book['directory'], result['artifact_sha256']))
    raw['result']['calculation']['end_nav'] = '9'
    sha = evidence.store(book['directory'], gate.canonical(raw).encode())
    with pytest.raises(ledger.LedgerError, match='重放不一致'):
        nav.replay(book['conn'], book['principal'], 'a', sha, book['directory'])
    with pytest.raises(ledger.LedgerError, match='账户不一致'):
        nav.replay(book['conn'], book['principal'], 'b', result['artifact_sha256'], book['directory'])


def test_shipping_backup_restore_preserves_nav_inputs_and_replay(book, tmp_path):
    from a_share_research import backup
    result = evaluate(book)
    bundle, restored = tmp_path / 'bundle', tmp_path / 'restored'
    backup.create(book['path'], bundle)
    backup.verify(bundle)
    backup.restore(bundle, restored)
    conn = db.connect(restored / 'app.db')
    try:
        replay = nav.replay(conn, book['principal'], 'a', result['artifact_sha256'], restored / 'account-evidence')
        assert replay['calculation'] == result['calculation']
        assert capture(book)['state_sha256'] == nav.capture(conn, book['principal'], 'a')['state_sha256']
    finally:
        conn.close()


def test_tools_permissions_discovery_roundtrip_and_revocation(book):
    ctx = Context(book['conn'], book['path'], book['principal'], account_shadow_enabled=True)
    names = {t['name'] for t in discover(book['principal'], account_shadow_enabled=True)}
    assert {'account.nav.capture', 'account.nav.evaluate', 'account.nav.replay'} <= names
    captured = invoke(ctx, 'account.nav.capture', {'account_id': 'a'})['data']
    assert captured == capture(book)
    result = invoke(ctx, 'account.nav.evaluate', {'account_id': 'a', 'request': request(book).model_dump(mode='json')})['data']
    assert invoke(ctx, 'account.nav.replay', {'account_id': 'a', 'artifact_sha256': result['artifact_sha256']})['data']['calculation'] == result['calculation']
    for role in ('research', 'owner'):
        other = api_keys.authenticate(book['conn'], api_keys.create(book['conn'], role, role=role))
        assert not any(t['name'].startswith('account.nav.') for t in discover(other, account_shadow_enabled=True))
        with pytest.raises(ToolError) as error:
            invoke(Context(book['conn'], book['path'], other, account_shadow_enabled=True), 'account.nav.capture', {'account_id': 'a'})
        assert error.value.status == 403
    book['conn'].execute('DELETE FROM api_keys WHERE id=?', (book['principal'].key_id,))
    with pytest.raises(ToolError) as error:
        invoke(ctx, 'account.nav.capture', {'account_id': 'a'})
    assert error.value.status == 403


def test_http_discovery_capture_evaluate_replay_and_missing_material(book):
    token = api_keys.create(book['conn'], 'NAV HTTP structural', role='assistant')
    headers = {'Authorization': 'Bearer ' + token}
    value = request(book).model_dump(mode='json')
    with TestClient(create_app(book['path'], account_shadow_enabled=True)) as client:
        captured = client.post('/tools/account.nav.capture', headers=headers, json={'account_id': 'a'})
        assert captured.status_code == 200
        response = client.post('/tools/account.nav.evaluate', headers=headers,
            json={'account_id': 'a', 'request': value})
        assert response.status_code == 200, response.text
        result = response.json()['result']['data']
        response = client.post('/tools/account.nav.replay', headers=headers,
            json={'account_id': 'a', 'artifact_sha256': result['artifact_sha256']})
        assert response.status_code == 200
        assert response.json()['result']['data']['calculation'] == result['calculation']
        value['coverage']['complete_external_flows'] = False
        response = client.post('/tools/account.nav.evaluate', headers=headers,
            json={'account_id': 'a', 'request': value})
        assert response.status_code == 422
    with TestClient(create_app(book['path'])) as client:
        assert client.post('/tools/account.nav.capture', headers=headers, json={'account_id': 'a'}).status_code == 404
