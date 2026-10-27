"""Home-page staleness: financial coverage of real reports against the statutory disclosure calendar."""

import json
import sqlite3
from datetime import date
from pathlib import Path

from a_share_research import db
from a_share_research.sources import due_report

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {r["id"]: r for r in json.loads((ROOT / "tests/data/report-coverage.json").read_text())["reports"]}


def coverage(report: dict) -> str:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reports (period TEXT, body TEXT)")
    conn.execute("INSERT INTO reports VALUES (?, ?)", (report["period"], json.dumps({"table": report["table"]})))
    return conn.execute(f"SELECT {db.COVERAGE} FROM reports r").fetchone()[0]


def test_early_reports_cover_their_newer_financial_table():
    # 五粮液 and 比亚迪 recorded the prior annual report as period while their tables held the half year.
    for report_id in (6, 7):
        assert REPORTS[report_id]["period"] == "2025-12-31"
        assert coverage(REPORTS[report_id]) == "2026-06-30"
    assert coverage(REPORTS[37]) == "2026-06-30"
    assert coverage(REPORTS[35]) == "2026-09-30"


def test_due_report_follows_statutory_deadlines():
    assert due_report(date(2026, 10, 27)) == ("2026-06-30", "2026 年半年报")
    assert due_report(date(2026, 10, 31)) == ("2026-06-30", "2026 年半年报")  # the deadline day itself is allowed
    assert due_report(date(2026, 11, 1)) == ("2026-09-30", "2026 年三季报")
    # Annual and first-quarter reports share 04-30; until then the third quarter stays the latest due.
    assert due_report(date(2027, 4, 30)) == ("2026-09-30", "2026 年三季报")
    assert due_report(date(2027, 5, 1)) == ("2027-03-31", "2027 年一季报")
    assert due_report(date(2027, 9, 1)) == ("2027-06-30", "2027 年半年报")


def test_half_year_coverage_turns_stale_only_after_third_quarter_deadline():
    for report_id in (6, 7, 37):
        assert not coverage(REPORTS[report_id]) < due_report(date(2026, 10, 31))[0]
        assert coverage(REPORTS[report_id]) < due_report(date(2026, 11, 1))[0]
    # 紫金矿业 already covers the third quarter, so it stays current through the annual-report season.
    assert not coverage(REPORTS[35]) < due_report(date(2027, 4, 30))[0]
