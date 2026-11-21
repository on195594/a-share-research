"""Structural ledger events, snapshots and account source databases."""

import sqlite3
from decimal import Decimal
from pathlib import Path

from a_share_research.account import ledger
from a_share_research.account.reconciliation import Event, Snapshot


def buy(**changes):
    return Event.model_validate(dict(kind='buy', code='600519', shares=100, price='1400.5',
                                    occurred_at='2026-10-08T15:30:00+08:00', broker_id='trade1') | changes)


def batch(book, *, version=0, events=None, positions=None, cash='59950', voids=()):
    positions = positions if positions is not None else {'600519':100}
    values = {code: Decimal(shares)*100 for code, shares in positions.items() if shares}
    return ledger.Batch(expected_version=version, events=[buy()] if events is None else events,
                        void_event_ids=list(voids), evidence_hashes=[book[3]],
                        snapshot=Snapshot(positions=positions, cash=cash, market_values=values,
                                          total_assets=Decimal(cash)+sum(values.values())))


def submit(book, value):
    return ledger.submit(book[0],book[1],'a',value,book[2])


SOURCE_SCHEMA = (Path(__file__).resolve().parents[1] / "data/account_source_schema.sql").read_text()


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
    path.with_name("account-snapshot.json").write_text('{"schema_version": 1, "portfolio_value": 500000}')
    return path
