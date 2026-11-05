"""Which unresolved review issues may ship as named gaps: real issues from production reports 106/108."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from a_share_research.research import open_questions

CAPTURED = json.loads((Path(__file__).parent / "data/review-open-questions-20261105.json").read_text())


@pytest.mark.parametrize("report_id", ["106", "108"])
def test_unanswered_parts_of_a_reviewed_question_ship_as_gaps(report_id):
    # 中国电信 106 (3–5 年回报未求值) and 格力 108 (20% 压力测试只算 19.51%) failed only on these.
    problems = CAPTURED[report_id]["problems"]
    assert open_questions(problems, reviewed=True) == problems


def test_nothing_ships_without_a_completed_review_of_the_final_draft():
    assert open_questions(CAPTURED["108"]["problems"], reviewed=False) is None


@pytest.mark.parametrize("change", [
    {"type": "claim"},           # the written content is wrong, not merely incomplete
    {"type": "omission"},        # a material risk is missing
    {"where": "risks[0]"},       # doubt about a published claim
    {"where": "stance"},         # the stance itself is disputed
    {"where": "summary"},
    {"where": "answer[2]/risks[0]"},
    {"id": None},                # mechanical problems carry no ledger id
])
def test_any_other_unresolved_issue_still_blocks(change):
    problems = deepcopy(CAPTURED["108"]["problems"])
    problems[0] |= change
    assert open_questions(problems, reviewed=True) is None


def test_mechanical_failures_without_review_still_block():
    assert open_questions([{"where": "risks[0]", "problem": "引文不在所引页"}], reviewed=False) is None
    assert open_questions([], reviewed=True) is None


def _execute(tmp_path, monkeypatch, problems):
    from a_share_research import db, research
    from support.reports import MATERIAL, report
    conn = db.connect(tmp_path / "isolated.db")
    run = db.create_run(conn, MATERIAL.code)
    draft = report()
    monkeypatch.setattr(research, "gather", lambda code: research.Material.from_snapshot(deepcopy(MATERIAL.snapshot())))
    monkeypatch.setattr(research, "explore", lambda runner, material, acquisition: draft)
    monkeypatch.setattr(research, "revise", lambda runner, material, rep, ledger, acquisition: (rep, deepcopy(problems), True))
    research.execute(conn, run)
    row = conn.execute("SELECT r.status, p.passed, p.body FROM runs r JOIN reports p ON p.run_id = r.id").fetchone()
    conn.close()
    return row["status"], row["passed"], json.loads(row["body"])


def test_open_questions_publish_the_report_with_named_gaps(tmp_path, monkeypatch):
    problems = CAPTURED["108"]["problems"]
    status, passed, body = _execute(tmp_path, monkeypatch, problems)
    assert (status, passed) == ("done", 1) and "failure" not in body and "problems" not in body
    assert [q["id"] for q in body["open_questions"]] == [p["id"] for p in problems]
    assert body["gaps"][-2:] == [f"复核认为尚未完成：{p['problem']}" for p in problems]


def test_a_disputed_claim_still_fails_the_report(tmp_path, monkeypatch):
    problems = deepcopy(CAPTURED["108"]["problems"])
    problems[1]["type"] = "claim"
    status, passed, body = _execute(tmp_path, monkeypatch, problems)
    assert (status, passed) == ("failed", 0) and body["failure"]["category"] == "substantive_review"
    assert "open_questions" not in body
