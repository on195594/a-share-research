"""Method retrieval and revision using two real, reviewed Moutai corrections."""

import json
from pathlib import Path

from a_share_research import db, service
from a_share_research.checking import check
from a_share_research.research import _research_context
from a_share_research.research_tools import research_methods
from support.reports import MATERIAL, report

CAPTURED = json.loads((Path(__file__).parent / "data/research-methods.json").read_text())["reports"]


def test_only_completed_revision_replaces_the_target_method(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    for row in CAPTURED:
        conn.execute("INSERT INTO runs(id,code,status,created_at,request) VALUES (?,?,?,?,?)",
                     (row["run_id"], row["code"], "done", row["created_at"], json.dumps(row["request"])))
        conn.execute("INSERT INTO reports(id,run_id,code,period,passed,body,created_at) VALUES (?,?,?,?,?,?,?)",
                     (row["id"], row["run_id"], row["code"], row["period"], row["passed"],
                      json.dumps(row["body"]), row["created_at"]))
    original = conn.execute("SELECT body FROM reports WHERE id=11").fetchone()[0]
    for status in ("running", "failed", "done"):
        db.update_run(conn, 18, status=status)
        found = research_methods(conn, "现金流 口径")
        keys = {(r["report_id"], r["location"]) for r in found}
        assert ((11, "methods[0]") in keys) == (status != "done")
        assert ((12, "methods[0]") in keys) == (status == "done")
        assert (11, "methods[1]") in keys
    assert found[0]["code"] == "600519.SH"
    assert found[0]["sources"]["D1"]["document"]["url"].startswith("https://static.cninfo.com.cn/")
    assert conn.execute("SELECT body FROM reports WHERE id=11").fetchone()[0] == original
    assert all(not entry["claims_to_recheck"].get("methods") for entry in _research_context(conn, "600519.SH", None))
    assert _research_context(conn, "600519.SH", 12)[0]["claims_to_recheck"]["methods"] == CAPTURED[1]["body"]["methods"]
    assert research_methods(conn, "no matching research method") == []
    feedback_id = service.submit(conn, "600519", "method-review", parent_report_id=12,
                                 feedback={"location": "methods[0]", "text": CAPTURED[1]["request"]["feedback"]["text"]})
    assert feedback_id > 18
    conn.close()


def test_method_claims_must_have_evidence_in_this_material():
    # These real methods cite half-year pages absent from the annual-only material.
    result = report(methods=CAPTURED[0]["body"]["methods"])
    problems = check(result, MATERIAL)
    assert problems and all(p["where"].startswith("methods[") for p in problems)
    assert check(report(methods=[]), MATERIAL) == []
