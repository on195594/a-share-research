"""Invariants of the v9 data foundation: rebuilt tables, generated identity columns and their checks."""

import json
import sqlite3
import threading

import pytest

from a_share_research import db, integrity, scans, service
from support.continuation import load


def v8_database(path, *, rows=True):
    """A database as production's v8 is: WAL, holding the real captured records."""
    old = sqlite3.connect(path, isolation_level=None)
    old.row_factory = sqlite3.Row
    old.execute("PRAGMA journal_mode=WAL")
    old.executescript(db.SCHEMA)
    for migration in db.MIGRATIONS[:8]:
        migration(old)
    old.execute("PRAGMA user_version = 8")
    if rows:
        load(old)
    old.close()
    return path


def schema(conn):
    return [tuple(row) for row in conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]


def stored(conn, table):
    names = sorted(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
    return names, [tuple(row) for row in conn.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY id")]


def test_rebuilt_tables_keep_payload_last_and_ids_are_never_reused(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    for table, payload, _ in db.REBUILT:
        assert list(conn.execute(f"PRAGMA table_xinfo({table})"))[-1]["name"] == payload
    first = db.create_run(conn, "603606.SH")
    db.save_report(conn, first, "603606.SH", "东方电缆", "2026", True, None, {"table": []})
    report = conn.execute("SELECT id FROM reports").fetchone()[0]
    conn.execute("DELETE FROM reports")
    conn.execute("DELETE FROM runs")
    second = db.create_run(conn, "603606.SH")
    db.save_report(conn, second, "603606.SH", "东方电缆", "2026", True, None, {"table": []})
    assert second > first and conn.execute("SELECT id FROM reports").fetchone()[0] > report


@pytest.mark.parametrize("insert", ["INSERT INTO runs(code, created_at) VALUES (?, 'x')",
                                    "INSERT INTO subscriptions(code, added_at) VALUES (?, 'x')"])
@pytest.mark.parametrize("code", ["603606.SS", "60360", "６０３６０６", "603606.sh", "603606 ", ""])
def test_storage_rejects_a_code_without_a_company(tmp_path, insert, code):
    conn = db.connect(tmp_path / "app.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (code,))


@pytest.mark.parametrize("code", ["603606", "603606.SH", "000333.SZ", "920001.BJ"])
def test_company_key_is_the_six_digits_of_every_stored_form(tmp_path, code):
    conn = db.connect(tmp_path / "app.db")
    run = db.create_run(conn, code)
    assert conn.execute("SELECT company_key FROM runs WHERE id = ?", (run,)).fetchone()[0] == code[:6]


@pytest.mark.parametrize("request_id, code, expected", [
    ("batch-1-603606", "603606", 1),
    ("batch-1-603606", "603606.SH", 1),
    ("batch-1-603606.SH", "603606", 1),
    ("batch-12-000333.SZ", "000333.SZ", 12),
    # Minimal mismatches the real corpus lacks: an external request id is only length-checked.
    ("batch-1oops-603606.SH", "603606.SH", None),
    ("batch-1-000333.SZ", "603606.SH", None),
    ("batch-01-603606", "603606", None),
    ("batch-1-603606.SS", "603606.SH", None),
    ("batch-1-603606x", "603606", None),
    ("batch-1-", "603606", None),
    ("batch-1", "603606", None),
    ("batch--603606", "603606", None),
    ("batchB-refresh-20261102-603606", "603606", None),
    (None, "603606", None),
])
def test_batch_id_only_accepts_the_exact_request_shape(tmp_path, request_id, code, expected):
    conn = db.connect(tmp_path / "app.db")
    run = db.create_run(conn, code, request_id)
    assert conn.execute("SELECT batch_id FROM runs WHERE id = ?", (run,)).fetchone()[0] == expected


def test_full_width_code_gets_a_request_error_not_a_database_error(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    with pytest.raises(service.RequestError):
        service.submit(conn, "６０３６０６", "full-width")


def test_migration_keeps_rows_triggers_and_sequence_and_reruns_as_a_noop(tmp_path):
    path = v8_database(tmp_path / "v8.db")
    old = sqlite3.connect(path)
    tables = ("runs", "reports", "calls", "prices", "scan_events", "subscriptions")
    before = {table: stored(old, table) for table in ("runs", "reports", "calls", "prices")}
    triggers = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    old.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert all("company_key" in {row["name"] for row in conn.execute(f"PRAGMA table_xinfo({table})")}
               for table in tables if table != "calls")
    for table, (names, rows) in before.items():
        assert [tuple(row) for row in conn.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY id")] == rows
    assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")} == triggers
    sequence = dict(tuple(row) for row in conn.execute("SELECT name, seq FROM sqlite_sequence"))
    assert sequence["runs"] == before["runs"][1][-1][before["runs"][0].index("id")]
    assert sequence["reports"] == before["reports"][1][-1][before["reports"][0].index("id")]

    final = schema(conn)
    conn.execute("PRAGMA user_version = 8")
    conn.close()
    again = db.connect(path)
    assert schema(again) == final and again.execute("PRAGMA user_version").fetchone()[0] == 9


def test_failure_after_a_table_is_dropped_rolls_back_everything(tmp_path, monkeypatch):
    path = v8_database(tmp_path / "v8.db")
    old = sqlite3.connect(path)
    before, rows = schema(old), stored(old, "runs")
    old.close()

    def fail_at_rename(conn):
        # The rename runs right after DROP TABLE, the worst moment for the migration to stop.
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE
                            else sqlite3.SQLITE_OK)
        db._migrate_v9(conn)

    monkeypatch.setattr(db, "MIGRATIONS", (*db.MIGRATIONS[:8], fail_at_rename))
    with pytest.raises(sqlite3.DatabaseError):
        db.connect(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 8
    assert schema(check) == before and stored(check, "runs") == rows


def test_two_starts_at_once_migrate_once_and_both_open(tmp_path):
    path = v8_database(tmp_path / "v8.db")
    errors, gate = [], threading.Barrier(2)

    def start():
        try:
            gate.wait()
            conn = db.connect(path)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
            conn.close()
        except BaseException as exc:  # surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_integrity_reports_relations_and_skips_them_on_a_v8_backup(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    parent_run = db.create_run(conn, "603606.SH")
    db.save_report(conn, parent_run, "603606.SH", "东方电缆", "2026", True, None, {"table": []})
    parent = conn.execute("SELECT id FROM reports").fetchone()[0]
    other = db.create_run(conn, "000333.SZ")
    conn.execute("UPDATE runs SET request = ? WHERE id = ?", (json.dumps({"parent_report_id": 999}), other))
    orphan = db.create_run(conn, "000333.SZ", "batch-77-000333")

    problems, counts = integrity.check(tmp_path / "app.db", tmp_path / "pdf")
    assert problems == []
    assert counts["dangling_parent_reports"] == [other] and counts["orphan_batch_runs"] == [orphan]

    conn.execute("UPDATE runs SET request = ? WHERE id = ?", (json.dumps({"parent_report_id": parent}), other))
    problems, counts = integrity.check(tmp_path / "app.db", tmp_path / "pdf")
    assert problems == [f"任务 {other}：公司与父报告 {parent} 不一致"] and counts["dangling_parent_reports"] == []

    assert "dangling_parent_reports" not in integrity.check(v8_database(tmp_path / "v8.db"), tmp_path / "pdf")[1]


def test_scan_api_rows_keep_their_published_fields(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    conn.execute("INSERT INTO subscriptions(code, origin, added_at) VALUES ('603606', 'external', 'x')")
    scan = scans.submit(conn)
    conn.execute("INSERT INTO scan_events(scan_id, code, doc_key, title, announced, url, kind, severity, detected_at, action)"
                 " VALUES (?, '603606', 'k', 't', 'a', 'u', 'periodic', 1, 'x', 'deferred')", (scan,))
    assert "company_key" not in scans.subscriptions(conn)[0]
    assert "company_key" not in scans.events(conn)[0]
