"""Schema migrations and derived writes against captured production report records."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from a_share_research import api_keys, db, scans
from support.continuation import RECORDS, load
from support.reports import MATERIAL
from support.account_analysis import insert as insert_analysis
from support.backup import database_rows


def legacy_database(path, columns="present"):
    schema = db.SCHEMA
    if columns != "present":
        schema = schema.replace(
            ",\n    identity TEXT\n", "",
        ).replace(
            ",\n    identity TEXT,\n    material TEXT,\n    request TEXT",
            ",\n    identity TEXT" if columns == "partial" else "",
        ).replace(
            ",\n    body_sha256 TEXT  -- sha256 of body as written; NULL for reports saved before it was recorded", "",
        )
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    for record in RECORDS:
        run, report = record["run"], record["report"]
        for table, values in (
            ("runs", run | {"code": report["code"], "created_at": report["created_at"]}),
            ("reports", report),
        ):
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            values = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v
                      for k, v in values.items() if k in existing}
            conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                         tuple(values.values()))
    conn.close()


def assert_derived(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    for index, table, column in (("calls_by_run", "calls", "run_id"),
                                 ("runs_by_status", "runs", "status"),
                                 ("sessions_by_expires", "sessions", "expires_at")):
        assert index in {row["name"] for row in conn.execute(f"PRAGMA index_list({table})")}
        assert [row["name"] for row in conn.execute(f"PRAGMA index_info({index})")] == [column]
    assert {"prompt_base", "prompt_block"} <= {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
    assert conn.execute("SELECT count(*) FROM runs WHERE replay_of IS NOT json_extract(identity, '$.replay_of')").fetchone()[0] == 0
    assert conn.execute(f"SELECT count(*) FROM reports r WHERE coverage IS NOT {db.COVERAGE}").fetchone()[0] == 0


@pytest.mark.parametrize("columns", ["missing", "partial", "present", "v2_rollback"])
def test_migrations_preserve_old_values_and_reconnect_is_idempotent(tmp_path, columns):
    path = tmp_path / "app.db"
    legacy_database(path, "present" if columns == "v2_rollback" else columns)
    if columns == "v2_rollback":
        with closing(sqlite3.connect(path, isolation_level=None)) as previous:
            db._migrate_v2(previous)
            previous.execute("PRAGMA user_version = 2")
            previous.execute("UPDATE runs SET replay_of = NULL")
            previous.execute("UPDATE reports SET coverage = NULL")
    with sqlite3.connect(path) as old:
        old.row_factory = sqlite3.Row
        before = {table: [dict(row) for row in old.execute(f"SELECT * FROM {table} ORDER BY id")]
                  for table in ("runs", "reports")}
    conn = db.connect(path)
    assert_derived(conn)
    for table, rows in before.items():
        for row in rows:
            actual = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row["id"],)).fetchone()
            assert {key: actual[key] for key in row if key not in {"replay_of", "coverage"}} == {
                key: value for key, value in row.items() if key not in {"replay_of", "coverage"}
            }
    assert conn.execute("SELECT coverage FROM reports WHERE id = 5").fetchone()[0] == "2026-06-30"
    for index, expected in (("reports_by_code_coverage", ["code", "coverage"]), ("runs_by_replay_of", ["replay_of"])):
        assert [row["name"] for row in conn.execute(f"PRAGMA index_info({index})")] == expected
    after = list(conn.iterdump())
    conn.close()
    conn = db.connect(path)
    assert_derived(conn)
    assert conn.total_changes == 0
    assert list(conn.iterdump()) == after
    conn.close()


def test_failed_migration_rolls_back_columns_backfill_and_version(tmp_path):
    path = tmp_path / "app.db"
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TRIGGER fail_backfill BEFORE UPDATE ON reports"
                     " BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected migration failure"):
        db.connect(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "replay_of" not in {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        assert "coverage" not in {row[1] for row in conn.execute("PRAGMA table_info(reports)")}
        conn.execute("DROP TRIGGER fail_backfill")
    conn = db.connect(path)
    assert_derived(conn)
    conn.close()


def test_concurrent_connections_migrate_once(tmp_path):
    path = tmp_path / "app.db"
    legacy_database(path)

    def connect():
        conn = db.connect(path)
        try:
            assert_derived(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: connect(), range(4)))


def test_in_memory_connections_each_start_with_a_fresh_schema(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with closing(db.connect(Path(":memory:"))) as first, closing(db.connect(Path(":memory:"))) as second:
        db.create_run(first, "603606")
        assert second.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert not (tmp_path / ":memory:").exists()


def test_identity_updates_and_material_writes_keep_replay_in_step(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    replay = next(record for record in RECORDS if record["report"]["id"] == 143)["run"]
    normal = RECORDS[0]["run"]
    for identity in (replay["identity"], normal["identity"], None, replay["identity"]):
        db.update_run(conn, replay["id"], identity=json.dumps(identity) if identity is not None else None)
        row = conn.execute("SELECT identity, replay_of FROM runs WHERE id = ?", (replay["id"],)).fetchone()
        assert row["replay_of"] == (identity or {}).get("replay_of")
    db.save_material(conn, replay["id"], MATERIAL.snapshot())
    row = conn.execute("SELECT identity, replay_of FROM runs WHERE id = ?", (replay["id"],)).fetchone()
    assert row["replay_of"] == json.loads(row["identity"])["replay_of"] == 7
    assert json.loads(row["identity"])["material_sha256"] == MATERIAL.snapshot()["sha256"]
    conn.close()


@pytest.mark.parametrize("report_id", [5, 37, 143])
def test_save_report_writes_coverage_with_the_backfill_expression(tmp_path, report_id):
    conn = db.connect(tmp_path / "app.db")
    report = next(record["report"] for record in RECORDS if record["report"]["id"] == report_id)
    changes = conn.total_changes
    run = db.create_run(conn, report["code"])
    assert conn.total_changes - changes == 1  # No derived UPDATE for a queued run without identity.
    db.save_report(conn, run, report["code"], report["name"], report["period"], report["passed"],
                   report["verdict"], report["body"])
    assert_derived(conn)
    assert conn.execute("SELECT coverage FROM reports").fetchone()[0] == "2026-06-30"
    conn.close()


def test_save_report_rolls_back_insert_if_coverage_update_fails(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    report = RECORDS[0]["report"]
    run = db.create_run(conn, report["code"])
    conn.execute("CREATE TRIGGER fail_coverage BEFORE UPDATE OF coverage ON reports"
                 " BEGIN SELECT RAISE(ABORT, 'injected coverage failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected coverage failure"):
        db.save_report(conn, run, report["code"], report["name"], report["period"], report["passed"],
                       report["verdict"], report["body"])
    assert conn.execute("SELECT count(*) FROM reports").fetchone()[0] == 0
    conn.close()


def test_future_additive_version_remains_usable(tmp_path):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    future = len(db.MIGRATIONS) + 1
    conn.execute(f"PRAGMA user_version = {future}")
    conn.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == future
    assert conn.total_changes == 0
    conn.close()


def test_scan_request_migration_preserves_v20_data_and_is_reentrant(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    migrations = db.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', migrations[:20])
        conn = db.connect(path)
        load(conn)
        conn.execute("INSERT INTO scans(status,requested_at,summary) VALUES ('done',?,'{}')", (db.now(),))
        before = list(conn.iterdump())
        conn.close()
    monkeypatch.setattr(db, 'MIGRATIONS', migrations[:21])
    conn = db.connect(path)
    after = list(conn.iterdump())
    # The additive table/index must not rewrite any captured research or scan row.
    assert all(line in after for line in before)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute('SELECT count(*) FROM scan_requests').fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        conn.execute('INSERT INTO scan_requests VALUES (?,?,?,?)', ('key:'+'a'*64, 'structural', 999, db.now()))
    conn.close()
    conn = db.connect(path)
    assert list(conn.iterdump()) == after and conn.total_changes == 0
    conn.close()


def test_scan_request_migration_failure_preserves_version_and_no_partial_table(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:20])
        conn = db.connect(path)
        load(conn)
        # Structural naming collision makes the index statement fail after CREATE TABLE.
        conn.execute('CREATE TABLE scan_requests_by_scan(unused TEXT)')
        before = list(conn.iterdump())
        conn.close()
    with pytest.raises(sqlite3.OperationalError, match='already a table'):
        db.connect(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 20
        assert list(conn.iterdump()) == before


def test_legacy_sql_inserts_and_updates_maintain_derived_columns(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    assert_derived(conn)
    report = RECORDS[0]["report"]
    replay = next(r["run"] for r in RECORDS if r["report"]["id"] == 143)
    conn.execute("UPDATE runs SET identity = ? WHERE id = ?",
                 (json.dumps(replay["identity"]), RECORDS[0]["run"]["id"]))
    conn.execute("UPDATE reports SET body = ?, period = ? WHERE id = 143",
                 (json.dumps(report["body"]), report["period"]))
    assert_derived(conn)
    conn.execute("UPDATE reports SET period = ? WHERE id = 143", (report["body"]["period"],))
    assert_derived(conn)
    conn.execute("UPDATE runs SET identity = NULL WHERE id = ?", (replay["id"],))
    assert_derived(conn)
    conn.close()


def test_disclosure_checks_migrations_preserve_evidence_and_rollback_compatibility(tmp_path, monkeypatch):
    from pathlib import Path
    from a_share_research import disclosure_checks

    path = tmp_path / 'v22.db'
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:22])
        conn = db.connect(path)
        load(conn)
        before = list(conn.iterdump())
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 22
        conn.close()
    with monkeypatch.context() as v23:
        v23.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:23])
        conn = db.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 23
    assert all(line in list(conn.iterdump()) for line in before)
    assert conn.execute('PRAGMA foreign_key_list(disclosure_checks)').fetchall() == []
    assert [r['name'] for r in conn.execute('PRAGMA index_info(disclosure_checks_by_company_report)')] == [
        'company_key', 'report_id']
    capture = json.loads((Path(__file__).with_name('data') / 'disclosure-checks.json').read_text())
    report = next(r for r in capture['reports'] if r['report_id'] == 156)
    # Explicit period-shift / cross-company variant tests storage, not investment performance.
    item = disclosure_checks.items(report)[0] | {'period': '2025-12-31'}
    observation = disclosure_checks.normalize_forecast(capture['tushare']['forecast']['603960.SH'], '603960')[0]
    check = disclosure_checks.judge(item, observation)
    old_report = RECORDS[0]['report']
    values = ('603960', old_report['id'], check['ref'], check['kind'], check['metric'], check['period'],
              check['source'], check['identity'], check['announced'], check['outcome'], check['direction'],
              check['reason'], report['created_at'], json.dumps(check['observed']))
    sql = ('INSERT INTO disclosure_checks(code,report_id,item_ref,kind,metric,period,source,source_identity,'
           'announced,outcome,direction,reason,checked_at,observed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
           ' ON CONFLICT DO NOTHING')
    assert conn.execute(sql, values).rowcount == 1
    assert conn.execute(sql, values).rowcount == 0
    after = list(conn.iterdump())
    with db.transaction(conn):
        db._migrate_v23(conn)
        db._migrate_v23(conn)
    assert list(conn.iterdump()) == after
    legacy = dict(conn.execute('SELECT * FROM disclosure_checks').fetchone())
    conn.close()
    conn = db.connect(path)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == len(db.MIGRATIONS)
    upgraded = dict(conn.execute('SELECT * FROM disclosure_checks').fetchone())
    assert upgraded == legacy | {'version': 1, 'evidence': None}
    # Captured-bound, version-only variant: reentry must preserve multi-version evidence.
    revision = {k: v for k, v in upgraded.items() if k not in ('id', 'company_key')}
    revision.update(version=2, evidence=json.dumps({'value': observation['values'][item['metric']]}, default=str))
    conn.execute(f"INSERT INTO disclosure_checks ({','.join(revision)}) VALUES ({','.join('?' for _ in revision)})",
                 tuple(revision.values()))
    after = list(conn.iterdump())
    with db.transaction(conn):
        db._migrate_v24(conn)
        db._migrate_v24(conn)
    assert list(conn.iterdump()) == after
    conn.close()
    conn = db.connect(path)
    assert conn.total_changes == 0 and list(conn.iterdump()) == after
    for statement in ('UPDATE disclosure_checks SET outcome=outcome', 'DELETE FROM disclosure_checks'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute(statement)
    # Legacy SQL remains compatible; this is not a semantic rollback-reader check.
    # Older A4b readers ignore version and must not read multi-version history.
    conn.execute('DELETE FROM reports WHERE id=?', (old_report['id'],))
    conn.execute('DELETE FROM runs WHERE id=?', (old_report['run_id'],))
    assert conn.execute('SELECT count(*) FROM disclosure_checks').fetchone()[0] == 2
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()


@pytest.fixture
def v24(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:24])
        with closing(db.connect(path)) as conn:
            load(conn)
            token = api_keys.create(conn, 'legacy assistant', role='assistant')
            key_id, token_hash = conn.execute('SELECT id,token_hash FROM api_keys').fetchone()
            requester = 'key:' + token_hash
            scan_id = scans.submit_request(conn, requester, 'pending')
            api_keys.create(conn, 'legacy owner', role='owner')
            owner_id = conn.execute("SELECT id FROM api_keys WHERE role='owner'").fetchone()[0]
            # Structural audit references in their original columns, alongside captured reports.
            insert_analysis(conn, 10, 1, actor_key_id=key_id, company_key=RECORDS[0]['report']['code'])
            conn.execute("INSERT INTO policy_versions VALUES ('audit','v0',?,?,'{}')", (db.now(), owner_id))
            conn.execute("""INSERT INTO policy_evaluations
                (identity,created_at,created_by,request_id,input_hash,result_hash,input,result)
                VALUES ('audit',?,?,'receipt','structural','structural','{}','{}')""", (db.now(), key_id))
            api_keys.create(conn, 'previously revoked')
            conn.execute("DELETE FROM api_keys WHERE name='previously revoked'")
    return path, token, key_id, requester, scan_id


def test_grant_migration_preserves_old_columns_audit_and_pending_request(v24, monkeypatch):
    path, token, key_id, requester, scan_id = v24
    monkeypatch.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:25])
    before = database_rows(path)
    assert before.pop('__user_version__') == 24
    keys = before.pop('api_keys')
    with closing(db.connect(path)) as conn:
        after = database_rows(path)
        assert after['__user_version__'] == 25
        assert after['api_keys'] == [(*row, 'pat', None, None) for row in keys]
        assert {table: after[table] for table in before} == before
        assert api_keys.authenticate(conn, token) == api_keys.Principal(key_id, 'assistant')
        assert scans.submit_request(conn, requester, 'pending') == scan_id
        with pytest.raises(sqlite3.IntegrityError, match='rotate key'):
            conn.execute("UPDATE api_keys SET role='owner' WHERE id=?", (key_id,))
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            conn.execute("INSERT INTO api_keys(name,token_hash,created_at) SELECT name,token_hash,created_at FROM api_keys")
        assert api_keys.revoke(conn, key_id)
        for table, ref in (('account_analysis_history', 'actor_key_id'), ('policy_evaluations', 'created_by')):
            row = conn.execute(f'SELECT k.id,k.revoked_at FROM {table} h JOIN api_keys k ON k.id=h.{ref}').fetchone()
            assert row['id'] == key_id and row['revoked_at']
        replacement = api_keys.create(conn, 'replacement')
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='api_keys'").fetchone()[0]
        assert api_keys.authenticate(conn, replacement).key_id == sequence == 4
        migrated = list(conn.iterdump())
        with db.transaction(conn):
            db._migrate_v25(conn)
        assert list(conn.iterdump()) == migrated
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with closing(db.connect(path)) as reopened:
        assert list(reopened.iterdump()) == migrated and reopened.total_changes == 0


def test_grant_copy_hash_mismatch_rolls_back_the_entire_migration(v24, monkeypatch):
    path = v24[0]
    with closing(sqlite3.connect(path)) as conn:
        before = list(conn.iterdump())
    original = api_keys._column_hashes

    def corrupt_copy(conn):
        if 'kind' in {row[1] for row in conn.execute('PRAGMA table_info(api_keys)')}:
            conn.execute("UPDATE api_keys SET name=name || ' corrupt copy'")
        return original(conn)

    monkeypatch.setattr(api_keys, '_column_hashes', corrupt_copy)
    with pytest.raises(ValueError, match='旧列哈希不一致'):
        db.connect(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 24
        assert list(conn.iterdump()) == before
        with pytest.raises(sqlite3.IntegrityError, match='rotate key'):
            conn.execute("UPDATE api_keys SET role='owner'")


def test_scheduled_runs_migration_preserves_v25_and_is_reentrant(v24, monkeypatch):
    path, _, key_id, _, scan_id = v24
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:25])
        with closing(db.connect(path)) as conn:
            before = list(conn.iterdump())
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 25

    with closing(db.connect(path)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 26
        assert [line for line in conn.iterdump() if 'scheduled_runs' not in line] == before
        columns = [r['name'] for r in conn.execute('PRAGMA table_xinfo(scheduled_runs)')]
        assert set(columns[-4:]) == {'state', 'text', 'result', 'error'}
        assert {r['from']: r['table'] for r in conn.execute('PRAGMA foreign_key_list(scheduled_runs)')} == {
            'principal_key_id': 'api_keys', 'scan_id': 'scans',
            'activation_id': 'policy_activations', 'evaluation_id': 'policy_evaluations'}
        assert conn.execute("SELECT partial FROM pragma_index_list('scheduled_runs') WHERE name='scheduled_runs_acked'"
                            ).fetchone()[0] == 1

        insert = 'INSERT INTO scheduled_runs(principal_key_id,job,slot,scan_id,status,created_at) VALUES (?,?,?,?,?,?)'
        entry = (key_id, 'advisor_scan', '2026-11-23', scan_id, 'running', db.now())
        conn.execute(insert, entry)
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            conn.execute(insert, entry)
        with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
            conn.execute("UPDATE scheduled_runs SET status='unknown'")
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
            conn.execute("UPDATE scheduled_runs SET scan_id=-1")
        after = list(conn.iterdump())
        with db.transaction(conn):
            db._migrate_v26(conn)
        assert list(conn.iterdump()) == after
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with closing(db.connect(path)) as reopened:
        assert list(reopened.iterdump()) == after and reopened.total_changes == 0


def test_scheduled_runs_partial_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    with monkeypatch.context() as old:
        old.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:25])
        with closing(db.connect(path)) as conn:
            # Make CREATE INDEX fail after the table has been created.
            conn.execute('CREATE TABLE scheduled_runs_acked(unused INTEGER)')
            before = list(conn.iterdump())
    with pytest.raises(sqlite3.OperationalError, match='already a table'):
        db.connect(path)
    with closing(sqlite3.connect(path)) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 25


def test_two_connections_migrate_empty_database_to_v26_once(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    start = Barrier(2)
    calls = []

    def migrate(conn):
        calls.append(conn.in_transaction)
        db._migrate_v26(conn)

    monkeypatch.setattr(db, 'MIGRATIONS', (*db.MIGRATIONS[:25], migrate))

    def connect():
        start.wait()
        with closing(db.connect(path)) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 26
            assert not conn.execute('SELECT * FROM scheduled_runs').fetchall()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(connect) for _ in range(2)]
        for task in tasks:
            task.result()
    assert calls == [True]


def test_wal_startup_retries_a_concurrent_lock_upgrade(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    with closing(sqlite3.connect(path)) as other:
        # A rollback-journal writer stops the other opener from switching to WAL.
        other.execute('BEGIN IMMEDIATE')

        def finish_writer(seconds):
            assert other.in_transaction
            other.commit()

        monkeypatch.setattr(db, 'sleep', finish_writer)
        with closing(db.connect(path)) as conn:
            assert not other.in_transaction
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert conn.execute('PRAGMA synchronous').fetchone()[0] == 2
