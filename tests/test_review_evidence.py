"""Protocol boundary mutations over real captured reports; no semantic/model verdict is simulated."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from a_share_research.evidence import (EvidenceError, source_registry, projection, quote_match,
                                      citation_link, citation_errors, report_source_view)
from a_share_research.research import Material, check, _parse, _verified_facts
from a_share_research.reviewing import ReviewLedger, ReviewFormatError, digest, legacy_issue_view, PROTOCOL
from a_share_research.research_tools import record_stop
from a_share_research import service, db
from support.reports import MATERIAL, SALES, CASH, report
from support.templates import ENV
from support.continuation import load

DATA = Path(__file__).parent / "data"
RECORDS = json.loads((DATA / "continuation-records.json").read_text())["records"]
BODY = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 37)
REVIEW = json.loads((DATA / "603606-review.json").read_text())
FAILURE = json.loads((DATA / "research-delivery-failure.json").read_text())


def captured_registry():
    # The 10/23 capture contains quoted excerpts, not whole pages or the task60 final draft.
    pages = {"D0": {}, "D1": {}}
    for item in REVIEW["report"]["risks"]:
        for cite in item["cites"]:
            if "quote" in cite:
                pages[cite["source"]].setdefault(cite["page"], [])
                pages[cite["source"]][cite["page"]].append(cite["quote"])
    return source_registry(BODY["document"], {p: "\n".join(q) for p, q in pages["D0"].items()},
                           sources={"D1": {"document": BODY["sources"]["D1"]["document"],
                                            "pages": {p: "\n".join(q) for p, q in pages["D1"].items()}}})


def test_d0_and_supplementary_quotes_use_same_identity_and_official_link_contract():
    registry = captured_registry()
    for item in REVIEW["report"]["risks"]:
        for cite in item["cites"]:
            if "quote" not in cite:
                continue
            assert citation_errors([cite], registry, BODY["table"]) == []
            link = citation_link(cite, registry)
            assert link["url"] == registry[cite["source"]]["href"] + f'#page={cite["page"]}'
    # Annual and interim source periods differ; both remain lawful evidence for a cross-period claim.
    assert registry["D0"]["document"]["period"] != registry["D1"]["document"]["period"]
    assert all("http" not in r["text"] for r in REVIEW["report"]["risks"])


def test_catalog_identity_never_grants_unread_quote_and_projection_does_not_mutate_snapshot():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra = {"catalog": {"D1": BODY["sources"]["D1"]["document"]}}
    before = material.snapshot()
    registry = material.source_registry()
    assert projection(registry, catalog=True)["D1"]["state"] == "listed"
    assert "D1" not in projection(registry)
    assert quote_match(SALES | {"source": "D1"}, registry) is None
    material.prompt_block()
    assert material.snapshot() == before
    assert check(report(), material) == []  # Unknown base URL doesn't invent one or require prose duplication.


@pytest.mark.parametrize("field", ["code", "period", "url", "sha256"])
def test_source_identity_conflicts_fail_instead_of_selecting_a_version(field):
    doc = deepcopy(BODY["document"])
    conflicting = deepcopy(BODY["sources"]["D1"]["document"])
    catalog = deepcopy(doc)
    catalog[field] = conflicting[field] if conflicting[field] != doc[field] else "conflict"
    with pytest.raises(EvidenceError):
        source_registry(doc, catalog={"D0": catalog})
    view = report_source_view(BODY | {"sources": {"D0": {"document": doc}}})
    assert view["sources"] == {} and view["error"]


def test_real_peer_identity_is_allowed_but_false_explicit_identity_is_rejected():
    # Same actual excerpt added as a supplementary source while base is the real Moutai fixture.
    peer = captured_registry()["D1"]
    registry = source_registry(MATERIAL.doc.info(), MATERIAL.pages, sources={"D1": peer})
    cite = REVIEW["report"]["risks"][0]["cites"][-1]
    assert citation_errors([cite], registry, MATERIAL.table) == []
    assert citation_errors([cite | {"source_code": MATERIAL.code}], registry, MATERIAL.table)
    assert citation_errors([cite | {"page": 1}], registry, MATERIAL.table)


def test_neighbor_and_cross_page_matches_link_actual_pages_not_claimed_page():
    registry = captured_registry()
    cite = REVIEW["report"]["risks"][2]["cites"][-1]
    assert citation_link(cite | {"page": 20}, registry)["pages"] == [21]
    assert citation_link(cite | {"page": 20}, registry)["url"].endswith("#page=21")
    # Boundary split of the same real continuous excerpt; no invented financial content.
    text = cite["quote"]
    split = len(text) // 2
    registry["D0"]["pages"] = {21: text[:split], 22: text[split:]}
    assert quote_match(cite, registry) == [21, 22]
    registry["D0"]["pages"] = {20: text[:split], 22: text[split:]}
    assert quote_match(cite, registry) is None  # Never bridge an unread missing page.


def test_legacy_manifest_links_pdf_without_fabricated_page_verification():
    view = report_source_view(BODY)
    cite = REVIEW["report"]["risks"][2]["cites"][-1]
    link = citation_link(cite, view["sources"])
    assert link["url"] == BODY["document"]["url"] and link["pages"] == []
    html = ENV.get_template("_macros.html").module.cites({"cites": [cite]}, {}, view["sources"])
    assert "匹配页未核定" in html and "#page=" not in html
    full = ENV.get_template("_macros.html").module.cites({"cites": [cite | {"page": 20}]}, {}, captured_registry())
    assert "匹配第 21 页" in full and "#page=21" in full


def payload(ledger, draft, **changes):
    return {"version": 1, "draft_sha256": digest(draft), "material_sha256": digest(BODY),
            "issues": [], "resolutions": [], "risk_resolutions": []} | changes


def issue():
    # Actual 10/23 criticism, with a real captured same-year inventory row. NOT task60 or a semantic endorsement.
    return REVIEW["problems"][0] | {"where": "missing", "type": "omission",
                                    "importance": REVIEW["problems"][0]["problem"],
                                    "evidence": [{"ref": "T.inventory", "period": "2025-12-31"}]}


def accept(ledger, response, draft=REVIEW["report"]):
    return ledger.accept(response, draft, captured_registry(), BODY["table"], digest(BODY))


def pending_ledger():
    ledger = ReviewLedger()
    accept(ledger, payload(ledger, REVIEW["report"], issues=[issue()]))
    return ledger


@pytest.mark.parametrize("broken", [None, {}, {"issues": []}, {"version": 1}, []])
def test_invalid_or_empty_review_is_not_legal_empty_issues(broken):
    ledger = ReviewLedger()
    with pytest.raises(ReviewFormatError):
        accept(ledger, broken)
    assert ledger.rounds == []
    assert accept(ledger, payload(ledger, REVIEW["report"])) == []


@pytest.mark.parametrize("mutation", ["missing", "unknown", "duplicate", "invalid_status", "no_evidence", "bad_target"])
def test_pending_issue_cannot_disappear_or_be_closed_by_invalid_resolution(mutation):
    ledger = pending_ledger()
    before = ledger.snapshot()
    decision = {"id": ledger.pending()[0]["id"], "status": "resolved", "reason": issue()["problem"],
                "targets": ["risks[2]"], "evidence": issue()["evidence"]}
    resolutions = [decision]
    if mutation == "missing": resolutions = []
    if mutation == "unknown": decision["id"] = "unknown"
    if mutation == "duplicate": resolutions *= 2
    if mutation == "invalid_status": decision["status"] = "done"
    if mutation == "no_evidence": decision["evidence"] = []
    if mutation == "bad_target": decision["targets"] = ["risks[99]"]
    with pytest.raises(ReviewFormatError):
        accept(ledger, payload(ledger, REVIEW["report"], resolutions=resolutions))
    assert ledger.snapshot() == before


def test_new_draft_requires_fresh_bound_review_and_old_issue_can_be_refuted_with_evidence():
    ledger = pending_ledger()
    original = REVIEW["report"]
    changed = deepcopy(original)
    changed["risks"].reverse()  # A new draft, even with identical prose, can't reuse prior endorsement.
    valid = payload(ledger, original, resolutions=[{"id": ledger.pending()[0]["id"], "status": "dismissed",
                     "reason": issue()["problem"], "evidence": issue()["evidence"]}])
    with pytest.raises(ReviewFormatError):
        accept(ledger, valid, changed)
    valid["draft_sha256"] = digest(changed)
    assert accept(ledger, valid, changed) == []
    assert ledger.snapshot()["issues"][0]["status"] == "dismissed"


def test_repair_self_report_does_not_close_issue_and_unresolved_review_keeps_id():
    ledger = pending_ledger()
    draft = deepcopy(REVIEW["report"]) | {"resolutions": [{"status": "resolved"}]}
    issue_id = ledger.pending()[0]["id"]
    with pytest.raises(ReviewFormatError):
        accept(ledger, payload(ledger, draft), draft)
    result = accept(ledger, payload(ledger, draft, resolutions=[{"id": issue_id, "status": "unresolved",
                                    "reason": issue()["problem"]}]), draft)
    assert result[0]["id"] == issue_id and result[0]["status"] == "unresolved"


def test_independent_resolution_is_bound_to_new_target_and_material():
    ledger = pending_ledger()
    decision = {"id": ledger.pending()[0]["id"], "status": "resolved", "reason": issue()["problem"],
                "targets": ["risks[2]"], "evidence": issue()["evidence"]}
    response = payload(ledger, REVIEW["report"], resolutions=[decision])
    with pytest.raises(ReviewFormatError):
        accept(ledger, response | {"material_sha256": digest(MATERIAL.snapshot())})
    assert accept(ledger, response) == []
    assert ledger.snapshot()["issues"][0]["reviewed_draft_sha256"] == digest(REVIEW["report"])


def test_malformed_citation_fields_are_blocked_without_crashing_or_becoming_facts():
    for cite in ({"ref": []}, {"source": [], "page": 9, "quote": SALES["quote"]},
                 {"ref": "T.op_cash", "period": []}, {"ref": "T.op_cash", "quote": SALES["quote"]}):
        assert citation_errors([cite], MATERIAL.source_registry(), MATERIAL.table)
        assert check(report(business=[{"text": SALES["quote"], "cites": [cite]}]), MATERIAL)


def test_risk_merge_requires_reviewed_destination_not_original_count_or_wording():
    ledger = ReviewLedger()
    original = deepcopy(REVIEW["report"])
    ledger.protect_risks(original)
    changed = {"risks": [{"text": "；".join(i["text"] for i in original["risks"]),
                           "cites": [c for i in original["risks"] for c in i["cites"]]}]}
    changes = ledger.risk_changes(changed)
    assert len(changes) == 3
    with pytest.raises(ReviewFormatError):
        accept(ledger, payload(ledger, changed), changed)
    decisions = [{"id": key, "status": "merged", "reason": risk["text"], "targets": ["risks[0]"],
                  "evidence": [c for c in risk["cites"] if "quote" in c]} for key, risk in changes.items()]
    assert accept(ledger, payload(ledger, changed, risk_resolutions=decisions), changed) == []
    assert not ledger.risk_changes(original)  # Reordering identical items requires no invented explanation.


def test_new_omission_needs_actual_evidence_and_relevance():
    ledger = ReviewLedger()
    for key, bad in (("evidence", []), ("evidence", [{"ref": "no-such-row"}]), ("importance", ""), ("id", "old-id")):
        with pytest.raises(ReviewFormatError):
            accept(ledger, payload(ledger, REVIEW["report"], issues=[issue() | {key: bad}]))
        assert not ledger.rounds
    with pytest.raises(ReviewFormatError):
        accept(ledger, payload(ledger, REVIEW["report"], issues=[issue(), issue()]))


def test_old_actual_43_opinions_remain_unresolved_unknown_not_silent_pass():
    states = legacy_issue_view(FAILURE["problems"])
    assert {i["status"] for i in states} == {"not_recorded"}
    assert all(i["problem"] == old["problem"] for i, old in zip(states, FAILURE["problems"]))
    diagnostics = service.diagnostics("failed", FAILURE)
    assert diagnostics["failure"] is None and diagnostics["stop_reasons"] == []
    assert diagnostics["problems"] == FAILURE["problems"]  # Don't infer causality from old budget text.


def test_interrupted_repair_keeps_unresolved_opinion_and_safe_stage_in_api_and_page():
    ledger = pending_ledger()
    failure = {"stage": "repair", "category": "model_error", "message": "研究阶段未能完成"}
    diagnostics = service.diagnostics("failed", {"failure": failure, "review_audit": ledger.snapshot()}, report_available=False)
    assert diagnostics["failure"] == failure and diagnostics["problems"] == ledger.pending()
    html = ENV.get_template("run.html").render(run={"status": "failed", "code": BODY["code"], "error": failure["message"]},
                diagnostics=diagnostics, steps={"repair": "按复核意见修改"}, feedback_retry=False, csrf="", request_id="local")
    assert "按复核意见修改" in html and "中断前仍未解决" in html and "不会自动重试" in html
    assert "可信投资结论" in html


def test_interrupted_task_metadata_does_not_rehash_or_modify_evidence(tmp_path):
    ledger = pending_ledger()
    conn = db.connect(tmp_path / "isolated.db")
    run = db.create_run(conn, BODY["code"], status="failed")
    material = MATERIAL.snapshot()
    # Lifecycle-only field mutation over captured material, not a real model failure or a report43 replay.
    db.save_material(conn, run, material)
    db.update_run(conn, run, identity=json.dumps({"failure": {"stage": "repair", "category": "model_error"},
                                                 "review_audit": ledger.snapshot()}))
    view = service.run_view(conn, run_id=run)
    assert view["diagnostics"]["failure"] == {"stage": "repair", "category": "model_error"}
    assert view["diagnostics"]["problems"] == ledger.pending()
    saved = json.loads(conn.execute("SELECT material FROM runs WHERE id=?", (run,)).fetchone()[0])
    assert saved == json.loads(json.dumps(material))
    assert Material.from_snapshot(saved).snapshot()["sha256"] == material["sha256"]
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    conn.close()


def test_full_evidence_projection_preserves_raw_text_for_api_consumers():
    registry = MATERIAL.source_registry()
    assert projection(registry, raw=True)["D0"]["raw_pages"] == MATERIAL.raw_pages
    assert "raw_pages" not in projection(registry)["D0"]  # Don't duplicate text in every model prompt.


def test_report_and_material_source_id_conflict_is_visible_not_silently_overridden():
    # A deliberately mismatched pair of two actual captured companies must never borrow a source URL.
    view = report_source_view(BODY, MATERIAL.snapshot()["data"])
    assert view["error"] and view["sources"] == {}


def test_carried_issue_cannot_endorse_reordered_disputed_claim_as_fact():
    ledger = ReviewLedger()
    original = REVIEW["report"]
    finding = issue() | {"where": "risks[2]"}
    accept(ledger, payload(ledger, original, issues=[finding]), original)
    initial_facts = _verified_facts(original, ledger.pending(), reviewed=True)
    assert original["risks"][2] not in initial_facts["risks"]
    changed = deepcopy(original)
    changed["risks"].reverse()
    pending = accept(ledger, payload(ledger, changed, resolutions=[{
        "id": ledger.pending()[0]["id"], "status": "unresolved", "reason": finding["problem"]}]), changed)
    assert pending[0]["where"] == "risks[2]"  # Origin is retained for audit, not used as the new location.
    assert _verified_facts(changed, pending, reviewed=True) == {}


@pytest.mark.parametrize("snapshot_sources", [{}, {"D1": {"document": {}, "pages": {}}}])
def test_partial_material_retains_report_manifest_without_inventing_page_text(snapshot_sources):
    material = {"doc": {}, "extra": {"sources": snapshot_sources}}
    before = deepcopy((BODY, material))
    view = report_source_view(BODY, material)
    assert not view["error"]
    assert set(view["sources"]) == {"D0", *BODY["sources"]}
    for source_id in BODY["sources"]:
        original = BODY["sources"][source_id]
        saved = view["sources"][source_id]
        assert saved["document"] == original["document"]
        assert saved["read_pages"] == original["pages"]
        assert saved["pages"] == {} and saved["raw_pages"] == {}
    cite = REVIEW["report"]["risks"][0]["cites"][-1]
    link = citation_link(cite, view["sources"])
    assert link["url"] == BODY["sources"]["D1"]["document"]["url"] and not link["pages"]
    html = ENV.get_template("_macros.html").module.cites({"cites": [cite]}, {}, view["sources"])
    assert "匹配页未核定" in html
    assert (BODY, material) == before


def test_manifest_merge_still_rejects_source_identity_and_page_conflicts():
    material = {"doc": BODY["document"], "extra": {"sources": {"D1": {
        "document": BODY["document"], "pages": {}}}}}
    assert report_source_view(BODY, material)["error"]
    body = deepcopy(BODY)
    body["sources"]["D1"]["pages"] = {23: REVIEW["report"]["risks"][0]["cites"][-1]["quote"]}
    material["extra"]["sources"]["D1"] = {"document": body["sources"]["D1"]["document"],
                                             "pages": {23: MATERIAL.pages[9]}}
    assert report_source_view(body, material)["error"]


def test_unknown_reviewer_fields_never_enter_stored_or_public_audit():
    ledger = ReviewLedger()
    original = REVIEW["report"]
    finding = issue() | {"prompt": PROTOCOL, "debug": {"internal_prompt": PROTOCOL}}
    finding["evidence"] = [finding["evidence"][0] | {"prompt": PROTOCOL}]
    accept(ledger, payload(ledger, original, issues=[finding], prompt=PROTOCOL))
    assert PROTOCOL not in json.dumps(ledger.snapshot(), ensure_ascii=False)
    resolution = {"id": ledger.pending()[0]["id"], "status": "dismissed", "reason": issue()["problem"],
                  "evidence": issue()["evidence"], "prompt": PROTOCOL}
    accept(ledger, payload(ledger, original, resolutions=[resolution], prompt=PROTOCOL))
    snapshot = ledger.snapshot()
    assert PROTOCOL not in json.dumps(snapshot, ensure_ascii=False)
    # Independent public boundary also filters a previously saved/untrusted payload without rewriting it.
    legacy = deepcopy(snapshot)
    legacy["prompt"] = PROTOCOL
    legacy["rounds"][0]["prompt"] = PROTOCOL
    legacy["issues"][0]["resolution"]["debug"] = PROTOCOL
    public = service.diagnostics("done", {"review_audit": legacy})
    assert PROTOCOL not in json.dumps(public, ensure_ascii=False)
    assert legacy["prompt"] == PROTOCOL
    assert public["review_audit"]["issues"][0]["status"] == "dismissed"


def test_stop_is_deduplicated_separate_from_review_failure():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    record_stop(material, "fetch_limit")
    record_stop(material, "fetch_limit")
    assert len(material.extra["stop_reasons"]) == 1
    assert "failure" not in material.extra
    assert _parse("") is None and _parse("not JSON") is None


def test_full_api_keeps_report_sources_when_old_material_has_no_supplements(tmp_path):
    conn = db.connect(tmp_path / "sources.db")
    load(conn)
    row = conn.execute("SELECT run_id, body FROM reports WHERE id=37").fetchone()
    body = json.loads(row["body"])
    # Metadata-presence boundary over the actual capture, not a complete replay snapshot.
    db.update_run(conn, row["run_id"], material=json.dumps({"data": {"extra": {"sources": {}}}}))
    changes = conn.total_changes
    view = service.report_view(conn, 37)
    for source_id, saved in body["sources"].items():
        source = view["evidence"]["sources"][source_id]
        assert source["document"] == saved["document"]
        assert source["read_pages"] == saved["pages"] and source["pages"] == {}
    assert view["evidence"]["source_error"] is None
    assert conn.execute("SELECT body FROM reports WHERE id=37").fetchone()[0] == row["body"]
    assert conn.total_changes == changes
    conn.close()


def test_issue_on_a_deleted_item_closes_with_empty_targets_but_an_omission_cannot():
    ledger = ReviewLedger()
    draft = REVIEW["report"]
    flagged = issue() | {"type": "claim", "where": "risks[2]"}
    accept(ledger, payload(ledger, draft, issues=[flagged, issue()]))
    claim_id, omission_id = (i["id"] for i in ledger.pending())
    ledger.protect_risks(draft)
    repaired = deepcopy(draft)
    del repaired["risks"][2]  # Structural deletion of the captured flagged claim, not a model repair.
    risks = [{"id": key, "status": "dismissed", "reason": issue()["problem"], "evidence": issue()["evidence"]}
             for key in ledger.risk_changes(repaired)]
    removed = {"status": "resolved", "reason": issue()["problem"], "targets": [], "evidence": issue()["evidence"]}
    # Mutation: "[]" means the criticised item was deleted, which can never satisfy a missing-content finding.
    with pytest.raises(ReviewFormatError, match=omission_id):
        accept(ledger, payload(ledger, repaired, resolutions=[removed | {"id": claim_id}, removed | {"id": omission_id}],
                               risk_resolutions=risks), repaired)
    keep = {"id": omission_id, "status": "unresolved", "reason": issue()["problem"]}
    pending = accept(ledger, payload(ledger, repaired, resolutions=[removed | {"id": claim_id}, keep],
                                     risk_resolutions=risks), repaired)
    assert [i["id"] for i in pending] == [omission_id]


class Replies:
    """Feeds fixed protocol responses to review(); it renders no financial content of its own."""

    def __init__(self, *texts):
        self.texts, self.prompts = list(texts), []

    def ask(self, step, prompt):
        self.prompts.append(prompt)
        return self.texts.pop(0)


def test_rejected_review_gets_one_correction_with_the_exact_defect():
    from a_share_research.research import review
    draft = report()
    bound = {"version": 1, "draft_sha256": digest(draft), "material_sha256": MATERIAL.snapshot()["sha256"],
             "resolutions": [], "risk_resolutions": []}
    question = {"type": "question", "problem": REVIEW["problems"][0]["problem"],
                "importance": REVIEW["problems"][0]["problem"], "evidence": []}
    combined = json.dumps(bound | {"issues": [question | {"where": "risks[0] / risks[1]"}]}, ensure_ascii=False)
    fixed = json.dumps(bound | {"issues": [question | {"where": "risks[0]"}]}, ensure_ascii=False)
    ledger, runner = ReviewLedger(), Replies(combined, fixed)
    assert [i["where"] for i in review(runner, MATERIAL, draft, ledger)] == ["risks[0]"]
    assert "issues[0]" in runner.prompts[1] and len(ledger.rounds) == 1
    # A second malformed answer still fails closed with nothing recorded.
    ledger, runner = ReviewLedger(), Replies(combined, combined)
    with pytest.raises(ReviewFormatError):
        review(runner, MATERIAL, draft, ledger)
    assert ledger.snapshot()["issues"] == [] and ledger.rounds == []


def test_own_failure_reason_reaches_the_run_instead_of_a_generic_notice(tmp_path):
    from a_share_research.research import execute
    conn = db.connect(tmp_path / "isolated.db")
    source = db.create_run(conn, BODY["code"], status="failed")  # No material snapshot was ever saved.
    run = db.create_run(conn, BODY["code"])
    execute(conn, run, replay_of=source)
    row = conn.execute("SELECT error, identity FROM runs WHERE id=?", (run,)).fetchone()
    assert row["error"] == f"任务 {source} 没有完整材料快照，不能重放；不会重新取数补齐"
    assert json.loads(row["identity"])["failure"]["category"] == "source_input"
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    conn.close()


def test_provider_text_wrapped_in_source_error_stays_out_of_the_run(tmp_path, monkeypatch):
    from a_share_research import research, sources
    conn = db.connect(tmp_path / "isolated.db")
    run = db.create_run(conn, BODY["code"])
    provider = "TuShare fina_indicator: upstream detail"  # The wrapping format sources.py uses for provider errors.

    def gather(code):
        raise sources.SourceError(provider)
    monkeypatch.setattr(research, "gather", gather)
    research.execute(conn, run)
    row = conn.execute("SELECT error FROM runs WHERE id=?", (run,)).fetchone()
    assert row["error"] == "来源输入未能完成，请核对已保存资料。" and "upstream" not in row["error"]
    conn.close()
