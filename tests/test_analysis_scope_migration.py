"""Structural migration rows only; no account or investment evidence is asserted."""
import sqlite3

import pytest

from a_share_research import db
from a_share_research.account import analysis_scope

_COLUMNS = ('id','holding_id','company_key','version','parent_id','request_hash','source','actor_key_id',
            'created_at','snapshot_hash','evidence_hashes','request','snapshot')


@pytest.fixture
def legacy():
    conn=sqlite3.connect(':memory:',isolation_level=None)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('''CREATE TABLE account_analysis_history (
        id INTEGER PRIMARY KEY, holding_id INTEGER NOT NULL, company_key TEXT,
        version INTEGER NOT NULL, parent_id INTEGER REFERENCES account_analysis_history(id),
        request_hash TEXT NOT NULL, source TEXT NOT NULL, actor_key_id INTEGER,
        created_at TEXT NOT NULL, snapshot_hash TEXT NOT NULL, evidence_hashes TEXT NOT NULL, request TEXT NOT NULL, snapshot TEXT NOT NULL,
        UNIQUE(holding_id,version), UNIQUE(holding_id,request_hash))''')
    conn.execute('CREATE INDEX account_analysis_company ON account_analysis_history(company_key,id)')
    for operation in ('UPDATE','DELETE'):
        conn.execute(f"CREATE TRIGGER account_analysis_no_{operation.lower()} BEFORE {operation} ON account_analysis_history BEGIN SELECT RAISE(ABORT,'analysis history is append-only'); END")
    for identity,holding,version,parent in ((10,1,0,None),(20,1,1,10),(30,2,0,None)):
        insert(conn,identity,holding,version,parent)
    yield conn
    conn.close()


def insert(conn, identity, holding, version=0, parent=None, **scope):
    values=dict(id=identity,holding_id=holding,company_key='600001',version=version,parent_id=parent,
        request_hash=f'request-{identity}',source='structural',actor_key_id=None,
        created_at='2026-11-11T10:00:00+08:00',snapshot_hash=f'snapshot-{identity}',
        evidence_hashes='["原始证据"]',request='{ "preserve whitespace": true }',snapshot='{"state": "原值"}')
    values.update(scope)
    conn.execute(f"INSERT INTO account_analysis_history ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",tuple(values.values()))


def rows(conn):
    return [tuple(row) for row in conn.execute(f"SELECT {','.join(_COLUMNS)} FROM account_analysis_history ORDER BY id")]


def migrate(conn):
    with db.transaction(conn):
        analysis_scope.migrate(conn)


def test_nonempty_legacy_rows_hashes_and_self_parent_are_preserved(legacy):
    before=rows(legacy)
    schema=[tuple(r) for r in legacy.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='account_analysis_history' AND sql IS NOT NULL AND type IN ('index','trigger') ORDER BY type,name")]
    migrate(legacy)
    assert rows(legacy)==before
    assert [tuple(r) for r in legacy.execute('SELECT account_id,cycle_id FROM account_analysis_history')]==[('','')]*3
    # This is the original unscoped latest query, before callers start passing account scope.
    latest=legacy.execute('SELECT * FROM account_analysis_history WHERE holding_id=? ORDER BY version DESC LIMIT 1',(1,)).fetchone()
    assert latest['id']==20 and latest['parent_id']==10
    assert legacy.execute('PRAGMA foreign_key_check').fetchall()==[]
    foreign=legacy.execute('PRAGMA foreign_key_list(account_analysis_history)').fetchall()
    assert len(foreign)==1 and foreign[0]['table']=='account_analysis_history'
    for kind,name,sql in schema:
        assert legacy.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',(kind,name)).fetchone()[0]==sql
    for command in ('UPDATE account_analysis_history SET source=source','DELETE FROM account_analysis_history'):
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):
            legacy.execute(command)


def test_scoped_holdings_and_cycles_have_independent_unique_keys(legacy):
    migrate(legacy)
    insert(legacy,40,1,account_id='a',cycle_id='cycle:a')
    insert(legacy,50,1,account_id='b',cycle_id='cycle:b')
    # Request hashes can repeat across account branches, never within a holding or cycle.
    insert(legacy,60,2,account_id='a',cycle_id='cycle:second',request_hash='request-50')
    for identity,holding,version,account,cycle,request_hash in (
        (70,1,0,'a','cycle:other','new'),
        (71,3,0,'a','cycle:a','new'),
        (72,1,1,'a','cycle:other','request-40'),
        (73,3,1,'a','cycle:a','request-40'),
    ):
        with pytest.raises(sqlite3.IntegrityError,match='UNIQUE'):
            insert(legacy,identity,holding,version,account_id=account,cycle_id=cycle,request_hash=request_hash)


@pytest.mark.parametrize('account,cycle',[('a',''),('','cycle:a'),(None,'cycle:a'),('a',None)])
def test_empty_scope_pairing_or_null_is_rejected(legacy,account,cycle):
    migrate(legacy)
    with pytest.raises(sqlite3.IntegrityError):
        insert(legacy,40,3,account_id=account,cycle_id=cycle)


def test_parent_foreign_key_remains_enforced_after_migration(legacy):
    migrate(legacy)
    insert(legacy,40,1,2,20)
    with pytest.raises(sqlite3.IntegrityError,match='FOREIGN KEY'):
        insert(legacy,50,1,3,999)


def test_migration_is_reentrant_including_new_scoped_rows(legacy):
    migrate(legacy)
    insert(legacy,40,1,account_id='a',cycle_id='cycle:a')
    before=[tuple(r) for r in legacy.execute('SELECT * FROM account_analysis_history ORDER BY id')]
    schema=[tuple(r) for r in legacy.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='account_analysis_history' ORDER BY type,name")]
    migrate(legacy)
    assert [tuple(r) for r in legacy.execute('SELECT * FROM account_analysis_history ORDER BY id')]==before
    assert [tuple(r) for r in legacy.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='account_analysis_history' ORDER BY type,name")]==schema


def test_mid_rebuild_sql_failure_rolls_back_original_schema_and_data(legacy):
    before=rows(legacy)
    schema=legacy.execute("SELECT sql FROM sqlite_master WHERE name='account_analysis_history'").fetchone()[0]
    def deny_index(action, first, second, database, trigger):
        if action==sqlite3.SQLITE_CREATE_INDEX and first=='account_analysis_cycle_version':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    legacy.set_authorizer(deny_index)
    with pytest.raises(sqlite3.DatabaseError):
        migrate(legacy)
    legacy.set_authorizer(None)
    assert rows(legacy)==before
    assert legacy.execute("SELECT sql FROM sqlite_master WHERE name='account_analysis_history'").fetchone()[0]==schema
    assert not legacy.in_transaction
    assert not legacy.execute("SELECT 1 FROM sqlite_master WHERE name='account_analysis_history_scope_migration'").fetchone()
    migrate(legacy)
    assert rows(legacy)==before


def test_corrupt_original_parent_fails_without_changing_original(legacy):
    legacy.execute('PRAGMA foreign_keys=OFF')
    insert(legacy,40,3,parent=999)
    legacy.execute('PRAGMA foreign_keys=ON')
    before=rows(legacy)
    with pytest.raises(ValueError,match='父链'):
        migrate(legacy)
    assert rows(legacy)==before
    assert len(legacy.execute('PRAGMA table_info(account_analysis_history)').fetchall())==len(_COLUMNS)


def test_migration_registration_and_default_legacy_insert_compatibility(tmp_path):
    conn=db.connect(tmp_path/'app.db')
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0]>=18
        insert(conn,10,1)
        assert tuple(conn.execute('SELECT account_id,cycle_id FROM account_analysis_history').fetchone())==('', '')
    finally:
        conn.close()


def test_v17_database_upgrades_nonempty_history_without_changing_ids(tmp_path):
    path=tmp_path/'v17.db'
    old=sqlite3.connect(path,isolation_level=None)
    old.row_factory=sqlite3.Row
    old.executescript(db.SCHEMA)
    try:
        for version, migration in enumerate(db.MIGRATIONS[:17],start=1):
            with db.transaction(old):
                migration(old)
                old.execute(f'PRAGMA user_version={version}')
        insert(old,10,1)
        insert(old,20,1,1,10)
        before=rows(old)
    finally:
        old.close()
    upgraded=db.connect(path)
    try:
        assert rows(upgraded)==before
        assert upgraded.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert upgraded.execute('PRAGMA user_version').fetchone()[0]>=18
    finally:
        upgraded.close()
