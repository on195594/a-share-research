"""Card cache hits, eviction and SQL edits against captured production reports."""

import hashlib
import json
import sqlite3

import pytest

from a_share_research import db, reading
from support.continuation import BODY, load


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'cards.db')
    load(connection)
    reading._report_card.cache_clear()
    try:
        yield connection
    finally:
        reading._report_card.cache_clear()
        connection.close()


def cards(conn):
    reports = reading.latest_reports(conn, include_checkpoint=False)
    passed = {r['code']: r for r in reading.latest_reports(conn, passed=True, include_checkpoint=False)}
    return reading.report_cards(conn, reports, passed)


def test_warm_cards_skip_body_reads_and_reuse_parsing(conn):
    expected = cards(conn)
    misses = reading._report_card.cache_info().misses
    assert misses == len(expected)
    assert all(c == reading.card(r, BODY[r['id']]) | {'from_batch': r['from_batch']}
               for c, r in zip(expected, reading.latest_reports(conn, include_checkpoint=False)))

    def deny_body(action, table, column, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and (table, column) == ('reports', 'body') else sqlite3.SQLITE_OK

    conn.set_authorizer(deny_body)
    try:
        actual = cards(conn)
        assert actual == expected
        assert reading._report_card.cache_info().hits == len(expected)
        # UI consumers can edit a returned card without modifying future responses.
        actual[0]['dims'].clear()
        assert cards(conn) == expected
        assert reading._report_card.cache_info().misses == misses
        assert reading.report_cards(conn, [], {}) == []
    finally:
        conn.set_authorizer(None)


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('recorded_hash', [False, True])
def test_body_only_sql_edits_invalidate_cards_even_with_stale_or_missing_hash(conn, tmp_path, external, recorded_hash):
    original = json.dumps(BODY[39], ensure_ascii=False)
    if recorded_hash:
        conn.execute('UPDATE reports SET body_sha256=? WHERE id=39',
                     (hashlib.sha256(original.encode()).hexdigest(),))
    before = cards(conn)
    writer = db.connect(tmp_path / 'cards.db') if external else conn
    try:
        body = dict(BODY[39])
        body.pop('table')
        writer.execute('UPDATE reports SET body=? WHERE id=39', (json.dumps(body, ensure_ascii=False),))
        after = cards(conn)
        assert before[0]['dims'] and not after[0]['dims']
        assert after[1:] == before[1:]
        assert cards(conn) == after
        # Replacing the body again must not resurrect its old cached contents.
        writer.execute('UPDATE reports SET body=? WHERE id=39', (original,))
        assert cards(conn) == before
    finally:
        if external:
            writer.close()


def test_cards_are_isolated_between_databases_and_rollback_is_not_cached(conn, tmp_path):
    expected = cards(conn)
    other = db.connect(tmp_path / 'other.db')
    try:
        load(other)
        other.execute('UPDATE reports SET passed=0 WHERE id=39')
        failed = cards(other)[0]
        assert failed['bucket'] == '复核未通过' and not failed['dims']
        assert failed['last_passed']['id'] == 37
        assert cards(conn) == expected
        conn.execute('BEGIN')
        conn.execute('UPDATE reports SET passed=0 WHERE id=39')
        assert cards(conn)[0]['last_passed']['id'] == 37
        conn.rollback()
        assert cards(conn) == expected
    finally:
        other.close()


def test_card_lru_is_bounded_and_retains_recent_entries(conn):
    report = reading.latest_reports(conn, include_checkpoint=False)[0]
    metadata = tuple((k, report[k]) for k in
                     ('id', 'code', 'name', 'passed', 'verdict', 'created_at', 'coverage'))
    version = conn.execute('PRAGMA data_version').fetchone()[0]
    first = (version, conn.total_changes)
    reading._report_card(conn, first, metadata)
    capacity = reading._report_card.cache_info().maxsize
    # Real unchanged report data across write generations exercises the actual bounded LRU.
    for index in range(capacity - 1):
        conn.execute('UPDATE reports SET body=body WHERE id=?', (report['id'],))
        latest = (version, conn.total_changes)
        if index == 0:
            evicted = latest
        reading._report_card(conn, latest, metadata)
    reading._report_card(conn, first, metadata)  # Keep the oldest entry recently used.
    conn.execute('UPDATE reports SET body=body WHERE id=?', (report['id'],))
    reading._report_card(conn, (version, conn.total_changes), metadata)
    before = reading._report_card.cache_info()
    reading._report_card(conn, first, metadata)
    reading._report_card(conn, latest, metadata)
    after = reading._report_card.cache_info()
    assert after.currsize == capacity and after.hits == before.hits + 2
    assert after.misses == before.misses
    reading._report_card(conn, evicted, metadata)
    assert reading._report_card.cache_info().misses == after.misses + 1
