"""Declared report references and point-in-time research coverage of posted trades."""
from __future__ import annotations

from contextlib import nullcontext
import sqlite3

from a_share_research import db
from a_share_research.account.ledger import LedgerError


def research_available_at_trade(conn: sqlite3.Connection) -> list[dict]:
    """Return the latest available formal report separately from the trade's explicit basis."""
    with nullcontext() if conn.in_transaction else db.transaction(conn, immediate=False):
        bound = conn.execute("""
            WITH source AS (
                SELECT CASE WHEN json_valid(watermark) THEN watermark END AS binding
                  FROM account_source WHERE id = 1 AND mode = 'ledger'
            )
            SELECT b.account_id FROM source JOIN account_books b
                ON b.account_id = json_extract(binding, '$.account_id')
             WHERE json_type(binding, '$.account_id') = 'text'
               AND json_type(binding, '$.account_scope') = 'text'
               AND trim(json_extract(binding, '$.account_scope')) != ''
               AND json_type(binding, '$.schema_version') = 'integer'
               AND json_extract(binding, '$.schema_version') = 1
               AND json_type(binding, '$.mirror_watermark') = 'object'
        """).fetchone()
        if bound is None:
            raise LedgerError('成交研究覆盖需要已绑定的账本，请核对账户来源与账本绑定', 409)
        rows = conn.execute(f"""
            WITH trades AS (
                SELECT j.id AS event_id, j.company_key, j.occurred_at,
                       json_extract(j.event, '$.kind') AS event_type,
                       json_extract(j.event, '$.report_id') AS stated_report_id
                  FROM account_journal j
                 WHERE j.account_id = ?
                   AND json_extract(j.event, '$.kind') IN ('buy', 'sell')
                   AND NOT EXISTS (SELECT 1 FROM account_voids v WHERE v.event_id = j.id)
            )
            SELECT t.*, substr(t.occurred_at, 1, 10) AS event_date,
                   available.id AS available_report_id, substr(available.created_at, 1, 10) AS available_report_date,
                   substr(available.created_at, 1, 10) = substr(t.occurred_at, 1, 10) AS same_day
              FROM trades t LEFT JOIN reports available ON available.id = (
                   SELECT r.id FROM reports r JOIN runs u ON u.id = r.run_id
                    WHERE r.company_key = t.company_key AND {db.FORMAL_REPORT}
                      AND r.created_at <= t.occurred_at AND (u.finished_at IS NULL OR u.finished_at <= t.occurred_at)
                    ORDER BY r.created_at DESC, r.id DESC LIMIT 1
              )
             ORDER BY t.occurred_at, t.event_id
        """, (bound[0],))
        return [dict(row) for row in rows]
