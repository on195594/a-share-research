#!/usr/bin/env python3
"""Same-material stability of research stances and hurdle prices.

Consecutive reports of one company on the same financial period (no new periodic report
in between) are reruns on the same material; this prints each company's stance sequence,
flips and hurdle-price range. Read-only, no model calls.

Natural reruns come from production. For an active measurement (D/E changes), copy the
production database into an isolated DATA_DIR, run full research there N times, and point
--db at the copy; the scenarios are chosen during evidence gathering, so replaying only the
final writing call reproduces them and measures nothing.

  uv run python scripts/stability.py [--since 2026-11-01] [--code 601600] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from a_share_research import report as report_body  # noqa: E402

DATABASE = Path('/home/lin/a-share-research-data/app.db')


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def spread(prices: list) -> dict:
    """Range relative to the median; the boundary band must exceed this to mean anything."""
    if not prices:
        return dict(n=0)
    middle = statistics.median(prices)
    return dict(n=len(prices), min=round(min(prices), 2), max=round(max(prices), 2),
                median=round(middle, 2), range_pct=round((max(prices) - min(prices)) / middle * 100, 1))


def observed(conn, since: str, code: str | None) -> dict:
    rows = conn.execute(
        "SELECT r.id, r.code, r.name, r.created_at, r.body, c.identity FROM reports r "
        "LEFT JOIN calls c ON c.id = (SELECT max(id) FROM calls WHERE run_id = r.run_id AND step = 'explore') "
        "WHERE r.created_at >= ? AND (? IS NULL OR r.code LIKE ? || '%') ORDER BY r.code, r.id",
        (since, code, code)).fetchall()
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        body: dict = dict(report_body.load(row['body']))
        calc = body.get('stance_calc') or {}
        v2 = (body.get('valuation_calc') or {}).get('v2') or {}
        if not body.get('stance') or body.get('replay_of') is not None:
            continue
        model = (json.loads(row['identity'] or '{}').get('actual') or {}).get('model')
        groups.setdefault((row['code'], body.get('period')), []).append(dict(
            report=row['id'], date=row['created_at'][:10], model=model, close=(body.get('valuation_calc') or {}).get('close'),
            action=body['stance'].get('action'), hurdle=calc.get('price') or v2.get('support_below')))
    result = []
    for (company, period), items in groups.items():
        if len(items) < 2:
            continue
        actions = [item['action'] for item in items]
        prices = [item['hurdle'] for item in items if isinstance(item['hurdle'], (int, float))]
        result.append(dict(code=company, period=period, reports=items,
                           flips=sum(a != b for a, b in zip(actions, actions[1:])), hurdle=spread(prices)))
    ranges = [group['hurdle']['range_pct'] for group in result if group['hurdle'].get('n', 0) >= 2]
    return dict(since=since, groups=result,
                summary=dict(companies=len(result), with_flip=sum(group['flips'] > 0 for group in result),
                             median_range_pct=statistics.median(ranges) if ranges else None))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=DATABASE)
    parser.add_argument('--since', default='2026-11-01')
    parser.add_argument('--code')
    args = parser.parse_args(argv)
    print(json.dumps(observed(connect(args.db), args.since, args.code), ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
