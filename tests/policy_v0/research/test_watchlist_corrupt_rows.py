"""Watchlist reads survive corrupt legacy cache rows."""

from __future__ import annotations

from a_share_research.policy.v0 import commands_admin, db, domain


def test_watchlist_tolerates_corrupt_legacy_json_fields() -> None:
    now = domain.utc_now().isoformat()
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO stock_fundamentals
               (code, name, industry, data, updated_at, ttl_hours)
               VALUES ('CORRUPT','损坏缓存','制造','{bad-json',?,24)""",
            (now,),
        )
        conn.execute(
            """INSERT INTO analysis_results
               (code, date, result, created_at, flags, score_breakdown)
               VALUES ('CORRUPT',?,'legacy',?,'{bad-flags','[wrong-type]')""",
            (domain.cst_today(), now),
        )
        conn.commit()

    rows = commands_admin.get_watchlist_rows()

    assert len(rows) == 1
    assert rows[0]["code"] == "CORRUPT"
    assert rows[0]["flags"] == []
    assert rows[0]["score_breakdown"] is None
    assert rows[0]["pe_ttm"] is None
