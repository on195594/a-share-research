"""Captured research success and failure records for delivery tests."""

import json
from pathlib import Path

from a_share_research import db, service
from support.reports import MATERIAL

RECORDS = json.loads((Path(__file__).resolve().parents[1] / "data/continuation-records.json").read_text())["records"]
SUCCESS = next(item["report"] for item in RECORDS if item["report"]["id"] == 143)
FAILURE = json.loads((Path(__file__).resolve().parents[1] / "data/research-delivery-failure.json").read_text())


def saved(conn, *, passed=True, parent=None):
    source = SUCCESS if passed else FAILURE
    run = service.submit(conn, source["code"], f"request-{parent}-{passed}", parent_report_id=parent)
    body = source["body"] if passed else {key: FAILURE[key] for key in ("problems", "gaps", "announcement_searches")}
    period = source["period"] if passed else max(source["coverage_periods"])
    db.save_report(conn, run, source["code"], source["name"], period, passed,
                   source.get("verdict") if passed else None, body)
    # Real captured annual-report excerpts supplement the captured same-company success body.
    db.update_run(conn, run, status="done" if passed else "failed", finished_at=db.now(),
                  material=json.dumps(MATERIAL.snapshot()) if passed else None,
                  error=None if passed else FAILURE["error"])
    return run, conn.execute("SELECT id FROM reports WHERE run_id = ?", (run,)).fetchone()[0]
