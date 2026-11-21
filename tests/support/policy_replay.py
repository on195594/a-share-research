"""Holdings, dividends and monitor inputs for v0 replays."""

import json
import sqlite3
from pathlib import Path

from a_share_research import db as app_db
from a_share_research.policy.v0 import cache, domain, market_quotes


def _seed_holding(
    database: Path,
    code: str = "600036",
    *,
    holding_id: int | None = None,
    closed: bool = False,
    shares: int = 100,
    l3_status: str = "not_triggered",
    l3_action: str = "review",
    next_review: str = "2999-01-01",
) -> int:
    app_db.connect(Path(database)).close()
    with sqlite3.connect(database) as conn:
        cursor = conn.execute(
            """INSERT INTO holdings
               (id, code, name, cost_price, shares, buy_date, stop_loss_15,
                stop_loss_20, updated_at, exit_price, exit_date, framework,
                initial_shares, main_entry_date, reference_cost, framework_confident)
               VALUES (?, ?, ?, 100, ?, '2026-01-01', 85, 80, ?, ?, ?,
                       'A通用', ?, '2026-01-01', 100, 1)""",
            (
                holding_id,
                code,
                f"fixture-{code}",
                shares,
                domain.utc_now().isoformat(),
                110 if closed else None,
                "2026-06-01" if closed else None,
                shares,
            ),
        )
        row_id = int(cursor.lastrowid if holding_id is None else holding_id)
        if not closed:
            conn.execute(
                """INSERT INTO holding_tier_state (holding_id, updated_at)
                   VALUES (?, ?)""",
                (row_id, domain.utc_now().isoformat()),
            )
            thesis_id = conn.execute(
                """INSERT INTO holding_thesis_versions
                   (holding_id, version, l1, l2, rewrite_reason, status, created_at)
                   VALUES (?, 1, 'quality', 'timing', 'fixture', 'active', ?)""",
                (row_id, domain.utc_now().isoformat()),
            ).lastrowid
            conn.execute(
                """INSERT INTO holding_l3_conditions
                   (holding_id, thesis_version_id, condition_text, origin_type,
                    status, evidence, as_of, next_review_date,
                    temporary_exit_rule, is_active, condition_scope, action_level,
                    materiality_basis, created_at, updated_at)
                   VALUES (?, ?, 'fixture condition', 'new_monitoring', ?,
                           'official fixture evidence', ?, ?, 'review next report',
                           1, 'core_driver', ?, 'core driver', ?, ?)""",
                (
                    row_id,
                    thesis_id,
                    l3_status,
                    domain.cst_today(),
                    next_review,
                    l3_action,
                    domain.utc_now().isoformat(),
                    domain.utc_now().isoformat(),
                ),
            )
        conn.commit()
    return row_id


def _quotes(codes: list[str], *, price: float = 100) -> dict:
    return {
        code: market_quotes.PriceQuote(
            price,
            domain.cst_today(),
            "10:00:00",
            "fixture",
            previous_close=price,
            industry_change_pct=0,
        )
        for code in codes
    }


def _run(capsys, argv: list[str]) -> tuple[int, dict]:
    result = cache.main(["monitor-snapshot", *argv, "--json"])
    captured = capsys.readouterr()
    return result, json.loads(captured.out)


HUADONG_EVENTS = [
    ("buy", "2026-06-10", 600, 30.1367, 0.0, 0.0, None, 0),
    ("buy", "2026-08-06", 200, 27.81, 4.84, 0.0, None, 0),
]


HUADONG_DIVIDENDS = [
    # Record date before the first buy: not entitled.
    {"record_date": "2026-06-09", "ex_date": "2026-06-10", "pay_date": "2026-06-10",
     "cash_div": 0.58, "split_ratio": 0.0},
    {"record_date": "2026-09-17", "ex_date": "2026-09-18", "pay_date": "2026-09-18",
     "cash_div": 0.35, "split_ratio": 0.0},
]
