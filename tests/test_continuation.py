"""Same-company continuation on a real reviewed report (东方电缆, production report 37)."""

import json
from pathlib import Path

from a_share_research import db
from a_share_research.research import _prior_report, _verified_facts, since_prior
from test_check import report

CAPTURED = json.loads((Path(__file__).parent / "data/continuation-603606.json").read_text())["report"]


def insert(conn, run_id: int, *, request: dict, identity: dict | None = None) -> None:
    conn.execute("INSERT INTO runs(id,code,status,created_at,request,identity) VALUES (?,?,?,?,?,?)",
                 (run_id, CAPTURED["code"], "done", CAPTURED["created_at"], json.dumps(request),
                  json.dumps(identity) if identity else None))
    conn.execute("INSERT INTO reports(id,run_id,code,period,passed,verdict,body,created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (run_id, run_id, CAPTURED["code"], CAPTURED["period"], CAPTURED["passed"], CAPTURED["verdict"],
                  json.dumps(CAPTURED["body"], ensure_ascii=False), CAPTURED["created_at"]))


def test_prior_report_carries_only_the_agenda_and_skips_replays_and_feedback(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    insert(conn, 1, request=CAPTURED["request"])
    insert(conn, 2, request=CAPTURED["request"], identity={"replay_of": 1})
    insert(conn, 3, request={"question": "核查", "parent_report_id": 1,
                             "feedback": {"location": "risks[0]", "text": "核查回款"}})
    prior = _prior_report(conn, CAPTURED["code"], 10)
    assert prior is not None
    assert prior["report_id"] == 1
    assert prior["risks"] == [item["text"] for item in CAPTURED["body"]["risks"]]
    assert prior["checkpoints"] == []  # written before checkpoints existed
    assert prior["period"] == "2026-06-30" and prior["market_date"] == "2026-10-26"
    assert "verdict" not in prior and "summary" not in prior
    assert CAPTURED["verdict"] not in json.dumps(prior, ensure_ascii=False)
    # A run never continues from itself or from later reports.
    assert _prior_report(conn, CAPTURED["code"], 1) is None
    conn.close()


def test_checkpoint_finding_keeps_other_reviewed_facts_but_never_the_checkpoints():
    facts = _verified_facts(report(), [{"where": "checkpoints[0]", "problem": "阈值无出处"}], reviewed=True)
    assert facts["risks"] and facts["business"] and facts["financial"]
    assert "checkpoints" not in facts


def test_since_prior_separates_backfilled_comparison_periods_from_new_disclosures():
    tables = json.loads((Path(__file__).parent / "data/since-prior-600519.json").read_text())["tables"]
    change = since_prior(tables["5"], tables["8"])
    # Report 8 added the prior-year half as a comparison period; nothing newer was disclosed in between.
    assert change["backfilled"] == ["2025-06-30"] and change["disclosed"] == []
    assert change["revised"] == [] and change["uncollected"] == []
    assert {v["label"]: (v["before"], v["after"]) for v in change["valuation"]}["市盈率TTM"] == (
        ("2026-10-22", "20.24"), ("2026-10-22", "20.24"))
    # Reversed, the half year is no longer new and an absent row is never read as zero.
    assert since_prior(tables["8"], tables["5"])["backfilled"] == []
