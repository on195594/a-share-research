"""Declared report references and point-in-time research coverage of posted trades."""
from __future__ import annotations

import sqlite3

from a_share_research import db


def research_available_at_trade(conn: sqlite3.Connection, account_id: str) -> list[dict]:
    """Return the latest available formal report separately from the trade's explicit basis."""
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
                WHERE r.company_key = t.company_key AND {db.BLIND_FORMAL_REPORT}
                  AND r.created_at <= t.occurred_at AND (u.finished_at IS NULL OR u.finished_at <= t.occurred_at)
                ORDER BY r.created_at DESC, r.id DESC LIMIT 1
          )
         ORDER BY t.occurred_at, t.event_id
    """, (account_id,))
    return [dict(row) for row in rows]
