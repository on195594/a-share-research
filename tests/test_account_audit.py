"""Structural accounting fixtures, not broker evidence or investment validation."""
import sqlite3

import pytest

from a_share_research.account import audit
from test_account_ledger import book as book
from test_account_ledger import batch, submit


@pytest.fixture
def posted(request):
    account = request.getfixturevalue("book")
    submit(account, batch(account))
    return account


def corrupt(conn, sql):
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'account_%'").fetchall():
        conn.execute('DROP TRIGGER ' + row[0])
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute(sql)


def test_clean_conflicts_duplicates_and_corrections(posted):
    conn = posted[0]
    assert audit.inspect(conn) == []
    submit(posted, batch(posted, version=1))
    submit(posted, batch(posted, version=0))
    assert audit.inspect(conn) == []
    from test_account_ledger import buy
    event_id = conn.execute('SELECT id FROM account_journal').fetchone()[0]
    submit(posted, batch(posted, version=1, events=[buy(price='1401')], cash='59900', voids=[event_id]))
    assert audit.inspect(conn) == []


@pytest.mark.parametrize('sql,expected', [
    ("UPDATE account_journal SET business_id='wrong'", '业务身份'),
    ("UPDATE account_journal SET account_id='missing'", '归属'),
    ("UPDATE account_journal SET company_key='000001'", '公司/时间'),
    ("UPDATE account_batches SET request_hash='wrong'", '哈希'),
    ("UPDATE account_batches SET receipt=json_set(receipt,'$.status','conflict')", '状态'),
    ("UPDATE account_batches SET status='conflict'", '未入账'),
    ("UPDATE account_books SET projection=json_set(projection,'$.cash','0')", '投影'),
    ("UPDATE account_books SET version=99", '版本'),
    ("UPDATE account_journal SET event='{}'", '非法'),
])
def test_tampering_is_reported(posted, sql, expected):
    corrupt(posted[0], sql)
    assert any(expected in p for p in audit.inspect(posted[0]))


def test_duplicate_active_business_id(posted):
    conn = posted[0]
    conn.execute('INSERT INTO account_journal(account_id,batch_id,business_id,company_key,occurred_at,event) '
                 'SELECT account_id,batch_id,business_id,company_key,occurred_at,event FROM account_journal')
    assert any('重复' in p for p in audit.inspect(conn))


def test_legacy_database():
    conn = sqlite3.connect(':memory:')
    assert audit.inspect(conn) == []
    conn.close()
