"""Method retrieval and revision using two real, reviewed Moutai corrections."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from a_share_research import db, service
from a_share_research.checking import check
from a_share_research.research import _research_context
from a_share_research.research_tools import (
    ResearchTools, active_methods_digest, method_cards, method_catalog, research_methods, run_method_catalog,
)
from support.reports import MATERIAL, report

CARDS = Path(__file__).parent / "data/method-cards"  # real card copied from the shared wiki, 2026-11-14
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
    frozen = method_catalog(conn, before=CAPTURED[0]["created_at"])
    frozen_digest = active_methods_digest(conn, catalog=frozen)
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
    # A later method correction cannot change the catalog available to an already-started batch/run.
    from copy import deepcopy
    material = deepcopy(MATERIAL)
    material.extra["method_catalog"] = frozen
    retrieved = ResearchTools(material, conn).research_methods("现金流 口径")
    assert (11, "methods[0]") in {(r["report_id"], r["location"]) for r in retrieved}
    assert all(r["report_id"] != 12 for r in retrieved)
    assert material.extra["methods_used"] == retrieved
    assert active_methods_digest(conn, catalog=frozen) == frozen_digest
    assert active_methods_digest(conn) != frozen_digest
    assert method_catalog(conn, before=CAPTURED[0]["created_at"]) == frozen
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


def test_method_cards_join_only_single_stock_catalogs_and_carry_no_evidence_identity(tmp_path, caplog):
    raw = (CARDS / "cyclical-earnings-normalization.md").read_bytes()
    cards = method_cards(CARDS)
    assert [card["card"] for card in cards] == ["cyclical-earnings-normalization"]
    card = cards[0]
    assert card["sha256"] == hashlib.sha256(raw).hexdigest()
    assert card["source_urls"] == [
        "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/commodityvaluedrivers.htm"]
    assert card["title"] and card["verified_at"] == "2026-11-14"
    assert card["method"]["text"].startswith("# ") and "status: stable" not in card["method"]["text"]
    # A card is guidance only: it has no report, evidence source or table row a report could cite.
    assert not {"report_id", "sources", "table", "code"} & set(card)

    conn = db.connect(tmp_path / "app.db")
    assert run_method_catalog(conn, before=None, batch=False, cards_dir=CARDS) == cards
    assert run_method_catalog(conn, before=None, batch=True, cards_dir=CARDS) == []
    assert run_method_catalog(conn, before=None, batch=False, cards_dir=None) == []
    conn.close()

    # Retrieval, the returned-record trail and the digest reuse the existing frozen-catalog path.
    material = deepcopy(MATERIAL)
    material.extra["method_catalog"] = cards
    tools = ResearchTools(material, None)
    assert tools.research_methods("低PE 正常化") == cards
    assert tools.research_methods("应收保理") == []
    assert material.extra["methods_used"] == cards
    assert '"card": "cyclical-earnings-normalization"' in material.prompt_block()
    assert active_methods_digest(None, catalog=cards) != active_methods_digest(None, catalog=[])

    # Minimal labelled variants of the real card: drafts and pages without frontmatter are not loaded.
    (tmp_path / "draft.md").write_bytes(raw.replace(b"status: stable", b"status: draft"))
    (tmp_path / "plain.md").write_text("# no frontmatter")
    assert method_cards(tmp_path) == []
    assert "draft.md" in caplog.text and "plain.md" in caplog.text
