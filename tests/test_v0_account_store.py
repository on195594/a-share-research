"""Invented account/policy contracts; fixtures are not broker acceptance evidence."""
import json
import sqlite3

import pytest

from a_share_research import api_keys
from a_share_research.account import analysis, evidence, ledger, position_store
from a_share_research.account.mirror import MANIFEST
from a_share_research.policy.v0 import account_store, db as policy_db
from a_share_research.tools import registry
from support.positions import batch, event, submit


def rows(conn, prefix=''):
    return {table: [dict(zip(MANIFEST[table], row)) for row in conn.execute(
        f"SELECT {','.join(MANIFEST[table])} FROM {prefix}{table} ORDER BY id")]
        for table in ('holdings', 'holding_events')}


def initialize(book, hashes=None):
    return account_store.initialize(book[0], book[1], 'a', book[2],
                                    [book[3]] if hashes is None else hashes)


def read(book):
    return account_store.read(book[0], book[1], 'a')


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv('POLICY_V0_DB', str(tmp_path / 'app.db'))
    monkeypatch.setenv('POLICY_V0_ACCOUNT_SHADOW', 'a')


def test_baseline_preserves_every_column_history_and_immutable_binding(policy_book):
    book = policy_book
    before = rows(book[0])
    first = initialize(book)
    assert {table: first[table] for table in before} == before
    assert initialize(book) == read(book) == first
    assert first['holdings'][0]['stop_loss_15'] == 8.5
    assert account_store.inspect(book[0]) == []
    for table in ('policy_v0_profiles', 'policy_v0_account_openings'):
        for statement in (f'DELETE FROM {table}', f'UPDATE {table} SET created_at=created_at'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                book[0].execute(statement)
    other = evidence.store(book[2], b'Different structural opening evidence')
    with pytest.raises(ledger.LedgerError, match='不能覆盖'):
        initialize(book, [other])


def test_future_buy_sell_stable_mapping_keeps_history_and_main_untouched(policy_book):
    book = policy_book
    before = rows(book[0])
    initialize(book)
    assert submit(book, batch(book, [event()]))['status'] == 'posted'
    bought = read(book)
    assert bought['holdings'][0]['shares'] == 200
    assert bought['holdings'][0]['reference_cost'] == 11
    assert bought['holdings'][0]['main_entry_date'] == '2026-11-02'
    assert bought['holding_events'][:3] == before['holding_events']
    buy_id = bought['holding_events'][-1]['id']
    assert buy_id > 20 and bought['holding_events'][-1]['holding_id'] == 1
    sale = event('sell', price='15', broker_id='structural-sale', occurred_at='2026-11-03T10:00:00+08:00')
    assert submit(book, batch(book, [sale], version=1, shares=100, cash='10300'))['status'] == 'posted'
    sold = read(book)
    assert sold == read(book)
    assert sold['holding_events'][:-1] == bought['holding_events']
    assert sold['holding_events'][-1]['id'] > buy_id
    assert sold['holding_events'][-1]['realized_pnl'] == 400
    assert sold['holdings'][0]['reference_cost'] == 11
    assert sold['holdings'][1] == before['holdings'][1]
    assert rows(book[0]) == before
    assert account_store.inspect(book[0]) == []


@pytest.mark.parametrize('missing', ['profile', 'framework'])
def test_inspection_detects_historical_financial_posting_with_missing_policy_context(policy_book, missing):
    book = policy_book
    profile = initialize(book)['profile_identity']
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    metadata = dict(policy_identity='unknown-structural-profile' if missing == 'profile' else profile,
                    name='Structural reopened')
    if missing != 'framework':
        metadata['framework'] = 'A通用'
    reopened = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    assert submit(book, batch(book, [reopened], version=1, shares=100, cash='10200',
                             entries={'0': metadata}))['status'] == 'posted'
    assert position_store.state(book[0], book[1], 'a')['version'] == 2
    with pytest.raises(ledger.LedgerError, match='政策投影缺口'):
        read(book)
    assert any('政策投影缺口' in item for item in account_store.inspect(book[0]))


def test_new_cycle_explicit_context_gets_fresh_stable_identity(policy_book):
    book = policy_book
    profile = initialize(book)['profile_identity']
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    reopened = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    assert submit(book, batch(book, [reopened], version=1, shares=100, cash='10200', entries={
        '0': dict(policy_identity=profile, framework='A通用', framework_confident=True,
                  name='Structural reopened')}))['status'] == 'posted'
    result = read(book)
    assert result == read(book)
    new = result['holdings'][-1]
    assert new['id'] > 2 and new['name'] == 'Structural reopened'
    assert new['framework'] == 'A通用' and new['framework_confident'] == 1
    assert result['holdings'][0]['exit_date'] == '2026-11-02'
    assert result['holding_events'][-1]['holding_id'] == new['id']


def test_account_submit_rolls_back_invalid_opening_framework_then_projects_valid_entry(policy_book, tmp_path, bind_account):
    book = policy_book
    conn, principal, directory, _ = book
    profile = initialize(book)['profile_identity']
    directory.rename(tmp_path / 'account-evidence')
    bind_account(conn, 'a')
    ctx = registry.Context(conn, tmp_path / 'app.db', principal, account_shadow_enabled=True)
    parameters, = conn.execute('SELECT parameters FROM policy_v0_profiles WHERE identity=?', (profile,)).fetchone()
    close = event('sell')
    reopen = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    def post(entry):
        value = batch(book, [close, reopen], shares=100, cash='10200', entries={'1': entry})
        return registry.invoke(ctx, 'account_submit', {'batch': value.model_dump(mode='json')})['data']

    for invalid in ({}, {'framework': 'A'}):
        with pytest.raises(registry.ToolError, match=r'position_entries\.1\.framework') as rejected:
            post({'policy_identity': profile, **invalid})
        assert rejected.value.code == 'invalid_arguments'
        assert all(option in str(rejected.value) for option in json.loads(parameters)['stops'])
        assert ledger.state(conn, principal, 'a')['version'] == 0
        assert account_store.project(conn, 'a')['version'] == 0
        assert conn.execute('SELECT count(*) FROM account_batches').fetchone()[0] == 0
    receipt = post({'policy_identity': profile, 'framework': 'A通用'})
    assert receipt['status'] == 'posted' and receipt['version'] == 1
    projected = account_store.project(conn, 'a')
    assert projected['holdings'][-1]['framework'] == 'A通用'
    assert account_store.inspect(conn) == []


@pytest.mark.parametrize('invalid', ['empty_evidence', 'absent_evidence', 'shares', 'late'])
def test_policy_opening_rejects_unbound_or_inconsistent_sources(policy_book, invalid):
    book = policy_book
    hashes = [book[3]]
    error = ledger.LedgerError
    match = '股数'
    if invalid == 'empty_evidence':
        hashes, match = [], '证据'
    elif invalid == 'absent_evidence':
        hashes, error, match = ['0' * 64], evidence.EvidenceError, None
    elif invalid == 'shares':
        book[0].execute('UPDATE holdings SET shares=99 WHERE id=1')
    else:
        assert submit(book, batch(book, [event()]))['status'] == 'posted'
        match = '首笔入账前'
    with pytest.raises(error, match=match):
        initialize(book, hashes)
    assert book[0].execute('SELECT count(*) FROM policy_v0_account_openings').fetchone()[0] == 0


@pytest.mark.parametrize('role', ['research', 'owner'])
def test_policy_reads_and_initialization_require_account_role(policy_book, role):
    book = policy_book
    denied = api_keys.authenticate(book[0], api_keys.create(book[0], 'structural-denied-' + role, role=role))
    with pytest.raises(ledger.LedgerError) as caught:
        account_store.initialize(book[0], denied, 'a', book[2], [book[3]])
    assert caught.value.status == 403
    initialize(book)
    with pytest.raises(ledger.LedgerError) as caught:
        account_store.read(book[0], denied, 'a')
    assert caught.value.status == 403


def test_source_hash_tampering_is_detected_by_read_and_inspection(policy_book):
    book = policy_book
    initialize(book)
    book[0].execute('DROP TRIGGER policy_v0_account_openings_no_update')
    book[0].execute("UPDATE policy_v0_account_openings SET source_hash='tampered'")
    with pytest.raises(ledger.LedgerError, match='哈希'):
        read(book)
    assert any('哈希' in item for item in account_store.inspect(book[0]))


@pytest.mark.parametrize('combined', [False, True])
def test_opt_in_temp_projection_is_sealed_and_default_off_stays_legacy(policy_book, tmp_path, monkeypatch, combined):
    book = policy_book
    before = rows(book[0])
    initialize(book)
    if combined:
        analysis.initialize(book[0], book[1], 1)
        monkeypatch.setenv('POLICY_V0_ANALYSIS_SHADOW', '1')
    else:
        monkeypatch.delenv('POLICY_V0_ANALYSIS_SHADOW', raising=False)
    assert submit(book, batch(book, [event()]))['status'] == 'posted'
    configure(monkeypatch, tmp_path)
    assert policy_db.is_read_only()
    with policy_db.db_session() as projected:
        assert projected.execute('PRAGMA query_only').fetchone()[0] == 1
        assert rows(projected)['holdings'][0]['shares'] == 200
        assert rows(projected, 'main.') == before
        assert projected.in_transaction
        for statement in ('DELETE FROM holdings', 'DELETE FROM temp.holding_events',
                          'DELETE FROM main.holdings', 'CREATE TEMP TABLE bypass(id)'):
            with pytest.raises(sqlite3.OperationalError):
                projected.execute(statement)
        if combined:
            tables = {row[0] for row in projected.execute("SELECT name FROM sqlite_temp_master WHERE type='table'")}
            assert set(analysis.TABLES).issubset(tables)
    assert rows(book[0]) == before
    monkeypatch.delenv('POLICY_V0_ACCOUNT_SHADOW')
    monkeypatch.delenv('POLICY_V0_ANALYSIS_SHADOW', raising=False)
    with policy_db.read_only_db_session() as normal:
        assert rows(normal) == before
        assert normal.execute('SELECT count(*) FROM sqlite_temp_master').fetchone()[0] == 0
