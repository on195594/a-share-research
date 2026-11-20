"""NAV boundary tests: structural accounts, genuine public H00906 response (Stage36).

The captured TuShare rows are real; the account numbers and evidence strings are
explicit structural fixtures and never establish real account performance.
"""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, db, gate, integrity
from a_share_research.account import evidence, ledger, nav
from a_share_research.account.reconciliation import Event, Opening, Snapshot
from a_share_research.core.market_data import MarketDataResult
from a_share_research.core.providers import TushareMarketDataProvider
from a_share_research.tools.registry import TOOLS, Context, ToolError, discover, invoke
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


@pytest.fixture
def collect_nav(book, bind_account, monkeypatch):
    bind_account(book['conn'], 'a')
    args = request(book).model_dump(mode='json', exclude={'benchmark_evidence_hash'})
    index = book['index']
    calls = []

    def fetch(provider, code, start, end):
        calls.append((code, start, end))
        rows = [row for row in index['rows'] if start <= row['date'] <= end]
        return MarketDataResult(pd.DataFrame(rows), 'ok', index['source'], index['fetched_at'],
                                source_as_of=max(row['date'] for row in rows), freshness_days=0)

    monkeypatch.setattr(TushareMarketDataProvider, 'fetch_index_bars', fetch)
    def collect(connection=book['conn'], **changes):
        ctx = Context(connection, book['path'], book['principal'], account_shadow_enabled=True)
        return invoke(ctx, 'account_nav_evaluate', {'request': args | changes})['data']

    return collect, calls


def test_collected_index_is_referenced_replayed_and_reused(book, collect_nav):
    collect, calls = collect_nav
    tool = TOOLS['account_nav_evaluate']
    assert tool.collects and tool.access == 'write'
    uploaded = evaluate(book)
    first = collect()
    assert first['calculation'] == uploaded['calculation']
    assert first['basis']['benchmark_provenance'] == 'tools_collected_evidence'
    assert uploaded['basis']['benchmark_provenance'] == 'supplied_evidence_not_live_collection'
    artifact = json.loads(evidence.read(book['directory'], first['artifact_sha256']))
    sha = artifact['request']['benchmark_evidence_hash']
    ref = book['conn'].execute('SELECT evidence_hashes FROM account_nav_refs WHERE artifact_sha256=?',
                               (first['artifact_sha256'],)).fetchone()
    assert sha in json.loads(ref[0])
    response = json.loads(evidence.read(book['directory'], sha))
    assert response['code'] == 'H00906.CSI' and response['source'] == 'tushare.index_daily'
    assert response['rows'] == book['index']['rows'][:4]
    assert response['fetched_at'] == book['index']['fetched_at']
    assert collect() == first
    replay = nav.replay(book['conn'], book['principal'], 'a', first['artifact_sha256'], book['directory'])
    assert replay['calculation'] == first['calculation'] and replay['basis'] == first['basis']
    assert calls == [('H00906.CSI', '2026-01-05', '2026-01-08')]
    assert book['conn'].execute('SELECT count(*) FROM account_nav_refs').fetchone()[0] == 2
    # The same dates with a different valuation selection are a distinct request.
    other = observe(book, '2026-01-08T15:00:00+08:00', '1200')
    changed = collect(end=other, expected_state_sha256=capture(book)['state_sha256'])
    assert changed['calculation']['account_return'] != first['calculation']['account_return']
    assert collect() == first and len(calls) == 2


def test_collected_index_reuses_request_committed_during_fetch(book, collect_nav, monkeypatch):
    collect, _ = collect_nav
    with closing(db.connect(book['path'])) as other, monkeypatch.context() as patch:
        def finish_other_request(*args):
            nonlocal first
            patch.undo()
            first = collect(connection=other)
            # Only the concurrent caller has usable data.
            raise TimeoutError()

        first = None
        patch.setattr(TushareMarketDataProvider, 'fetch_index_bars', finish_other_request)
        outer = collect()
    assert outer['artifact_sha256'] == first['artifact_sha256']
    assert book['conn'].execute('SELECT count(*) FROM account_nav_refs').fetchone()[0] == 1


@pytest.mark.parametrize(('endpoint', 'stamp'), [
    ('start', '2026-01-05T14:59:59+08:00'),
    ('end', '2026-01-08T15:00:00.000001+08:00'),
])
def test_non_close_endpoint_rejected_before_collection(book, collect_nav, endpoint, stamp):
    collect, calls = collect_nav
    ref = observe(book, stamp, '1000')
    value = request(book).model_dump(mode='json')
    value['coverage'][endpoint] = stamp
    with pytest.raises(ToolError, match='15:00:00') as error:
        collect(**{endpoint: ref}, coverage=value['coverage'], expected_state_sha256=value['expected_state_sha256'])
    assert (error.value.status, error.value.code) == (422, 'invalid_arguments')
    assert calls == []


@pytest.mark.parametrize('missing', ['2026-01-05', '2026-01-08'])
def test_collected_index_requires_both_exact_closes(book, collect_nav, missing):
    collect, _ = collect_nav
    # Remove only the endpoint from the captured rows, leaving adjacent trading days.
    book['index']['rows'] = [r for r in book['index']['rows'] if r['date'] != missing]
    with pytest.raises(ToolError, match=f'{missing}.*另选') as error:
        collect()
    assert (error.value.status, error.value.code) == (422, 'invalid_arguments')
    response = json.loads(evidence.read(book['directory'], str(error.value).split()[-1]))
    assert response['status'] == 'ok' and missing not in [row['date'] for row in response['rows']]
    assert book['conn'].execute('SELECT count(*) FROM account_nav_refs').fetchone()[0] == 0


def test_index_collection_exception_saves_failure_and_allows_retry(book, collect_nav, monkeypatch):
    collect, _ = collect_nav

    def interrupted(*args):
        raise TimeoutError('private upstream details')

    monkeypatch.setattr(TushareMarketDataProvider, 'fetch_index_bars', interrupted)
    with pytest.raises(ToolError, match='行情暂不可用，稍后用原请求重试') as error:
        collect()
    assert (error.value.status, error.value.code) == (503, 'unavailable')
    sha = str(error.value).split()[-1]
    raw = evidence.read(book['directory'], sha)
    response = json.loads(raw)
    assert response['requested_dates'] == ['2026-01-05', '2026-01-08']
    assert response['source'] == 'tushare.index_daily' and response['status'] == 'failed'
    assert response['error_type'] == 'TimeoutError' and response['rows'] is None
    assert b'private upstream details' not in raw
    assert book['conn'].execute('SELECT count(*) FROM account_nav_refs').fetchone()[0] == 0


@pytest.mark.parametrize(('code', 'status', 'tool_code', 'message'), [
    ('TIMEOUT', 503, 'unavailable', '行情暂不可用，稍后用原请求重试'),
    ('EMPTY_RESPONSE', 422, 'invalid_arguments', '2026-01-05 至 2026-01-08.*另选区间'),
])
def test_failed_index_response_reports_recovery_and_evidence(book, collect_nav, monkeypatch,
                                                            code, status, tool_code, message):
    collect, _ = collect_nav
    index = book['index']
    failed = MarketDataResult(None, 'failed', index['source'], index['fetched_at'], error_code=code)
    monkeypatch.setattr(TushareMarketDataProvider, 'fetch_index_bars', lambda *args: failed)
    with pytest.raises(ToolError, match=message) as error:
        collect()
    assert (error.value.status, error.value.code) == (status, tool_code)
    response = json.loads(evidence.read(book['directory'], str(error.value).split()[-1]))
    assert response['status'] == 'failed' and response['error_code'] == code
    assert book['conn'].execute('SELECT count(*) FROM account_nav_refs').fetchone()[0] == 0


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
    value = request(book)
    coverage = evidence.store(book['directory'], b'Structural NAV coverage declaration')
    value.coverage.evidence_hashes = [coverage]
    result = evaluate(book, value)
    assert evaluate(book, value) == result
    sha = result['artifact_sha256']
    hashes = sorted([coverage, book['sha'], value.benchmark_evidence_hash])
    ref, = book['conn'].execute('SELECT * FROM account_nav_refs')
    assert (ref['artifact_sha256'], ref['account_id'], ref['start_date'], ref['end_date'],
            ref['benchmark_code'], ref['key_id']) == (
                sha, 'a', '2026-01-05', '2026-01-08', 'H00906.CSI', book['principal'].key_id)
    assert json.loads(ref['evidence_hashes']) == hashes
    bundle, restored = tmp_path / 'bundle', tmp_path / 'restored'
    manifest = backup.create(book['path'], bundle)
    assert manifest['required']['account_evidence'] == sorted([sha, *hashes])
    backup.verify(bundle)
    backup.restore(bundle, restored)
    conn = db.connect(restored / 'app.db')
    try:
        replay = nav.replay(conn, book['principal'], 'a', result['artifact_sha256'], restored / 'account-evidence')
        assert replay['calculation'] == result['calculation']
        assert capture(book)['state_sha256'] == nav.capture(conn, book['principal'], 'a')['state_sha256']
    finally:
        conn.close()


@pytest.mark.parametrize('damage', ['artifact', 'index', 'dates', 'references'])
def test_integrity_detects_missing_nav_evidence_or_mismatched_reference(book, damage):
    value = request(book)
    sha = evaluate(book, value)['artifact_sha256']
    if damage in ('artifact', 'index'):
        (book['directory'] / (sha if damage == 'artifact' else value.benchmark_evidence_hash)).unlink()
    else:
        book['conn'].execute('DROP TRIGGER account_nav_refs_no_update')
        if damage == 'dates':
            book['conn'].execute("UPDATE account_nav_refs SET start_date='2026-01-06'")
        else:
            book['conn'].execute("UPDATE account_nav_refs SET evidence_hashes='[]'")
    problems, _ = integrity.check(book['path'], book['path'].parent / 'pdf')
    assert len(problems) == 1 and f'净值引用 {sha}' in problems[0]


def test_tools_permissions_discovery_roundtrip_and_revocation(book, bind_account):
    bind_account(book['conn'], 'a')
    ctx = Context(book['conn'], book['path'], book['principal'], account_shadow_enabled=True)
    names = {t['name'] for t in discover(book['principal'], account_shadow_enabled=True)}
    assert {'account_nav_capture', 'account_nav_evaluate', 'account_nav_replay'} <= names
    captured = invoke(ctx, 'account_nav_capture', {'account_id': 'a'})['data']
    assert captured == capture(book)
    result = invoke(ctx, 'account_nav_evaluate', {'account_id': 'a', 'request': request(book).model_dump(mode='json')})['data']
    assert invoke(ctx, 'account_nav_replay', {'account_id': 'a', 'artifact_sha256': result['artifact_sha256']})['data']['calculation'] == result['calculation']
    for role in ('research', 'owner'):
        other = api_keys.authenticate(book['conn'], api_keys.create(book['conn'], role, role=role))
        assert not any(t['name'].startswith('account_nav_') for t in discover(other, account_shadow_enabled=True))
        with pytest.raises(ToolError) as error:
            invoke(Context(book['conn'], book['path'], other, account_shadow_enabled=True), 'account_nav_capture', {'account_id': 'a'})
        assert error.value.status == 403
    api_keys.revoke(book['conn'], book['principal'].key_id)
    with pytest.raises(ToolError) as error:
        invoke(ctx, 'account_nav_capture', {'account_id': 'a'})
    assert error.value.status == 403


def test_http_discovery_capture_evaluate_replay_and_missing_material(book, bind_account):
    bind_account(book['conn'], 'a')
    token = api_keys.create(book['conn'], 'NAV HTTP structural', role='assistant')
    headers = {'Authorization': 'Bearer ' + token}
    value = request(book).model_dump(mode='json')
    with TestClient(create_app(book['path'], account_shadow_enabled=True)) as client:
        captured = client.post('/tools/account_nav_capture', headers=headers, json={'account_id': 'a'})
        assert captured.status_code == 200
        response = client.post('/tools/account_nav_evaluate', headers=headers,
            json={'account_id': 'a', 'request': value})
        assert response.status_code == 200, response.text
        result = response.json()['result']['data']
        response = client.post('/tools/account_nav_replay', headers=headers,
            json={'account_id': 'a', 'artifact_sha256': result['artifact_sha256']})
        assert response.status_code == 200
        assert response.json()['result']['data']['calculation'] == result['calculation']
        value['coverage']['complete_external_flows'] = False
        response = client.post('/tools/account_nav_evaluate', headers=headers,
            json={'account_id': 'a', 'request': value})
        assert response.status_code == 422
    with TestClient(create_app(book['path'])) as client:
        assert client.post('/tools/account_nav_capture', headers=headers, json={'account_id': 'a'}).status_code == 404
