"""Invented scoped analysis contracts only; no investment or broker acceptance evidence."""
from datetime import datetime
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from a_share_research import api_keys
from a_share_research.account import analysis, cycle_analysis, evidence, ledger, position_store
from test_account_analysis import legacy_rows, request
from test_analysis_transitions import rewrite, open_alert
from test_position_store import seed, event, batch, submit
from test_v0_account_store import book as policy_book


@pytest.fixture
def book(tmp_path):
    yield from policy_book.__wrapped__(tmp_path)


def initialize(book, cycle='legacy:1', account='a', version=0, revision=None, hashes=None):
    return cycle_analysis.initialize(book[0], book[1], account, cycle,
        cycle_analysis.Initialize(expected_account_version=version,
            evidence_hashes=[book[3]] if hashes is None else hashes, legacy_revision_id=revision), book[2])


def current(book, cycle='legacy:1', account='a'):
    return cycle_analysis.current(book[0], book[1], account, cycle)


def update(book, value=None, cycle='legacy:1', account='a'):
    return cycle_analysis.update(book[0], book[1], account, cycle,
        value or request(book, [dict(kind='tier', tier='tier2', status='completed')]), book[2])


def reopened(book):
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    buy = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    assert submit(book, batch(book, [buy], version=1, shares=100, cash='10200',
        entries={'0': dict(policy_identity='structural-profile', name='Structural reopened')}))['status'] == 'posted'
    return position_store.state(book[0], book[1], 'a')['projection']['cycles'][-1]['cycle_id']


def test_seed_baseline_requires_explicit_current_legacy_revision_and_keeps_snapshot(book):
    old = analysis.initialize(book[0], book[1], 1)
    before = legacy_rows(book[0])
    with pytest.raises(ledger.LedgerError, match='当前分析版本'):
        initialize(book)
    base = initialize(book, revision=old['revision_id'])
    assert base['snapshot'] == old['snapshot']
    assert base['account_id'] == 'a' and base['cycle_id'] == 'legacy:1'
    assert base['source'] == 'journal_baseline' and base['version'] == 0
    assert initialize(book, revision=old['revision_id']) == base
    assert legacy_rows(book[0]) == before
    other = evidence.store(book[2], b'Other invented structural baseline evidence')
    with pytest.raises(ledger.LedgerError, match='不能覆盖'):
        initialize(book, revision=old['revision_id'], hashes=[other])
    assert analysis.inspect(book[0]) == []


def test_same_holding_id_accounts_and_legacy_api_remain_isolated(book):
    conn, principal, directory, sha = book
    conn.execute("INSERT INTO holding_tier_state(holding_id,updated_at) VALUES (1,'2026-11-01')")
    old = analysis.initialize(conn, principal, 1)
    ledger.initialize(conn, principal, 'b', dict(positions={'600001':100}, cash='10000'),
        datetime.fromisoformat('2026-11-01T00:00:00+08:00'), directory, sha, tolerance=Decimal(0))
    position_store.initialize(conn, principal, 'b', [seed()], directory, [sha])
    a = initialize(book, revision=old['revision_id'])
    b = initialize(book, account='b', revision=old['revision_id'])
    result = update(book)
    assert result['holding_id'] == a['holding_id'] == b['holding_id'] == 1
    assert current(book, account='b') == b
    assert analysis.current(conn, principal, 1) == old
    legacy = analysis.update(conn, principal, 1,
        request(book, [dict(kind='tier', tier='tier3', status='completed')]), directory)
    assert current(book) == result and current(book, account='b') == b
    assert analysis.current(conn, principal, 1) == legacy
    assert analysis.history(conn, principal, 1)['revisions'] == [old, legacy]
    assert cycle_analysis.history(conn, principal, 'a', 'legacy:1')['revisions'] == [a, result]
    assert cycle_analysis.history(conn, principal, 'b', 'legacy:1')['revisions'] == [b]
    assert analysis.inspect(conn) == []


def test_new_cycle_starts_empty_pending_then_appends_thesis_alert_and_tier(book):
    cycle = reopened(book)
    before = legacy_rows(book[0])
    base = initialize(book, cycle, version=2)
    snapshot = base['snapshot']
    assert all(snapshot[t] == [] for t in analysis.TABLES if t != 'holding_tier_state')
    assert all(snapshot['holding_tier_state'][0][tier + '_status'] == 'pending' for tier in ('tier1','tier2','tier3'))
    value = request(book, [rewrite(retire_l3_ids=[]), open_alert(),
                          dict(kind='tier', tier='tier2', status='completed')])
    result = update(book, value, cycle)
    assert update(book, value, cycle) == result
    assert result['version'] == 1 and result['parent_id'] == base['revision_id']
    assert len(result['snapshot']['holding_l3_conditions']) == 2
    assert result['snapshot']['holding_thesis_versions'][0]['status'] == 'active'
    assert result['snapshot']['holding_alerts'][0]['status'] == 'active'
    assert result['snapshot']['holding_tier_state'][0]['tier2_status'] == 'completed'
    assert legacy_rows(book[0]) == before
    history = cycle_analysis.history(book[0], book[1], 'a', cycle, limit=1)
    assert history['revisions'] == [base]
    assert cycle_analysis.history(book[0], book[1], 'a', cycle,
        after_id=history['next_after_id'])['revisions'] == [result]
    assert analysis.inspect(book[0]) == []


@pytest.mark.parametrize('invalid', ['version', 'evidence', 'borrow', 'empty'])
def test_new_baseline_rejects_invalid_version_evidence_and_legacy_borrow(book, invalid):
    legacy = analysis.initialize(book[0], book[1], 1)
    cycle = reopened(book)
    args = dict(cycle=cycle, version=2)
    error = ledger.LedgerError
    if invalid == 'version':
        args['version'] = 1
    elif invalid == 'evidence':
        args['hashes'], error = ['0' * 64], evidence.EvidenceError
    elif invalid == 'borrow':
        args['revision'] = legacy['revision_id']
    else:
        args['hashes'], error = [], ValidationError
    with pytest.raises(error):
        initialize(book, **args)
    assert cycle_analysis.history(book[0], book[1], 'a', cycle)['revisions'] == []


def test_seed_cannot_import_stale_or_other_holdings_revision(book):
    conn, principal, *_ = book
    conn.execute("INSERT INTO holding_tier_state(holding_id,updated_at) VALUES (1,'2026-11-01')")
    old = analysis.initialize(conn, principal, 1)
    latest = analysis.update(conn, principal, 1, request(book,
        [dict(kind='tier', tier='tier2', status='completed')]), book[2])
    with pytest.raises(ledger.LedgerError, match='当前分析版本'):
        initialize(book, revision=old['revision_id'])
    assert initialize(book, revision=latest['revision_id'])['snapshot'] == latest['snapshot']


@pytest.mark.parametrize('retirement', ['closed', 'void'])
def test_closed_or_voided_cycle_keeps_audit_history_but_refuses_new_writes(book, retirement):
    cycle = reopened(book)
    base = initialize(book, cycle, version=2)
    if retirement == 'closed':
        sale = event('sell', price='12', broker_id='structural-final-sell', occurred_at='2026-11-04T10:00:00+08:00')
        value = batch(book, [sale], version=2, shares=0, cash='11400')
    else:
        identity = book[0].execute('SELECT max(id) FROM account_journal').fetchone()[0]
        value = batch(book, [], version=2, shares=0, cash='11200', voids=[identity])
    assert submit(book, value)['status'] == 'posted'
    with pytest.raises(ledger.LedgerError, match='关闭|失效'):
        update(book, cycle=cycle)
    assert current(book, cycle) == base
    assert cycle_analysis.history(book[0], book[1], 'a', cycle)['revisions'] == [base]
    assert analysis.inspect(book[0]) == []


@pytest.mark.parametrize('column,value', [('snapshot_hash','bad'), ('cycle_id','wrong-cycle')])
def test_hash_and_cycle_ownership_tampering_are_detected(book, column, value):
    cycle = reopened(book)
    base = initialize(book, cycle, version=2)
    book[0].execute('DROP TRIGGER account_analysis_no_update')
    book[0].execute(f'UPDATE account_analysis_history SET {column}=? WHERE id=?', (value, base['revision_id']))
    assert analysis.inspect(book[0])
    with pytest.raises(ledger.LedgerError, match='哈希|身份'):
        update(book, cycle=cycle)


def test_snapshot_row_cannot_claim_another_holding_even_with_recomputed_hash(book):
    from a_share_research import gate
    cycle = reopened(book)
    base = initialize(book, cycle, version=2)
    snapshot = base['snapshot']
    snapshot['holding_tier_state'][0]['holding_id'] = 999
    book[0].execute('DROP TRIGGER account_analysis_no_update')
    book[0].execute('UPDATE account_analysis_history SET snapshot=?,snapshot_hash=? WHERE id=?',
        (json.dumps(snapshot), gate.digest(snapshot), base['revision_id']))
    assert analysis.inspect(book[0])
    with pytest.raises(ledger.LedgerError, match='归属'):
        update(book, cycle=cycle)


@pytest.mark.parametrize('role', ['research', 'owner'])
def test_scoped_read_and_write_require_assistant(book, role):
    cycle = reopened(book)
    initialize(book, cycle, version=2)
    denied = api_keys.authenticate(book[0], api_keys.create(book[0], 'structural-denied-' + role, role=role))
    for action in (
        lambda: cycle_analysis.current(book[0], denied, 'a', cycle),
        lambda: cycle_analysis.history(book[0], denied, 'a', cycle),
        lambda: cycle_analysis.initialize(book[0], denied, 'a', cycle,
            dict(expected_account_version=2, evidence_hashes=[book[3]]), book[2]),
        lambda: cycle_analysis.update(book[0], denied, 'a', cycle,
            request(book, [dict(kind='tier', tier='tier2', status='completed')]), book[2]),
    ):
        with pytest.raises(ledger.LedgerError) as caught:
            action()
        assert caught.value.status == 403


def configure_projection(book, tmp_path, monkeypatch):
    from a_share_research.policy.v0 import account_store
    account_store.initialize(book[0], book[1], 'a', book[2], [book[3]])
    monkeypatch.setenv('POLICY_V0_DB', str(tmp_path / 'app.db'))
    monkeypatch.setenv('POLICY_V0_ACCOUNT_SHADOW', 'a')
    monkeypatch.setenv('POLICY_V0_ANALYSIS_SHADOW', '1')


def reopen_for_projection(book):
    from a_share_research.policy.v0 import account_store
    profile = account_store.read(book[0], book[1], 'a')['profile_identity']
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    buy = event(price='10', broker_id='structural-reopen', occurred_at='2026-11-03T10:00:00+08:00')
    assert submit(book, batch(book, [buy], version=1, shares=100, cash='10200', entries={
        '0': dict(policy_identity=profile, framework='A通用', name='Structural new cycle')}))['status'] == 'posted'
    return position_store.state(book[0], book[1], 'a')['projection']['cycles'][-1]['cycle_id']


def test_combined_projection_refuses_new_cycle_until_explicit_analysis_baseline(book, tmp_path, monkeypatch):
    import sqlite3
    from a_share_research.policy.v0 import db as policy_db, commands_monitor
    analysis.initialize(book[0], book[1], 1)
    configure_projection(book, tmp_path, monkeypatch)
    cycle = reopen_for_projection(book)
    with pytest.raises(sqlite3.OperationalError, match='缺少|基线'):
        policy_db.get_db()
    base = initialize(book, cycle, version=2)
    before = legacy_rows(book[0])
    result = update(book, request(book, [rewrite(retire_l3_ids=[]), open_alert()]), cycle)
    with policy_db.db_session() as projected:
        hid = base['holding_id']
        assert projected.execute('SELECT shares FROM holdings WHERE id=?', (hid,)).fetchone()[0] == 100
        assert projected.execute('SELECT count(*) FROM holding_l3_conditions WHERE holding_id=?', (hid,)).fetchone()[0] == 2
        assert projected.execute('SELECT count(*) FROM holding_alerts WHERE holding_id=?', (hid,)).fetchone()[0] == 1
        assert projected.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            projected.execute('DELETE FROM temp.holding_l3_conditions')
    monitored = commands_monitor._load_monitor_local_snapshot()['holdings']
    assert len(monitored) == 1 and monitored[0]['id'] == hid
    assert len(monitored[0]['l3_conditions']) == 2 and len(monitored[0]['alerts']) == 1
    assert len(monitored[0]['theses']) == 1 and monitored[0]['tier']['tier1_status'] == 'pending'
    sale = event('sell', price='12', broker_id='structural-final-sell', occurred_at='2026-11-04T10:00:00+08:00')
    assert submit(book, batch(book, [sale], version=2, shares=0, cash='11400'))['status'] == 'posted'
    with policy_db.db_session() as projected:
        assert projected.execute('SELECT exit_date FROM holdings WHERE id=?', (hid,)).fetchone()[0] == '2026-11-04'
        assert projected.execute('SELECT count(*) FROM holding_l3_conditions WHERE holding_id=?', (hid,)).fetchone()[0] == 2
    assert current(book, cycle) == result
    assert commands_monitor._load_monitor_local_snapshot()['holdings'] == []
    assert legacy_rows(book[0]) == before


def test_scoped_seed_projection_stays_independent_of_later_legacy_revision(book, tmp_path, monkeypatch):
    from a_share_research.policy.v0 import db as policy_db
    book[0].execute("INSERT INTO holding_tier_state(holding_id,updated_at) VALUES (1,'2026-11-01')")
    old = analysis.initialize(book[0], book[1], 1)
    configure_projection(book, tmp_path, monkeypatch)
    initialize(book, revision=old['revision_id'])
    scoped = update(book)
    analysis.update(book[0], book[1], 1,
        request(book, [dict(kind='tier', tier='tier3', status='completed')]), book[2])
    with policy_db.db_session() as projected:
        row = projected.execute('SELECT tier2_status,tier3_status FROM holding_tier_state WHERE holding_id=1').fetchone()
        assert tuple(row) == ('completed', 'pending')
    assert current(book) == scoped
    assert analysis.inspect(book[0]) == []


def test_combined_seed_without_scoped_baseline_uses_legacy_analysis(book, tmp_path, monkeypatch):
    from a_share_research.policy.v0 import db as policy_db
    book[0].execute("INSERT INTO holding_tier_state(holding_id,updated_at) VALUES (1,'2026-11-01')")
    analysis.initialize(book[0], book[1], 1)
    configure_projection(book, tmp_path, monkeypatch)
    analysis.update(book[0], book[1], 1,
        request(book, [dict(kind='tier', tier='tier3', status='completed')]), book[2])
    with policy_db.db_session() as projected:
        assert projected.execute('SELECT tier3_status FROM holding_tier_state WHERE holding_id=1').fetchone()[0] == 'completed'


def test_http_scope_opt_in_permissions_and_idempotent_analysis_lifecycle(book, tmp_path):
    from fastapi.testclient import TestClient
    from a_share_research.tools.server import create_app
    cycle = reopened(book)
    token = api_keys.create(book[0], 'structural-http-assistant', role='assistant')
    research = api_keys.create(book[0], 'structural-http-research', role='research')
    headers = {'Authorization': 'Bearer ' + token}
    research_headers = {'Authorization': 'Bearer ' + research}
    sha = evidence.store(tmp_path / 'account-evidence', b'Invented HTTP cycle analysis evidence only')
    scope = dict(account_id='a', cycle_id=cycle)
    initial = scope | dict(initialize=dict(expected_account_version=2, evidence_hashes=[sha]))
    endpoint = '/tools/account.cycle_analysis.'
    with TestClient(create_app(tmp_path / 'app.db')) as client:
        assert client.post(endpoint + 'initialize', headers=headers, json=initial).status_code == 404
        assert client.post(endpoint + 'current', headers=headers, json=scope).status_code == 404
    with TestClient(create_app(tmp_path / 'app.db', account_shadow_enabled=True)) as client:
        for name, payload in [('initialize', initial), ('current', scope), ('history', scope),
            ('update', scope | dict(update=request(book, [open_alert()]).model_dump(mode='json')))]:
            assert client.post(endpoint + name, headers=research_headers, json=payload).status_code == 403
        def call(name, payload):
            response = client.post(endpoint + name, headers=headers, json=payload)
            assert response.status_code == 200, response.text
            return response.json()['result']['data']
        base = call('initialize', initial)
        assert call('initialize', initial) == base
        value = request(book, [rewrite(retire_l3_ids=[]), open_alert()], evidence_hashes=[sha])
        args = scope | dict(update=value.model_dump(mode='json'))
        first = call('update', args)
        assert call('update', args) == first
        l3 = first['snapshot']['holding_l3_conditions'][0]['id']
        value = request(book, [dict(kind='l3', id=l3, status='watch', as_of='2026-11-11',
            evidence='Invented HTTP watch evidence')], version=1, evidence_hashes=[sha])
        args = scope | dict(update=value.model_dump(mode='json'))
        second = call('update', args)
        assert call('update', args) == second
        assert second['snapshot']['holding_l3_conditions'][0]['status'] == 'watch'
        assert call('current', scope) == second
        assert call('history', scope)['revisions'] == [base, first, second]
        assert call('history', scope | dict(after_id=first['revision_id']))['revisions'] == [second]
    assert analysis.inspect(book[0]) == []
