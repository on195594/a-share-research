"""Invented structural transition fixtures only; no new investment or broker evidence."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from a_share_research.account import analysis, ledger
from test_account_analysis import branch as _branch_fixture, request, append, legacy_rows

branch = _branch_fixture


def condition(**overrides):
    return dict(condition='Structural replacement condition', scope='core_driver', action='review',
                materiality_basis='Structural materiality', temporary_exit_rule='Structural exit rule',
                evidence_cadence='quarterly') | overrides


def rewrite(**overrides):
    return dict(kind='thesis', l1='Structural replacement L1', l2='Structural replacement L2',
                rewrite_reason='Structural rewrite', retire_l3_ids=[1], new_l3=[condition(), condition(scope='non_core')]) | overrides


def open_alert(reason_code='structural-new', **overrides):
    return dict(kind='alert_open', level='red', category='unverified', reason_code=reason_code,
                reason='Structural alert reason', evidence='Structural alert evidence', review_due='2026-11-12') | overrides


def test_rewrite_preserves_prior_revisions_and_atomically_links_new_conditions(branch):
    conn, principal, *_ = branch
    baseline = analysis.initialize(conn, principal, 1)
    legacy = legacy_rows(conn)
    value = request(branch, [rewrite()])
    result = append(branch, value)
    assert append(branch, value) == result
    assert result['version'] == 1 and result['parent_id'] == baseline['revision_id']
    snapshot = result['snapshot']
    theses = snapshot['holding_thesis_versions']
    assert len(theses) == 2
    assert theses[0]['status'] == 'superseded' and theses[0]['l1'] == 'Structural L1'
    assert theses[1]['status'] == 'active' and theses[1]['version'] == 2
    assert theses[1]['rewrite_reason'] == 'Structural rewrite'
    old, retired, *new = snapshot['holding_l3_conditions']
    assert old['id'] == 1 and old['is_active'] == 0 and old['retired_reason'] == 'Structural rewrite'
    assert old['retired_at'] and old['condition_text'] == 'Structural condition'
    assert retired == baseline['snapshot']['holding_l3_conditions'][1]
    assert len(new) == 2 and len({r['id'] for r in new}) == 2
    for row in new:
        assert row['is_active'] == 1 and row['status'] == 'pending'
        assert row['thesis_version_id'] == theses[1]['id']
        assert row['evidence'] is None and row['as_of'] is None and row['origin_type'] == 'new_monitoring'
    assert analysis.history(conn, principal, 1)['revisions'][0] == baseline
    assert legacy_rows(conn) == legacy
    assert analysis.inspect(conn) == []
    with pytest.raises(ledger.LedgerError, match='版本冲突'):
        append(branch, request(branch, [rewrite(l1='Different structural L1')]))


@pytest.mark.parametrize('ids', [[], [2], [3], [1, 2], [1, 3], [999]])
def test_incomplete_cross_holding_or_retired_replacement_rejected_atomically(branch, ids):
    conn, principal, *_ = branch
    baseline = analysis.initialize(conn, principal, 1)
    value = request(branch, [open_alert(), rewrite(retire_l3_ids=ids)])
    with pytest.raises(ledger.LedgerError, match='全部活动'):
        append(branch, value)
    assert analysis.current(conn, principal, 1) == baseline
    assert len(analysis.history(conn, principal, 1)['revisions']) == 1


def test_duplicate_retirement_and_mixed_l3_updates_are_rejected(branch):
    with pytest.raises(ValidationError, match='不能重复'):
        request(branch, [rewrite(retire_l3_ids=[1, 1])])
    with pytest.raises(ValidationError, match='分批'):
        request(branch, [rewrite(), dict(kind='l3', id=1, status='watch', as_of='2026-11-11', evidence='Structural')])


@pytest.mark.parametrize('action', ['reduce', 'exit'])
def test_non_core_conditions_cannot_request_trades(branch, action):
    with pytest.raises(ValidationError, match='non_core'):
        request(branch, [rewrite(new_l3=[condition(scope='non_core', action=action)])])


def test_global_ids_stay_distinct_across_holdings_and_rewrite_generations(branch):
    conn, principal, directory, _ = branch
    analysis.initialize(conn, principal, 1)
    analysis.initialize(conn, principal, 2)
    first = append(branch, request(branch, [rewrite()]))
    second = analysis.update(conn, principal, 2, request(branch, [rewrite(retire_l3_ids=[3]), open_alert()]), directory)
    first_ids = {r['id'] for r in first['snapshot']['holding_l3_conditions']}
    second_ids = {r['id'] for r in second['snapshot']['holding_l3_conditions']}
    assert first_ids.isdisjoint(second_ids)
    first_thesis_ids = {r['id'] for r in first['snapshot']['holding_thesis_versions']}
    second_thesis_ids = {r['id'] for r in second['snapshot']['holding_thesis_versions']}
    assert first_thesis_ids.isdisjoint(second_thesis_ids)
    active_ids = [r['id'] for r in first['snapshot']['holding_l3_conditions'] if r['is_active']]
    third = append(branch, request(branch, [rewrite(retire_l3_ids=active_ids), open_alert()], version=1))
    newest_ids = {r['id'] for r in third['snapshot']['holding_l3_conditions'] if r['is_active']}
    assert newest_ids.isdisjoint(first_ids | second_ids)
    a = third['snapshot']['holding_alerts'][-1]
    b = second['snapshot']['holding_alerts'][-1]
    assert a['id'] != b['id'] and a['holding_id'] == 1 and b['holding_id'] == 2
    assert analysis.inspect(conn) == []


def test_failure_after_rewrite_does_not_publish_half_replacement(branch):
    conn, principal, *_ = branch
    baseline = analysis.initialize(conn, principal, 1)
    value = request(branch, [rewrite(), dict(kind='alert', id=3, status='pending', evidence='Structural invalid transition')])
    with pytest.raises(ledger.LedgerError):
        append(branch, value)
    assert analysis.current(conn, principal, 1) == baseline
    assert conn.execute('SELECT count(*) FROM account_analysis_history').fetchone()[0] == 1


@pytest.mark.parametrize('reason_code,expected_id,expected_status', [('structural-1',1,'active'), ('structural-2',2,'pending')])
def test_open_reuses_unresolved_reason_and_keeps_pending_status(branch, reason_code, expected_id, expected_status):
    conn, principal, *_ = branch
    base = analysis.initialize(conn, principal, 1)
    result = append(branch, request(branch, [open_alert(reason_code)]))
    alerts = result['snapshot']['holding_alerts']
    assert len(alerts) == len(base['snapshot']['holding_alerts'])
    updated = next(r for r in alerts if r['id'] == expected_id)
    assert updated['status'] == expected_status and updated['level'] == 'red'
    assert updated['reason'] == 'Structural alert reason' and updated['review_due'] == '2026-11-12'
    assert updated['opened_at'] == '2026-11-01'


def test_liquidity_defer_preserves_original_deadline(branch):
    conn, principal, *_ = branch
    conn.execute("UPDATE holding_alerts SET reason_code='price_stop2_liquidity_defer', review_due='2026-11-11T16:00:00+08:00' WHERE id=1")
    analysis.initialize(conn, principal, 1)
    result = append(branch, request(branch, [open_alert('price_stop2_liquidity_defer', review_due='2026-12-01')]))
    row = result['snapshot']['holding_alerts'][0]
    assert row['review_due'] == '2026-11-11T16:00:00+08:00'
    assert row['reason'] == 'Structural alert reason'


def test_open_after_resolution_creates_new_entity_and_retries_are_idempotent(branch):
    conn, principal, *_ = branch
    base = analysis.initialize(conn, principal, 1)
    old = deepcopy(base['snapshot']['holding_alerts'][2])
    value = request(branch, [open_alert('structural-3')])
    result = append(branch, value)
    assert append(branch, value) == result
    alerts = result['snapshot']['holding_alerts']
    assert alerts[2] == old
    assert alerts[-1]['id'] != old['id'] and alerts[-1]['status'] == 'active'
    assert alerts[-1]['reason_code'] == old['reason_code'] and len(alerts) == 4


@pytest.mark.parametrize('due', ['2026-02-30', 'not-a-date', '2026-11-11T16:00:00', '', 123])
def test_invalid_dates_or_timezone_free_times_rejected(branch, due):
    with pytest.raises(ValidationError):
        request(branch, [open_alert(review_due=due)])


@pytest.mark.parametrize('due', [None, '2026-11-12', '2026-11-12T16:00:00+08:00'])
def test_valid_deadline_formats_survive_append(branch, due):
    conn, principal, *_ = branch
    analysis.initialize(conn, principal, 1)
    result = append(branch, request(branch, [open_alert(review_due=due)]))
    assert result['snapshot']['holding_alerts'][-1]['review_due'] == due


def test_new_entity_ids_do_not_reuse_legacy_autoincrement_high_watermark(branch):
    conn, principal, *_ = branch
    for table in ('holding_l3_conditions', 'holding_thesis_versions', 'holding_alerts'):
        conn.execute('UPDATE sqlite_sequence SET seq=1000 WHERE name=?', (table,))
    analysis.initialize(conn, principal, 1)
    result = append(branch, request(branch, [rewrite(), open_alert()]))
    snapshot = result['snapshot']
    assert snapshot['holding_thesis_versions'][-1]['id'] == 1001
    assert [r['id'] for r in snapshot['holding_l3_conditions'] if r['is_active']] == [1001, 1002]
    assert snapshot['holding_alerts'][-1]['id'] == 1001
