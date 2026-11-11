"""Verified-source and authorization contracts; fixtures are structural accounts."""
import json

import pytest

from a_share_research import api_keys, db
from a_share_research.account import mirror
from a_share_research.policy import account_view, registry
from a_share_research.tools.registry import Context, ToolError, invoke
from test_policy_account_input import bound_book as input_book
from test_position_store import batch, event, submit


@pytest.fixture
def bound_book(tmp_path):
    yield from input_book.__wrapped__(tmp_path)


def test_bound_account_reads_only_selected_ledger_and_is_read_only(bound_book):
    path, conn, principal, _, _ = bound_book
    before = list(conn.iterdump())
    result = account_view.current(conn, principal, path)
    assert result['codes'] == ['600001']
    assert result['source']['mode'] == 'ledger'
    assert result['source']['watermark']['account_id'] == 'a'
    assert list(conn.iterdump()) == before


def test_mirror_normalizes_open_codes_and_ignores_closed_history(bound_book):
    path, conn, principal, _, _ = bound_book
    conn.execute("UPDATE holdings SET code='600001.SH' WHERE id=1")
    conn.execute('UPDATE account_source SET mode=?,source_sha256=?,watermark=?',
                 ('mirror', mirror.digest(mirror._read(conn, path.parent)), '{}'))
    assert account_view.current(conn, principal, path)['codes'] == ['600001']
    conn.execute("UPDATE holdings SET code='600099' WHERE id=1")
    with pytest.raises(registry.PolicyError, match='来源哈希'):
        account_view.current(conn, principal, path)


def test_missing_source_and_corrupt_projection_are_not_empty_accounts(bound_book):
    path, conn, principal, _, _ = bound_book
    conn.execute("UPDATE account_books SET projection=?", (json.dumps({'positions': {}, 'cash': '10000'}),))
    with pytest.raises(registry.PolicyError, match='无法核对'):
        account_view.current(conn, principal, path)
    conn.execute('DELETE FROM account_source')
    with pytest.raises(registry.PolicyError, match='已核对'):
        account_view.current(conn, principal, path)


def test_closed_ledger_does_not_resurrect_active_legacy_row(bound_book):
    path, conn, principal, directory, sha = bound_book
    book = (conn, principal, directory, sha)
    assert submit(book, batch(book, [event('sell')], shares=0, cash='11200'))['status'] == 'posted'
    assert conn.execute('SELECT count(*) FROM holdings WHERE exit_date IS NULL').fetchone()[0] == 1
    assert account_view.current(conn, principal, path)['codes'] == []


def test_unconvertible_mirror_code_is_explicit_failure(bound_book):
    path, conn, principal, _, _ = bound_book
    conn.execute("UPDATE holdings SET code='not-an-a-share' WHERE id=1")
    conn.execute('UPDATE account_source SET mode=?,source_sha256=?,watermark=?',
                 ('mirror', mirror.digest(mirror._read(conn, path.parent)), '{}'))
    with pytest.raises(registry.PolicyError, match='代码不能转换'):
        account_view.current(conn, principal, path)


def test_research_revoked_keys_and_disabled_gate_rejected(bound_book):
    path, conn, principal, _, _ = bound_book
    context = Context(conn, path, principal, account_shadow_enabled=True)
    assert invoke(context, 'account.current', {})['data']['codes'] == ['600001']
    with pytest.raises(ToolError) as denied:
        invoke(Context(conn, path, principal), 'account.current', {})
    assert denied.value.status == 404
    research = api_keys.authenticate(conn, api_keys.create(conn, 'current-research', role='research'))
    with pytest.raises(ToolError) as denied:
        invoke(Context(conn, path, research, account_shadow_enabled=True), 'account.current', {})
    assert denied.value.status == 403
    conn.execute('DELETE FROM api_keys WHERE id=?', (principal.key_id,))
    with pytest.raises(ToolError) as denied:
        invoke(context, 'account.current', {})
    assert denied.value.status == 403


def test_verified_empty_mirror_is_distinct_from_missing_source(tmp_path):
    path = tmp_path / 'app.db'
    with db.connect(path) as conn:
        principal = api_keys.authenticate(conn, api_keys.create(conn, 'empty', role='assistant'))
        conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',
                     ('mirror', mirror.digest(mirror._read(conn, tmp_path)), db.now(), '{}'))
        assert account_view.current(conn, principal, path)['codes'] == []
