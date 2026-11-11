"""Structural cutover contracts only; invented fixtures are not broker acceptance."""
import hashlib
import json
import sqlite3

import pytest

from a_share_research import db, gate
from a_share_research.account import analysis, cycle_analysis, ledger, mirror
from a_share_research.policy import cutover
from test_policy_account_input import bound_book as ledger_book
from test_position_store import batch, event


def refresh(book):
    path, conn, _, _, _ = book
    content = mirror._read(conn, path.parent)
    watermark = dict(rows={t: len(rows) for t, rows in content['tables'].items()},
                     sequences=content['sequences'],
                     files={name: data and hashlib.sha256(data).hexdigest()
                            for name, data in content['files'].items()})
    row = ('mirror', mirror.digest(content), db.now(), gate.canonical(watermark))
    conn.execute('UPDATE account_source SET mode=?,source_sha256=?,updated_at=?,watermark=? WHERE id=1', row)
    return row


@pytest.fixture
def book(tmp_path):
    for value in ledger_book.__wrapped__(tmp_path):
        refresh(value)
        yield value


def validate(book, row=None):
    path, conn, _, _, _ = book
    row = row or tuple(conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source').fetchone())
    with db.transaction(conn, immediate=False):
        return cutover.validate(conn, path.parent, 'a', 'structural-scope', row)


def test_ready_validation_is_read_only(book):
    before = list(book[1].iterdump())
    files = {p: p.read_bytes() for p in book[3].iterdir()}
    result = validate(book)
    assert result['mode'] == 'ledger' and result['account_version'] == 0
    assert list(book[1].iterdump()) == before
    assert {p: p.read_bytes() for p in book[3].iterdir()} == files


def test_requires_caller_transaction(book):
    with pytest.raises(cutover.CutoverError, match='事务'):
        cutover.validate(book[1], book[0].parent, 'a', 'structural-scope', refresh(book))


def test_read_only_plain_sqlite_connection(book):
    with sqlite3.connect(book[0].as_uri() + '?mode=ro', uri=True) as conn:
        conn.execute('BEGIN')
        row = conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source').fetchone()
        result = cutover.validate(conn, book[0].parent, 'a', 'structural-scope', row)
        assert conn.in_transaction and conn.total_changes == 0
        assert result == validate(book)


@pytest.mark.parametrize('sql', [
    "UPDATE holdings SET name='changed' WHERE id=1",
    "UPDATE holdings SET name='changed closed history' WHERE id=2",
    "UPDATE holding_events SET notes='changed preopening history' WHERE id=20",
])
def test_refreshed_mirror_cannot_reuse_stale_policy_opening(book, sql):
    book[1].execute(sql)
    refresh(book)
    with pytest.raises(cutover.CutoverError, match='政策期初'):
        validate(book)


@pytest.mark.parametrize('variant', ['hash', 'watermark', 'mode'])
def test_source_must_match_actual_mirror(book, variant):
    row = list(refresh(book))
    if variant == 'hash':
        row[1] = '0' * 64
    elif variant == 'mode':
        row[0] = 'ledger'
    else:
        watermark = json.loads(row[3])
        watermark['rows']['holdings'] = True
        row[3] = gate.canonical(watermark)
    with pytest.raises(cutover.CutoverError):
        validate(book, tuple(row))


def update_analysis(book, scoped=False):
    request = analysis.Update(expected_version=0, evidence_hashes=[book[4]], reason='Structural branch update',
                              changes=[dict(kind='alert_open', level='yellow', category='unverified',
                                            reason_code='structural', reason='Structural change', evidence='Fixture only')])
    if scoped:
        cycle_analysis.update(book[1], book[2], 'a', 'legacy:1', request, book[3])
    else:
        analysis.update(book[1], book[2], 1, request, book[3])


def test_later_scoped_analysis_is_preserved(book):
    update_analysis(book, scoped=True)
    assert validate(book)['account_version'] == 0


def test_later_legacy_analysis_requires_new_scoped_baseline(book):
    update_analysis(book)
    with pytest.raises(cutover.CutoverError, match='当前旧分析版本'):
        validate(book)


def test_refreshed_analysis_mirror_requires_reconciliation(book):
    book[1].execute("INSERT INTO holding_tier_state(holding_id,tier1_status,tier2_status,tier3_status,updated_at) VALUES(1,'completed','pending','pending',?)", (db.now(),))
    refresh(book)
    with pytest.raises(cutover.CutoverError, match='旧分析基线'):
        validate(book)


@pytest.mark.parametrize('table', ['account_books', 'account_position_books'])
def test_nonzero_version_is_not_opening(book, table):
    book[1].execute(f'UPDATE {table} SET version=1')
    with pytest.raises(cutover.CutoverError, match='零版本'):
        validate(book)


def test_journal_cannot_be_hidden_by_reset_version(book):
    legacy_fixture = (book[1], book[2], book[3], book[4])
    assert ledger.submit(book[1], book[2], 'a', batch(legacy_fixture, [event()]), book[3])['status'] == 'posted'
    book[1].execute('UPDATE account_books SET version=0')
    book[1].execute('UPDATE account_position_books SET version=0')
    with pytest.raises(cutover.CutoverError, match='金融入账'):
        validate(book)


def test_missing_original_evidence_fails_closed(book):
    (book[3] / book[4]).unlink()
    with pytest.raises(cutover.CutoverError, match='证据校验失败'):
        validate(book)
