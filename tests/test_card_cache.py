"""Cross-connection card caching against captured production reports."""

from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
import json
import sqlite3
from threading import Barrier, get_ident
import weakref

import pytest

from a_share_research import db, reading
from support.continuation import BODY, load


def record_hashes(connection):
    for row in connection.execute('SELECT id, body FROM reports'):
        connection.execute('UPDATE reports SET body_sha256=? WHERE id=?',
                           (hashlib.sha256(row['body'].encode()).hexdigest(), row['id']))


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'cards.db')
    load(connection)
    record_hashes(connection)
    reading._card_cache.clear()
    try:
        yield connection
    finally:
        reading._card_cache.clear()
        connection.close()


def cards(conn):
    reports = reading.latest_reports(conn, include_checkpoint=False)
    passed = {r['code']: r for r in reading.latest_reports(conn, passed=True, include_checkpoint=False)}
    return reading.report_cards(conn, reports, passed)


def deny_body(action, table, column, *_):
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and (table, column) == ('reports', 'body') else sqlite3.SQLITE_OK


def body_reads(statements):
    return [sql for sql in statements if sql.startswith('SELECT id, body FROM reports')]


def test_warm_cards_skip_body_reads_and_reuse_parsing(conn):
    statements = []
    conn.set_trace_callback(statements.append)
    expected = cards(conn)
    conn.set_trace_callback(None)
    assert len(body_reads(statements)) == 1  # All cold cards are fetched together.
    assert len(reading._card_cache) == len(expected)
    assert all(c == reading.card(r, BODY[r['id']]) | {'from_batch': r['from_batch']}
               for c, r in zip(expected, reading.latest_reports(conn, include_checkpoint=False)))
    cached = dict(reading._card_cache)

    conn.set_authorizer(deny_body)
    try:
        actual = cards(conn)
        assert actual == expected
        # UI consumers can edit a returned card without modifying future responses.
        actual[0]['dims'].clear()
        assert cards(conn) == expected
        assert all(reading._card_cache[key] is value for key, value in cached.items())
        assert reading.report_cards(conn, [], {}) == []
    finally:
        conn.set_authorizer(None)


def test_independent_threads_and_connections_hit_every_warm_card(conn, tmp_path):
    expected = cards(conn)
    cached = dict(reading._card_cache)
    barrier = Barrier(4)

    def read_in_thread(_):
        barrier.wait()
        for _ in range(3):
            connection = db.connect(tmp_path / 'cards.db')
            try:
                connection.set_authorizer(deny_body)
                assert cards(connection) == expected
            finally:
                connection.close()
        return get_ident()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(read_in_thread, range(4)))) == 4
    assert len(reading._card_cache) == len(cached)
    assert all(reading._card_cache[key] is value for key, value in cached.items())


class WeakConnection(sqlite3.Connection):
    """A real SQLite connection that supports weak references."""


def test_cache_does_not_retain_closed_connections(conn, tmp_path):
    connection = sqlite3.connect(tmp_path / 'cards.db', factory=WeakConnection, isolation_level=None)
    connection.row_factory = sqlite3.Row
    reference = weakref.ref(connection)
    expected = cards(connection)  # Populate the cache through the tracked connection.
    connection.close()
    del connection
    gc.collect()
    assert reference() is None
    conn.set_authorizer(deny_body)
    try:
        assert cards(conn) == expected
    finally:
        conn.set_authorizer(None)


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('recorded_hash', [False, True])
def test_body_edits_invalidate_cards_and_legacy_reports_bypass_cache(conn, tmp_path, external, recorded_hash):
    original = json.dumps(BODY[39], ensure_ascii=False)
    if not recorded_hash:
        conn.execute('UPDATE reports SET body_sha256=NULL WHERE id=39')
    before = cards(conn)
    writer = db.connect(tmp_path / 'cards.db') if external else conn
    try:
        body = dict(BODY[39])
        body.pop('table')
        for text in (json.dumps(body, ensure_ascii=False), original):
            sha = hashlib.sha256(text.encode()).hexdigest() if recorded_hash else None
            writer.execute('UPDATE reports SET body=?, body_sha256=? WHERE id=39', (text, sha))
            statements = []
            conn.set_trace_callback(statements.append)
            after = cards(conn)
            conn.set_trace_callback(None)
            assert bool(after[0]['dims']) == (text == original)
            assert after[1:] == before[1:]
            assert cards(conn) == after
            # A partial miss fetches only that report, never the already cached bodies.
            assert body_reads(statements) == ([] if recorded_hash and text == original else
                                             ['SELECT id, body FROM reports WHERE id IN (39)'])
        assert cards(conn) == before
        if not recorded_hash:
            assert all(key[1] != 39 for key in reading._card_cache)
    finally:
        conn.set_trace_callback(None)
        if external:
            writer.close()


def test_cards_are_isolated_between_databases_and_rollback_is_not_cached(conn, tmp_path):
    expected = cards(conn)
    cached = dict(reading._card_cache)
    other = db.connect(tmp_path / 'other.db')
    try:
        load(other)
        record_hashes(other)
        assert cards(other) == expected
        assert len(reading._card_cache) == 2 * len(cached)
        other.execute('UPDATE reports SET passed=0 WHERE id=39')
        failed = cards(other)[0]
        assert failed['bucket'] == '复核未通过' and not failed['dims']
        assert failed['last_passed']['id'] == 37
        assert cards(conn) == expected
        cached = dict(reading._card_cache)
        conn.execute('BEGIN')
        conn.execute('UPDATE reports SET passed=0 WHERE id=39')
        assert cards(conn)[0]['last_passed']['id'] == 37
        assert dict(reading._card_cache) == cached
        conn.rollback()
        assert cards(conn) == expected
    finally:
        other.close()


def test_metadata_changes_invalidate_only_affected_cards(conn):
    before = cards(conn)
    conn.execute('UPDATE reports SET passed=0 WHERE id=39')
    statements = []
    conn.set_trace_callback(statements.append)
    after = cards(conn)
    conn.set_trace_callback(None)
    assert after[0]['bucket'] == '复核未通过' and not after[0]['dims']
    assert after[0]['last_passed']['id'] == 37
    assert after[1:] == before[1:]
    assert body_reads(statements) == ['SELECT id, body FROM reports WHERE id IN (39)']


def test_mismatched_body_hash_is_not_cached(conn):
    original = json.dumps(BODY[39], ensure_ascii=False)
    body = dict(BODY[39])
    body.pop('table')
    conn.execute('UPDATE reports SET body=? WHERE id=39', (json.dumps(body, ensure_ascii=False),))
    assert not cards(conn)[0]['dims']
    assert all(key[1] != 39 for key in reading._card_cache)
    conn.execute('UPDATE reports SET body=? WHERE id=39', (original,))
    assert cards(conn)[0]['dims']


def test_unrelated_writes_preserve_cached_cards(conn, tmp_path):
    expected = cards(conn)
    for writer in (conn, db.connect(tmp_path / 'cards.db')):
        try:
            db.create_run(writer, '600519.SH')
            conn.set_authorizer(deny_body)
            assert cards(conn) == expected
        finally:
            conn.set_authorizer(None)
            if writer is not conn:
                writer.close()


def test_in_memory_databases_bypass_cache(conn):
    expected = cards(conn)
    cached = dict(reading._card_cache)
    other = sqlite3.connect(':memory:', isolation_level=None)
    other.row_factory = sqlite3.Row
    try:
        conn.backup(other)
        assert cards(other) == expected
        other.execute('UPDATE reports SET passed=0 WHERE id=39')
        assert cards(other)[0]['bucket'] == '复核未通过'
        assert dict(reading._card_cache) == cached
    finally:
        other.close()


def test_card_lru_is_bounded_and_retains_recent_entries(conn, tmp_path):
    report = reading.latest_reports(conn, include_checkpoint=False)[0]
    expected = reading.report_cards(conn, [report], {})
    first = next(iter(reading._card_cache))
    # Distinct real databases containing the captured report fill the bounded cache.
    for index in range(reading._CARD_CACHE_SIZE):
        other = sqlite3.connect(tmp_path / f'copy-{index}.db', isolation_level=None)
        other.row_factory = sqlite3.Row
        try:
            conn.backup(other)
            if index == reading._CARD_CACHE_SIZE - 1:
                evicted = next(key for key in reading._card_cache if key != first)
                assert reading.report_cards(conn, [report], {}) == expected  # Refresh the oldest entry.
            assert reading.report_cards(other, [report], {}) == expected
        finally:
            other.close()
    assert len(reading._card_cache) == reading._CARD_CACHE_SIZE
    assert first in reading._card_cache and evicted not in reading._card_cache
    conn.set_authorizer(deny_body)
    try:
        assert reading.report_cards(conn, [report], {}) == expected
    finally:
        conn.set_authorizer(None)
