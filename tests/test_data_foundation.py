"""Invariants of the v9 data foundation: rebuilt tables, generated identity columns and their checks."""

import ast
import importlib
import json
import re
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

import a_share_research
from a_share_research import api_keys, companies, db, integrity, reading, research, research_tools, scans, screening, service, stock_codes
from support.continuation import BODY, load
from support.valuation import BODY as VALUATION_BODY, RECORD


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


COMPANY_TABLES = ("runs", "reports", "prices", "scan_events", "subscriptions")
CANONICAL_CODES = (("603606", "603606"), ("603606.SH", "603606"),
                   ("000333.SZ", "000333"), ("920001.BJ", "920001"))
INVALID_CODES = ("603606.SS", "60360", "６０３６０６", "603606.sh", "603606 ", "", "6036060", "60360x")


def test_rebuilt_tables_keep_payload_last_and_ids_are_never_reused(conn):
    for table in COMPANY_TABLES:
        assert "company_key" in {row["name"] for row in conn.execute(f"PRAGMA table_xinfo({table})")}
    for table, payload in (("runs", "material"), ("reports", "body"), ("calls", "prompt")):
        assert list(conn.execute(f"PRAGMA table_xinfo({table})"))[-1]["name"] == payload
    report = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
    first = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    conn.execute("DELETE FROM reports WHERE id=?", (report['id'],))
    # Delete the latest run too, after removing only its dependent captured report.
    conn.execute("DELETE FROM reports WHERE run_id=?", (first,))
    conn.execute("DELETE FROM runs WHERE id=?", (first,))
    second = db.create_run(conn, report['code'])
    db.save_report(conn, second, report['code'], report['name'], report['period'], report['passed'],
                   report['verdict'], json.loads(report['body']))
    assert second > first
    assert conn.execute("SELECT MAX(id) FROM reports").fetchone()[0] > report['id']


def insert_company(conn, table, code):
    """Only code/identity varies; payloads come from captured company evidence."""
    if table == 'runs':
        conn.execute('INSERT INTO runs(code,created_at) VALUES (?,?)', (code, RECORD['created_at']))
    elif table == 'subscriptions':
        conn.execute('INSERT INTO subscriptions(code,added_at) VALUES (?,?)', (code, RECORD['created_at']))
    elif table == 'scan_events':
        document = VALUATION_BODY['document']
        scan = scans.submit(conn)
        conn.execute(
            'INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action) '
            "VALUES (?,?,?,?,?,?,'periodic',2,?,'deferred')",
            (scan, code, document['sha256'], document['title'], document['announced'], document['url'],
             document['retrieved_at']))
    else:
        run = db.create_run(conn, RECORD['code'])
        if table == 'reports':
            db.save_report(conn, run, code, RECORD['name'], RECORD['period'], RECORD['passed'],
                           RECORD['verdict'], VALUATION_BODY)
        else:
            # Price payload is opaque to storage; use captured close evidence, not invented prices.
            close = next(row for row in VALUATION_BODY['table'] if row['id'] == 'M.close')
            conn.execute('INSERT INTO prices(run_id,code,name,as_of,expires,body,created_at) VALUES (?,?,?,?,?,?,?)',
                         (run, code, RECORD['name'], RECORD['created_at'], RECORD['created_at'],
                          json.dumps(close), RECORD['created_at']))


@pytest.mark.parametrize('table', COMPANY_TABLES)
@pytest.mark.parametrize('code', INVALID_CODES)
def test_storage_rejects_a_code_without_a_company(tmp_path, table, code):
    conn = db.connect(tmp_path / 'app.db')
    with pytest.raises(sqlite3.IntegrityError, match='company_key'):
        insert_company(conn, table, code)


@pytest.mark.parametrize('table', COMPANY_TABLES)
@pytest.mark.parametrize('code,expected', CANONICAL_CODES)
def test_company_key_and_normalization_agree_on_canonical_forms(tmp_path, table, code, expected):
    conn = db.connect(tmp_path / 'app.db')
    insert_company(conn, table, code)
    assert conn.execute(f'SELECT company_key FROM {table} WHERE code=?', (code,)).fetchone()[0] == expected
    assert stock_codes.normalize_key(code) == expected


@pytest.mark.parametrize('code', [code for code in INVALID_CODES if code != '603606 '])
def test_normalization_rejects_the_same_noncanonical_codes(code):
    with pytest.raises(ValueError):
        stock_codes.normalize_key(code)


@pytest.mark.parametrize('table', COMPANY_TABLES)
@pytest.mark.parametrize('code,expected', CANONICAL_CODES)
@pytest.mark.parametrize('padding', [(' ', ''), ('', ' '), ('\t', '\n')])
def test_whitespace_tolerance_is_only_an_input_boundary(tmp_path, table, code, expected, padding):
    conn = db.connect(tmp_path / 'app.db')
    value = padding[0] + code + padding[1]
    assert stock_codes.normalize_key(value) == expected
    with pytest.raises(sqlite3.IntegrityError, match='company_key'):
        insert_company(conn, table, value)


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


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'foundation.db')
    load(connection)
    yield connection
    connection.close()


@pytest.fixture
def scan_event(conn):
    document = VALUATION_BODY['document']
    scan = scans.submit(conn)
    conn.execute(
        'INSERT INTO scan_events(scan_id,code,doc_key,title,announced,url,kind,severity,detected_at,action) '
        "VALUES (?,?,?,?,?,?,'periodic',2,?,'deferred')",
        (scan, stock_codes.normalize_key(RECORD['code']), document['sha256'], document['title'],
         document['announced'], document['url'], document['retrieved_at']))
    return scan


@pytest.mark.parametrize('query', [
    lambda conn, code: reading.latest_reports(conn, codes=[code]),
    lambda conn, code: reading.latest_reports(conn, code_prefix=code),
    lambda conn, code: reading.api_reports(conn, code),
    lambda conn, code: reading.stock_reports(conn, code),
    lambda conn, code: reading.stock_reports(conn, code, summaries=True),
    lambda conn, code: reading.followups(conn, code, 37),
])
def test_read_queries_normalize_company_inputs(conn, query):
    bare = query(conn, '603606')
    assert bare
    assert query(conn, '603606.SH') == bare
    assert query(conn, '603606.SS') == []


def test_content_search_normalizes_company_input(conn):
    text = BODY[39]['risks'][0]['text'][:12]
    bare = reading.search_items(conn, text, code='603606')
    assert bare[0]
    assert reading.search_items(conn, text, code='603606.SH') == bare


@pytest.mark.parametrize('query', [
    lambda conn, code: research._research_context(conn, code, 37),
    lambda conn, code: research._prior_report(conn, code, 999),
    lambda conn, code: screening.reusable_report(conn, code, '2026-10-20'),
])
def test_research_queries_normalize_both_code_forms(conn, query):
    bare = query(conn, '603606')
    assert bare
    assert query(conn, '603606.SH') == bare


def test_scan_events_normalize_both_code_forms(conn, scan_event):
    bare = scans.events(conn, code='603606')
    assert len(bare) == 1
    assert scans.events(conn, code='603606.SH') == bare


def test_latest_close_normalizes_both_code_forms(conn):
    day, close = next(iter(next(row for row in VALUATION_BODY['table'] if row['id'] == 'M.close')['values'].items()))
    conn.execute('INSERT INTO subscriptions(code,added_at,last_close,last_close_date) VALUES (?,?,?,?)',
                 (RECORD['code'], RECORD['created_at'], close, day))
    bare = scans.latest_close(conn, '603606')
    assert bare['last_close'] == float(close)
    assert scans.latest_close(conn, '603606.SH') == bare



def test_business_sql_uses_shared_company_and_sample_definitions():
    root = Path(db.__file__).parent
    # Section 3.5 of the data foundation plan: selection predicates kept as frozen.
    retained = {'validation.py': 3, 'zones.py': 1, 'reading.py': 2}
    found = dict.fromkeys(retained, 0)
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text())
        # DDL defines the identity; only these exact assignments may use substr(code).
        excluded = set()
        samples = set()
        if path.name == 'db.py':
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    names = {target.id for target in node.targets if isinstance(target, ast.Name)}
                    if names & {'COMPANY_KEY', 'BATCH_ID'}:
                        excluded.update(id(child) for child in ast.walk(node))
                    if names & {'RESEARCH_ORIGINAL', 'FORMAL_REPORT'}:
                        samples.update(id(child) for child in ast.walk(node))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            location = f'{path.name}:{node.lineno}'
            if id(node) not in excluded:
                assert not re.search(r'\bsubstr\s*\(\s*(?:[a-z_]\w*\.)?code\b', text, re.I), location
            assert not re.search(r"\bLIKE\s*'batch-%'", text, re.I), location
            if id(node) not in samples:
                hits = len(re.findall(r'\breplay_of\s+IS\s+NULL\b', text, re.I))
                assert not hits or path.name in retained, location
                found[path.name] = found.get(path.name, 0) + hits
    assert {name: found[name] for name in retained} == retained


@pytest.fixture
def api_boundary(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA', tmp_path)
    monkeypatch.setenv('DEV_LOGIN', '1')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8627')
    monkeypatch.setenv('SESSION_SECRET', 'foundation-test-session-' * 2)
    monkeypatch.setattr(a_share_research, 'web', None, raising=False)
    monkeypatch.delitem(sys.modules, 'a_share_research.web', raising=False)
    web = importlib.import_module('a_share_research.web')
    monkeypatch.setattr(web, 'conn', conn)
    token = api_keys.create(conn, 'foundation-boundary-test')
    try:
        request = web.Request({'type': 'http', 'headers': [(b'authorization', f'Bearer {token}'.encode())]})
        yield web, request
    finally:
        sys.modules.pop('a_share_research.web', None)


@pytest.mark.parametrize('code', ['603606.SH', '60360', '603606 ', '６０３６０６'])
@pytest.mark.parametrize('endpoint', ['reports', 'search', 'card'])
def test_http_company_boundaries_keep_status_and_error_text(api_boundary, code, endpoint):
    web, request = api_boundary
    with pytest.raises(service.RequestError) as error:
        if endpoint == 'search':
            web.api_search(request, BODY[39]['risks'][0]['text'][:12], code=code, page=1)
        elif endpoint == 'reports':
            web.api_reports(request, code)
        else:
            web.api_company_card(request, code)
    assert error.value.status == 422
    assert str(error.value) == '公司代码必须为六位数字'


def test_http_full_width_submission_is_rejected_by_the_request_model(api_boundary):
    web, _ = api_boundary
    with pytest.raises(ValidationError):
        web.ResearchRequest(code='６０３６０６', request_id='full-width')


def test_full_width_input_gets_each_boundary_error():
    assert not stock_codes.KEY_RE.fullmatch('６０３６０６')
    assert not stock_codes.CODE_RE.fullmatch('６０３６０６.SH')
    with pytest.raises(ValueError, match='公司代码必须为六位数字'):
        research_tools.ResearchTools.code('６０３６０６')


@pytest.mark.parametrize('code', ['６０３６０６', '603606.SS', '60360'])
def test_invalid_company_input_keeps_source_error_without_fetching(monkeypatch, code):
    def unexpected_fetch():
        pytest.fail('Invalid company input must not fetch the remote catalog')

    monkeypatch.setattr(companies, 'stock_list', unexpected_fetch)
    with pytest.raises(companies.SourceError) as error:
        companies.company(code)
    assert str(error.value) == f'{code} is not a listed A share on cninfo'


def test_external_identifier_outside_the_stored_form_does_not_break_a_source():
    # T00018.SH is in the live index_member_all response (is_new=Y, 2026-11-07).
    with pytest.raises(companies.SourceError, match='估值行情返回其他公司'):
        scans.normalize_close({'ts_code': 'T00018.SH', 'close': 1.0, 'trade_date': '2026-11-06'}, '603606',
                              '2026-11-07T10:00:00+08:00')
