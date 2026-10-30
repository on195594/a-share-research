"""Schema migrations and derived writes against captured production report records."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from a_share_research import db
from support.continuation import RECORDS, load
from support.reports import MATERIAL


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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM runs WHERE replay_of IS NOT json_extract(identity, '$.replay_of')").fetchone()[0] == 0
    assert conn.execute(f"SELECT count(*) FROM reports r WHERE coverage IS NOT {db.COVERAGE}").fetchone()[0] == 0


@pytest.mark.parametrize("columns", ["missing", "partial", "present"])
def test_migrations_preserve_old_values_and_reconnect_is_idempotent(tmp_path, columns):
    path = tmp_path / "app.db"
    legacy_database(path, columns)
    with sqlite3.connect(path) as old:
        old.row_factory = sqlite3.Row
        before = {table: [dict(row) for row in old.execute(f"SELECT * FROM {table} ORDER BY id")]
                  for table in ("runs", "reports")}
    conn = db.connect(path)
    assert_derived(conn)
    for table, rows in before.items():
        for row in rows:
            actual = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row["id"],)).fetchone()
            assert {key: actual[key] for key in row} == row
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
    run = db.create_run(conn, report["code"])
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
