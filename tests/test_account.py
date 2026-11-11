"""Migration v10 account tables, the pre-cutover mirror and the trade/research coverage query."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from a_share_research import db
from a_share_research.account import mirror, trades
from test_service_delivery import SUCCESS, saved

SOURCE_SCHEMA = (Path(__file__).parent / "data/account_source_schema.sql").read_text()


def source_database(path):
    """An old-shaped account database: production's schema with a few structural rows."""
    conn = sqlite3.connect(path)
    conn.executescript(SOURCE_SCHEMA)
    conn.executemany("INSERT INTO holdings (code, name, cost_price, shares, buy_date, updated_at, framework)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [("600519", "贵州茅台", 1400.5, 100, "2026-10-08", "2026-10-08T15:30:00+08:00", "E消费"),
                      ("000858", "五粮液", 120.0, 300, "2026-10-09", "2026-10-09T15:30:00+08:00", None),
                      ("600036", "招商银行", 40.25, 500, "2026-10-10", "2026-10-10T15:30:00+08:00", "B银行")])
    conn.execute("DELETE FROM holdings WHERE code = '600036'")  # leaves the id counter above the largest id
    conn.executemany("INSERT INTO holding_events (holding_id, code, event_type, event_date, shares, price,"
                     " cash_amount, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [(1, "600519", "buy", "2026-10-08", 100, 1400.5, -140050.0, None, "2026-10-08T15:30:00+08:00"),
                      (2, "000858", "buy", "2026-10-09", 300, 120.0, -36000.0, "首次建仓", "2026-10-09T15:30:00+08:00"),
                      (1, "600519", "dividend", "2026-10-20", None, None, 2500.0, None, "2026-10-20T09:00:00+08:00")])
    conn.execute("INSERT INTO holding_alerts (holding_id, code, level, category, reason_code, reason, opened_at,"
                 " updated_at) VALUES (1, '600519', 'yellow', 'unverified', 'legacy-1', '待分类', '2026-10-08', '2026-10-08')")
    conn.execute("INSERT INTO holding_thesis_versions (holding_id, version, l1, l2, rewrite_reason, status, created_at)"
                 " VALUES (1, 1, '品牌', '提价', 'initial', 'active', '2026-10-08')")
    conn.execute("INSERT INTO holding_l3_conditions (holding_id, condition_text, created_at, updated_at,"
                 " thesis_version_id) VALUES (1, '批价跌破成本线', '2026-10-08', '2026-10-08', 1)")
    conn.execute("INSERT INTO holding_tier_state (holding_id, updated_at) VALUES (1, '2026-10-08')")
    conn.execute("INSERT INTO retro_notes (holding_id, code, retro_text, created_at) VALUES (3, '600036', '复盘', '2026-10-11')")
    conn.executemany("INSERT INTO analysis_results (code, date, result, score) VALUES (?, ?, ?, ?)",
                     [("600519", "2026-10-07", "{}", 78), ("SAP", "2026-04-01", "{}", None)])
    conn.execute("INSERT INTO stock_fundamentals (code, name, industry, data, updated_at) VALUES"
                 " ('600519', '贵州茅台', '白酒', '{\"pe\": 21.9}', '2026-10-08')")
    conn.execute("INSERT INTO quote_snapshots (code, price, quote_date, quote_time, fetched_at, source,"
                 " verification_sources) VALUES ('600519', 1439.0, '2026-11-10', '15:00:00', '2026-11-10T15:01:00', 'tushare', '[]')")
    conn.execute("INSERT INTO qualitative_only_securities VALUES ('000858', '五粮液', '白酒', '2026-10-09')")
    conn.execute("INSERT INTO market_indicator_snapshots VALUES ('cn10y', 1.71, '2026-11-10', '2026-11-10T16:00:00', 'akshare', 'ok')")
    conn.execute("INSERT INTO schema_migrations VALUES ('001-core-tables', '2026-03-27')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def source(tmp_path):
    return source_database(tmp_path / "cache.db")


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    yield conn
    conn.close()


def test_mirror_copies_every_listed_column_and_keeps_id_counters(conn, source):
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
])
def test_source_that_left_the_manifest_is_refused_and_target_is_untouched(conn, source, change, message):
    mirror.mirror(conn, source)
    before = mirror.digest(mirror._read(conn))
    change(source)
    with pytest.raises(mirror.MirrorError) as error:
        mirror.mirror(conn, source)
    assert message in str(error.value)
    assert mirror.digest(mirror._read(conn)) == before


def test_copy_that_does_not_match_the_source_is_rolled_back(conn, source):
    first = mirror.mirror(conn, source)
    # Minimal tamper variant: the target alters a copied value while the mirror writes it.
    conn.execute("CREATE TRIGGER tamper AFTER INSERT ON holdings BEGIN"
                 " UPDATE holdings SET shares = shares + 1 WHERE id = NEW.id; END")
    with pytest.raises(mirror.MirrorError, match="已回滚"):
        mirror.mirror(conn, source)
    assert mirror.digest(mirror._read(conn)) == first["source_sha256"]
    assert conn.execute("SELECT source_sha256 FROM account_source").fetchone()[0] == first["source_sha256"]


def test_mirror_never_runs_after_the_cutover(conn, source):
    mirror.mirror(conn, source)
    conn.execute("UPDATE account_source SET mode = 'ledger'")
    conn.execute("UPDATE holdings SET shares = 150 WHERE code = '600519'")  # a post-cutover entry
    with pytest.raises(mirror.MirrorError, match="已切换"):
        mirror.mirror(conn, source)
    assert conn.execute("SELECT shares FROM holdings WHERE code = '600519'").fetchone()[0] == 150


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


def test_research_available_at_trade_only_counts_formal_reports_dated_up_to_the_trade(conn):
    _, report = saved(conn)
    saved(conn, passed=False)
    today = date.fromisoformat(db.now()[:10])
    code = SUCCESS["code"]
    events = [("buy", today - timedelta(days=1), code), ("buy", today, code), ("sell", today + timedelta(days=1), code),
              ("dividend", today + timedelta(days=2), code), ("buy", today + timedelta(days=1), "000858")]
    conn.executemany("INSERT INTO holding_events (holding_id, code, event_type, event_date, created_at)"
                     " VALUES (1, ?, ?, ?, 'now')", [(c, kind, day.isoformat()) for kind, day, c in events])

    rows = trades.research_available_at_trade(conn)
    assert [(row["event_type"], row["available_report_id"], row["same_day"]) for row in rows] == [
        ("buy", None, None), ("buy", report, 1), ("sell", report, 0), ("buy", None, None)]
    assert all(row["stated_report_id"] is None for row in rows)
