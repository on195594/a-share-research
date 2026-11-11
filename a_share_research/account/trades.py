"""How trades relate to research that existed when they were made (coverage diagnostics only)."""
from __future__ import annotations

import sqlite3

from a_share_research import db

# For each buy or sell: the latest formal report on the same company dated on or before the trade day.
# A same-day report cannot be ordered against the trade, so nothing causal may be read from this.
_AVAILABLE = f"""
SELECT e.id AS event_id, e.company_key, e.event_type, e.event_date, e.report_id AS stated_report_id,
       (SELECT r.id FROM reports r JOIN runs u ON u.id = r.run_id
         WHERE r.company_key = e.company_key AND {db.FORMAL_REPORT}
           AND substr(r.created_at, 1, 10) <= e.event_date
         ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS available_report_id
  FROM holding_events e WHERE e.event_type IN ('buy', 'sell')
"""


def research_available_at_trade(conn: sqlite3.Connection) -> list[dict]:
    """One row per buy or sell; available_report_id is None when no report of this system preceded it."""
    rows = conn.execute(
        f"SELECT a.*, substr(r.created_at, 1, 10) AS available_report_date,"
        f" substr(r.created_at, 1, 10) = a.event_date AS same_day"
        f" FROM ({_AVAILABLE}) a LEFT JOIN reports r ON r.id = a.available_report_id"
        " ORDER BY a.event_date, a.event_id")
    return [dict(row) for row in rows]
