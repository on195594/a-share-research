"""Account migrations, the pre-cutover mirror and journal trade/research coverage."""

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from a_share_research import db, gate
from a_share_research.account import ledger, mirror
from a_share_research.account.reconciliation import Opening
from a_share_research.tools.registry import Context, ToolError, invoke
from support.accounts import batch, buy, source_database, submit
from support.delivery import SUCCESS, saved


@pytest.fixture
def source(tmp_path):
    (tmp_path / "old").mkdir()
    return source_database(tmp_path / "old" / "cache.db")


def target_digest(conn):
    return mirror.digest(mirror._read(conn, mirror.target_directory(conn)))


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    yield conn
    conn.close()


def test_mirror_copies_every_listed_column_and_keeps_id_counters(conn, source, tmp_path):
    result = mirror.mirror(conn, source)

    old = sqlite3.connect(source)
    for table, columns in mirror.MANIFEST.items():
        select = f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1, 2"
        assert [tuple(row) for row in conn.execute(select)] == old.execute(select).fetchall(), table
        assert result["rows"][table] == len(old.execute(select).fetchall())
    assert result["rows"]["holdings"] == 2 and result["sequences"]["holdings"] == 3
    assert dict(tuple(row) for row in conn.execute("SELECT code, company_key FROM analysis_results")) == {
        "600519": "600519", "SAP": None}
    # The source once issued id 3; the mirror must not hand it out again.
    assert conn.execute("INSERT INTO holdings (code) VALUES ('601318')").lastrowid == 4
    state = conn.execute("SELECT * FROM account_source").fetchone()
    assert (state["mode"], state["source_sha256"]) == ("mirror", result["source_sha256"])
    copied = tmp_path / "account-snapshot.json"
    assert copied.read_bytes() == source.with_name("account-snapshot.json").read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o600


def test_rerun_replaces_target_changes_with_the_source(conn, source):
    first = mirror.mirror(conn, source)
    conn.execute("INSERT INTO quote_snapshots (code, price, quote_date, quote_time, fetched_at, source,"
                 " verification_sources) VALUES ('000858', 1, '2026-11-11', '15:00:00', 'x', 'shadow', '[]')")
    conn.execute("UPDATE holdings SET shares = 1")
    assert mirror.mirror(conn, source) == first
    assert [row[0] for row in conn.execute("SELECT shares FROM holdings ORDER BY id")] == [100, 300]
    assert conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0] == 1

    old = sqlite3.connect(source)
    old.execute("DELETE FROM holding_events WHERE event_type = 'dividend'")
    old.commit()
    second = mirror.mirror(conn, source)
    assert second["rows"]["holding_events"] == 2 and second["source_sha256"] != first["source_sha256"]
    assert second["sequences"]["holding_events"] == 3

    source.with_name("account-snapshot.json").unlink()
    third = mirror.mirror(conn, source)
    assert third["files"] == {"account-snapshot.json": None} and third["source_sha256"] != second["source_sha256"]
    assert not (mirror.target_directory(conn) / "account-snapshot.json").exists()


def _source_change(statement):
    def change(path):
        old = sqlite3.connect(path)
        old.execute(statement)
        old.commit()
        old.close()
    return change


@pytest.mark.parametrize("change, message", [
    (_source_change("ALTER TABLE holdings ADD COLUMN broker TEXT"), "holdings 的列与迁移清单不一致：多出 ['broker']"),
    (_source_change("ALTER TABLE holding_events RENAME COLUMN report_id TO research_id"), "缺少 ['report_id']"),
    (_source_change("CREATE TABLE cash_flows (id INTEGER PRIMARY KEY)"), "多出 ['cash_flows']"),
    (_source_change("UPDATE analysis_results SET data_date = '2026-10-07' WHERE code = '600519'"), "analysis_results.data_date"),
    (lambda path: path.unlink(), "来源账户库不存在"),
])  # each is a minimal variant of the real schema that the manifest does not describe
def test_source_that_left_the_manifest_is_refused_and_target_is_untouched(conn, source, change, message):
    mirror.mirror(conn, source)
    before = target_digest(conn)
    change(source)
    with pytest.raises(mirror.MirrorError) as error:
        mirror.mirror(conn, source)
    assert message in str(error.value)
    assert target_digest(conn) == before


def test_copy_that_does_not_match_the_source_is_rolled_back(conn, source):
    first = mirror.mirror(conn, source)
    # Minimal tamper variant: the target alters a copied value while the mirror writes it.
    conn.execute("CREATE TRIGGER tamper AFTER INSERT ON holdings BEGIN"
                 " UPDATE holdings SET shares = shares + 1 WHERE id = NEW.id; END")
    with pytest.raises(mirror.MirrorError, match="已回滚"):
        mirror.mirror(conn, source)
    assert target_digest(conn) == first["source_sha256"]
    assert conn.execute("SELECT source_sha256 FROM account_source").fetchone()[0] == first["source_sha256"]


def test_mirror_never_runs_after_the_cutover(conn, source):
    mirror.mirror(conn, source)
    conn.execute("UPDATE account_source SET mode = 'ledger'")
    conn.execute("UPDATE holdings SET shares = 150 WHERE code = '600519'")  # a post-cutover entry
    with pytest.raises(mirror.MirrorError, match="已切换"):
        mirror.mirror(conn, source)
    assert conn.execute("SELECT shares FROM holdings WHERE code = '600519'").fetchone()[0] == 150


def test_mirror_does_not_write_account_data_into_a_shared_directory(conn, source):
    directory = mirror.target_directory(conn)
    directory.chmod(0o755)
    try:
        with pytest.raises(mirror.MirrorError, match="0700"):
            mirror.mirror(conn, source)
    finally:
        directory.chmod(0o700)
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0
    assert not (directory / "account-snapshot.json").exists()


def test_account_tables_reject_what_the_old_schema_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO holding_events (holding_id, code, event_type, event_date, created_at)"
                     " VALUES (1, '600519', 'transfer', '2026-10-08', 'now')")
    conn.execute("INSERT INTO holding_thesis_versions (holding_id, version, l1, l2, rewrite_reason, status, created_at)"
                 " VALUES (1, 1, 'a', 'b', 'c', 'active', 'now')")
    with pytest.raises(sqlite3.IntegrityError):  # one active thesis per holding
        conn.execute("INSERT INTO holding_thesis_versions (holding_id, version, l1, l2, rewrite_reason, status,"
                     " created_at) VALUES (1, 2, 'a', 'b', 'c', 'active', 'now')")


def test_policy_activations_only_append(conn):
    conn.execute("INSERT INTO policy_activations (policy, activated_at, activated_by, identity)"
                 " VALUES ('v0', ?, 'owner', '{}')", (db.now(),))
    for statement in ("UPDATE policy_activations SET policy = 'v1'", "DELETE FROM policy_activations"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)
    assert [row[0] for row in conn.execute("SELECT policy FROM policy_activations")] == ["v0"]


def test_research_coverage_uses_only_effective_bound_trades_at_their_execution_time(book, monkeypatch):  # noqa: F811
    conn, principal, directory, sha = book
    binding = dict(schema_version=1, account_id='a', account_scope='structural-scope', mirror_watermark={})
    conn.execute('INSERT INTO account_source VALUES (1,?,?,?,?)',
                 ('ledger', 'a' * 64, db.now(), gate.canonical(binding)))
    ctx = Context(conn, directory.parent / 'app.db', principal, account_shadow_enabled=True)
    assert invoke(ctx, 'account_research_coverage', {})['rows'] == []
    with monkeypatch.context() as published:
        published.setattr(db, 'now', lambda: SUCCESS['created_at'])
        _, report_id = saved(conn)
        saved(conn, passed=False)
    ledger.initialize(conn, principal, 'b', Opening(positions={}, cash='200000'),
                      datetime.fromisoformat('2026-10-01T00:00:00+08:00'), directory, sha, tolerance=Decimal('0'))
    assert ledger.submit(conn, principal, 'b', batch(book), directory)['status'] == 'posted'

    # The captured report was published between these two trades on the same day.
    early = buy(occurred_at='2026-10-27T10:00:00+08:00')
    late = buy(kind='sell', broker_id='sale', occurred_at='2026-10-27T14:00:00+08:00', report_id=report_id)
    assert submit(book, batch(book, events=[early, late], cash='200000', positions={}))['status'] == 'posted'
    morning, afternoon = invoke(ctx, 'account_research_coverage', {})['rows']
    assert (morning['event_type'], morning['available_report_id'], morning['stated_report_id']) == ('buy', None, None)
    assert (afternoon['event_type'], afternoon['available_report_id'], afternoon['stated_report_id']) == (
        'sell', report_id, report_id)

    correction = late.model_copy(update={'report_id': None})
    dividend = buy(kind='dividend', shares=0, price=0, cash='2500', broker_id='div',
                   occurred_at='2026-10-28T09:00:00+08:00')
    other = buy(code='000858', broker_id='next', price='120', occurred_at='2026-10-29T10:00:00+08:00')
    corrected = batch(book, version=1, events=[correction, dividend, other], voids=[afternoon['event_id']],
                      cash='190500', positions={'000858': 100})
    assert submit(book, corrected)['status'] == 'posted'
    first, second, third = invoke(ctx, 'account_research_coverage', {})['rows']
    assert first == morning and second['event_id'] != afternoon['event_id']
    assert (second['available_report_id'], second['same_day'], second['stated_report_id']) == (report_id, 1, None)
    assert (third['company_key'], third['available_report_id']) == ('000858', None)


@pytest.mark.parametrize(('mode', 'watermark'), [
    ('mirror', '{}'),
    ('ledger', '{'),
    ('ledger', gate.canonical(dict(schema_version=1, account_id='a', account_scope='structural-scope',
                                   mirror_watermark={}, extra=True))),
    ('ledger', gate.canonical(dict(schema_version=1, account_id='missing', account_scope='structural-scope', mirror_watermark={}))),
])
def test_research_coverage_refuses_an_unbound_source(book, mode, watermark):  # noqa: F811
    conn = book[0]
    conn.execute('INSERT INTO account_source VALUES (1,?,?,?,?)', (mode, 'a' * 64, db.now(), watermark))
    ctx = Context(conn, book[2].parent / 'app.db', book[1], account_shadow_enabled=True)
    with pytest.raises(ToolError, match='绑定') as error:
        invoke(ctx, 'account_research_coverage', {})
    assert error.value.status == 409
