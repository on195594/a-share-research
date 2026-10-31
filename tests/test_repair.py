"""Patch boundary mutations of captured report bodies, without model calls or invented facts."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from a_share_research.checking import check
from a_share_research.material import Material
from a_share_research.prompts import RULES, output_schema
from a_share_research.research import (
    PATCH_SCHEMA, RepairFormatError, merge_repair_patch, repair, repair_prompt, repair_result,
)
from a_share_research.reviewing import ReviewLedger, ReviewFormatError, digest
from a_share_research.sources import Document

DATA = Path(__file__).parent / "data"
RECORDS = json.loads((DATA / "continuation-records.json").read_text())["records"]
BODY = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 37)
NORMAL = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 39)
REVIEW = json.loads((DATA / "603606-review.json").read_text())
METHODS = json.loads((DATA / "research-methods.json").read_text())["reports"][0]["body"]["methods"]
ITEM = BODY["risks"][0]
MATERIAL = Material(BODY["code"], BODY["name"], Document(**BODY["document"]), {}, {},
                    BODY["table"], BODY["market_date"])
METHOD_REQUEST = json.loads((DATA / "research-methods.json").read_text())["reports"][0]["request"]


def serialized(value):
    return json.dumps(value, ensure_ascii=False).encode()


def replace(where="risks[2]", value=ITEM):
    return {"op": "replace", "where": where, "value": deepcopy(value)}


def bound(draft, **changes):
    return {"version": 1, "draft_sha256": digest(draft), "material_sha256": digest(BODY),
            "issues": [], "resolutions": [], "risk_resolutions": []} | changes


def finding():
    # Captured inventory criticism and table row; protocol metadata is not a semantic verdict.
    return REVIEW["problems"][0] | {"where": "missing", "type": "omission",
                                   "importance": REVIEW["problems"][0]["problem"],
                                   "evidence": [{"ref": "T.inventory", "period": "2025-12-31"}]}


def pending_ledger():
    ledger = ReviewLedger()
    ledger.accept(bound(BODY, issues=[finding()]), BODY, {}, BODY["table"], digest(BODY))
    return ledger


@pytest.mark.parametrize("original", [BODY, NORMAL])
def test_empty_patch_preserves_every_byte_and_has_no_aliases(original):
    draft = deepcopy(original)
    merged = merge_repair_patch(draft, {"ops": []}, [], MATERIAL)
    assert serialized(merged) == serialized(original)
    merged["risks"][0]["cites"].clear()
    assert serialized(draft) == serialized(original)


def test_replace_delete_add_use_original_indices_and_preserve_untouched_content():
    draft = deepcopy(BODY)
    patch = {"ops": [{"op": "delete", "where": "risks[0]"},
                     {"op": "add", "where": "risks", "value": BODY["risks"][0]},
                     replace(), {"op": "delete", "where": "risks[3]"},
                     {"op": "add", "where": "risks", "value": BODY["risks"][3]}]}
    before_patch = deepcopy(patch)
    merged = merge_repair_patch(draft, patch, [], MATERIAL)
    assert merged["risks"] == [BODY["risks"][1], ITEM, BODY["risks"][4], ITEM, BODY["risks"][3]]
    for old_index, new_index in [(1, 0), (4, 2)]:
        assert serialized(merged["risks"][new_index]) == serialized(BODY["risks"][old_index])
    for key in BODY.keys() - {"risks"}:
        assert serialized(merged[key]) == serialized(BODY[key])
    assert draft == BODY and patch == before_patch
    # Changing the order of deletes/replaces cannot shift their original targets.
    reordered = {"ops": [patch["ops"][3], patch["ops"][2], *patch["ops"][:2], patch["ops"][4]]}
    assert merge_repair_patch(draft, reordered, [], MATERIAL) == merged
    merged["risks"][1]["cites"].clear()
    assert draft == BODY and patch == before_patch


@pytest.mark.parametrize("section,item", [
    ("risks", ITEM), ("business", BODY["business"][0]), ("financial", BODY["financial"][0]),
    ("answer", NORMAL["answer"][0]), ("checkpoints", NORMAL["checkpoints"][0]), ("methods", METHODS[0]),
])
def test_all_schema_sections_can_append_and_missing_optional_sections_can_be_built(section, item):
    merged = merge_repair_patch(BODY, {"ops": [{"op": "add", "where": section, "value": item}]}, [], MATERIAL)
    assert merged[section] == BODY.get(section, []) + [item]
    replaced = merge_repair_patch(merged, {"ops": [replace(f"{section}[0]", item)]}, [], MATERIAL)
    deleted = merge_repair_patch(replaced, {"ops": [{"op": "delete", "where": f"{section}[0]"}]}, [], MATERIAL)
    assert deleted.get(section, []) == replaced[section][1:]


def test_optional_scalar_replacements_do_not_rewrite_other_fields():
    changes = {k: NORMAL[k] for k in ("verdict", "summary", "gaps")}
    assert merge_repair_patch(BODY, changes, [], MATERIAL) == BODY | changes


@pytest.mark.parametrize("patch,match", [
    (None, "补丁须为对象"), ([], "补丁须为对象"),
    ({"ops": {}}, "ops 必须是数组"), ({"ops": [], "risks": BODY["risks"]}, "不要输出完整报告"),
    ({"ops": [], "business": BODY["business"][1:]}, "不要输出完整报告"),
    ({"ops": [None]}, r"ops\[0\]"), ({"ops": [{"op": "move"}]}, "replace/delete/add"),
    ({"ops": [replace("unknown[0]")]}, "章节或位置不存在"),
    ({"ops": [{"op": "add", "where": "unknown", "value": ITEM}]}, "章节或位置不存在"),
    ({"ops": [replace("risks[-1]")]}, "章节或位置不存在"),
    ({"ops": [replace("risks[1.0]")]}, "章节或位置不存在"),
    ({"ops": [replace("risks[True]")]}, "章节或位置不存在"),
    ({"ops": [replace("risks[01]")]}, "章节或位置不存在"),
    ({"ops": [replace("risks")]}, "章节或位置不存在"),
    ({"ops": [replace(["risks", 0])]}, "章节或位置不存在"),
    ({"ops": [replace(f"risks[{len(BODY['risks'])}]")]}, "越界"),
    ({"ops": [replace("methods[0]")]}, "越界"),
    ({"ops": [replace(), replace()]}, "重复操作"),
    ({"ops": [replace(), {"op": "delete", "where": "risks[2]"}]}, "既删又改"),
    ({"ops": [{"op": "delete", "where": "risks[2]"}, replace()]}, "既删又改"),
    ({"ops": [{"op": "delete", "where": "risks[2]"}] * 2}, "重复操作"),
    ({"ops": [{"op": "replace", "where": "risks[2]"}]}, "必须包含字段"),
    ({"ops": [replace(value=None)]}, "条目对象"),
    ({"ops": [], "resolutions": []}, "补丁须为对象"),
    ({"ops": [], "risk_resolutions": []}, "补丁须为对象"),
])
def test_malformed_patches_are_actionable_and_atomic(patch, match):
    draft, before = deepcopy(BODY), deepcopy(patch)
    with pytest.raises(RepairFormatError, match=match):
        merge_repair_patch(draft, patch, [], MATERIAL)
    assert draft == BODY and patch == before


def test_invalid_late_operation_does_not_apply_earlier_valid_operations():
    draft = deepcopy(BODY)
    with pytest.raises(RepairFormatError, match=r"ops\[1\].*越界"):
        merge_repair_patch(draft, {"ops": [replace(), replace("risks[999]")]}, [], MATERIAL)
    assert draft == BODY
    with pytest.raises(RepairFormatError, match="不是条目数组"):
        merge_repair_patch(draft | {"risks": ITEM}, {"ops": [replace()]}, [], MATERIAL)


def test_patch_parser_rejects_invalid_objects_and_full_report_parser_is_unchanged():
    for response in ("not JSON", "[]", "null", json.dumps(BODY)):
        with pytest.raises(RepairFormatError):
            repair_result(BODY, response, [], MATERIAL)
    assert repair_result(None, json.dumps(BODY), [], MATERIAL) == BODY
    assert repair_result(None, "not JSON", [], MATERIAL) is None
    assert repair_result({}, '{"ops": []}', [], MATERIAL) == {}
    assert repair_result(BODY, '{"ops": [], "ops": []}', [], MATERIAL) == BODY
    # A valid patch still targets the captured original position.
    assert repair_result(BODY, json.dumps({"ops": [replace()]}), [REVIEW["problems"][0]], MATERIAL)["risks"][2] == ITEM


def test_prompt_indexes_original_items_and_keeps_rules_and_full_report_fallback():
    prompt = repair_prompt(MATERIAL, BODY, REVIEW["problems"])
    indexed = json.loads(prompt.split("# 原报告\n")[1].split("\n\n# 资料")[0])
    assert indexed["risks"]["risks[2]"] == BODY["risks"][2]
    assert indexed["business"]["business[0]"] == BODY["business"][0]
    assert RULES in prompt and output_schema(MATERIAL) not in prompt and PATCH_SCHEMA in prompt
    assert "输出且只输出一个 JSON 对象" not in prompt
    assert "不得用新风险替换另一项重大风险" in prompt
    full = repair_prompt(MATERIAL, None, [])
    assert "请修正后输出完整的新报告" in full and PATCH_SCHEMA not in full
    assert RULES in full and output_schema(MATERIAL) in full


class CapturedReplies:
    """Offline replay of captured report text and protocol mutations; no provider or verdict simulation."""

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def ask(self, step, prompt):
        self.calls.append((step, prompt))
        return next(self.responses)


@pytest.mark.parametrize("original", [BODY, NORMAL, BODY | {"risks": ITEM}])
@pytest.mark.parametrize("patch", ["not JSON", json.dumps({"ops": [replace("risks[999]")]}), json.dumps(BODY)])
def test_invalid_patch_rewrites_in_the_same_repair_call(original, patch, caplog):
    before = deepcopy(original)
    runner = CapturedReplies(patch, "```json\n" + json.dumps(NORMAL) + "\n```")
    result = repair(runner, MATERIAL, original, REVIEW["problems"])
    assert result == NORMAL and original == before
    assert runner.calls == [
        ("repair", repair_prompt(MATERIAL, original, REVIEW["problems"])),
        ("repair", repair_prompt(MATERIAL, None, REVIEW["problems"])),
    ]
    assert "请修正后输出完整的新报告" in runner.calls[1][1]
    assert output_schema(MATERIAL) in runner.calls[1][1] and PATCH_SCHEMA not in runner.calls[1][1]
    assert len(caplog.records) == 1 and caplog.records[0].levelname == "WARNING"
    assert "Invalid repair patch; falling back to full rewrite:" in caplog.text


@pytest.mark.parametrize("prefix,suffix", [("```json\n", "\n```"), ("修正如下：\n", "\n以上为补丁。")])
def test_wrapped_patch_uses_one_call_and_still_rejects_invalid_operations(prefix, suffix):
    response = prefix + json.dumps({"summary": NORMAL["summary"]}) + suffix
    runner = CapturedReplies(response)
    assert repair(runner, MATERIAL, BODY, []) == BODY | {"summary": NORMAL["summary"]}
    assert len(runner.calls) == 1
    with pytest.raises(RepairFormatError, match="越界"):
        repair_result(BODY, prefix + json.dumps({"ops": [replace("risks[999]")]}) + suffix, [], MATERIAL)


@pytest.mark.parametrize("section,item", [("answer", NORMAL["answer"][0]), ("methods", METHODS[0]),
                                         ("checkpoints", NORMAL["checkpoints"][0])])
@pytest.mark.parametrize("present", [False, True])
def test_add_treats_missing_or_null_sections_as_empty(section, item, present):
    draft = deepcopy(BODY)
    draft.pop(section, None)
    if present:
        draft[section] = None
    patch = {"ops": [{"op": "add", "where": section, "value": item}]}
    assert merge_repair_patch(draft, patch, [], MATERIAL)[section] == [item]


@pytest.mark.parametrize("section,item", [("answer", NORMAL["answer"][0]), ("methods", METHODS[0]),
                                         ("checkpoints", NORMAL["checkpoints"][0])])
@pytest.mark.parametrize("required", [False, True])
def test_empty_section_is_removed_only_when_optional(section, item, required):
    material = deepcopy(MATERIAL)
    if section == "answer" and required:
        material.extra["question"] = METHOD_REQUEST["question"]
    if (section == "methods" and required) or (section == "checkpoints" and not required):
        material.extra["feedback"] = METHOD_REQUEST["feedback"]
    merged = merge_repair_patch(BODY | {section: [item]},
                                {"ops": [{"op": "delete", "where": f"{section}[0]"}]}, [], material)
    assert (section in merged) == required
    issues = [p for p in check(merged, material) if p["where"] == section]
    assert bool(issues) == (required and section != "methods")


def test_feedback_missing_methods_can_be_repaired_to_empty_without_inventing_a_method():
    material = deepcopy(MATERIAL)
    material.extra = deepcopy(METHOD_REQUEST)
    draft = deepcopy(BODY)
    draft.pop("methods", None)
    assert any(p["where"] == "methods" for p in check(draft, material))
    merged = merge_repair_patch(draft, {}, [], material)
    assert merged["methods"] == []
    assert not any(p["where"] == "methods" for p in check(merged, material))


def test_content_shape_errors_are_left_to_check_after_safe_merge():
    patch = {"ops": [replace(value={"text": ITEM["text"]})], "verdict": None, "summary": [], "gaps": [ITEM]}
    merged = merge_repair_patch(BODY, patch, [], MATERIAL)
    assert {"verdict", "summary", "gaps", "risks[2]"} <= {p["where"] for p in check(merged, MATERIAL)}


def test_unmentioned_item_mutation_invalidates_the_bound_review():
    merged = merge_repair_patch(BODY, {"ops": [replace()]}, [], MATERIAL)
    assert serialized(merged["business"]) == serialized(BODY["business"])
    response = bound(merged)
    ledger = ReviewLedger()
    assert ledger.accept(response, merged, {}, BODY["table"], digest(BODY)) == []
    tampered = deepcopy(merged)
    tampered["business"][0] = deepcopy(BODY["business"][1])
    assert digest(tampered) != digest(merged)
    with pytest.raises(ReviewFormatError, match="绑定无效"):
        ledger.accept(response, tampered, {}, BODY["table"], digest(BODY))


@pytest.mark.parametrize("ops", [
    [replace("risks[0]", BODY["risks"][1])],
    [{"op": "delete", "where": "risks[0]"}, {"op": "add", "where": "risks", "value": BODY["risks"][1]}],
])
def test_risk_replacement_or_delete_plus_add_cannot_bypass_old_risk_decisions(ops):
    ledger = pending_ledger()
    ledger.protect_risks(BODY)
    before = ledger.snapshot()
    draft = merge_repair_patch(BODY, {"ops": ops}, ledger.pending(), MATERIAL)
    changes = ledger.risk_changes(draft)
    assert list(changes.values()) == [BODY["risks"][0]]
    keep = {"id": ledger.pending()[0]["id"], "status": "unresolved", "reason": finding()["problem"]}
    with pytest.raises(ReviewFormatError, match="复核决定 ID"):
        ledger.accept(bound(draft, resolutions=[keep]), draft, {}, BODY["table"], digest(BODY))
    assert ledger.snapshot() == before
    # A purported independent decision still needs valid evidence and a new target.
    risk_id = next(iter(changes))
    decision = {"id": risk_id, "status": "retained", "reason": ITEM["text"],
                "targets": ["risks[999]"], "evidence": finding()["evidence"]}
    with pytest.raises(ReviewFormatError, match="新版条目去向"):
        ledger.accept(bound(draft, resolutions=[keep], risk_resolutions=[decision]), draft, {}, BODY["table"], digest(BODY))
    assert ledger.snapshot() == before


def rebuttal(ledger):
    return {"id": ledger.pending()[0]["id"], "reason": finding()["problem"], "evidence": finding()["evidence"]}


@pytest.mark.parametrize("next_patch", [{}, {"rebuttals": []}, {"ops": [replace()]}])
def test_rebuttals_do_not_survive_the_next_merge_or_appear_in_original_prompt(next_patch):
    ledger = pending_ledger()
    draft = merge_repair_patch(BODY, {"rebuttals": [rebuttal(ledger)]}, ledger.pending(), MATERIAL)
    prompt = repair_prompt(MATERIAL, draft, ledger.pending())
    original = json.loads(prompt.split("# 原报告\n")[1].split("\n\n# 资料")[0])
    assert "repair_rebuttals" not in original
    merged = merge_repair_patch(draft, next_patch, ledger.pending(), MATERIAL)
    assert "repair_rebuttals" not in merged
    assert draft["repair_rebuttals"] == [rebuttal(ledger)]
    current = rebuttal(ledger) | {"reason": ITEM["text"]}
    assert merge_repair_patch(draft, {"rebuttals": [current]}, ledger.pending(), MATERIAL)["repair_rebuttals"] == [current]


@pytest.mark.parametrize("mutation", ["row", "period", "unread", "quote"])
def test_check_rejects_fabricated_rebuttal_citations_using_body_citation_rules(mutation):
    ledger = pending_ledger()
    material = deepcopy(MATERIAL)
    cite = next(c for item in REVIEW["report"]["risks"] for c in item["cites"]
                if c.get("source") == "D1" and "quote" in c)
    material.extra["sources"] = {"D1": {"document": BODY["sources"]["D1"]["document"],
                                       "pages": {cite["page"]: cite["quote"]}}}
    note = rebuttal(ledger) | {"evidence": [finding()["evidence"][0], cite]}
    good = merge_repair_patch(BODY, {"rebuttals": [note]}, ledger.pending(), material)
    assert not any(p["where"].startswith("repair_rebuttals") for p in check(good, material))
    bad_cite = {
        "row": finding()["evidence"][0] | {"ref": "T.missing"},
        "period": finding()["evidence"][0] | {"period": BODY["market_date"]},
        "unread": cite | {"source": "D9"},
        "quote": cite | {"quote": ITEM["text"]},
    }[mutation]
    bad = merge_repair_patch(good, {"rebuttals": [note | {"evidence": [bad_cite]}]}, ledger.pending(), material)
    problems = [p for p in check(bad, material) if p["where"] == "repair_rebuttals[0]"]
    body = BODY | {"risks": [{"text": ITEM["text"], "cites": [bad_cite]}]}
    body_errors = [p["problem"] for p in check(body, material) if p["where"] == "risks[0]"]
    assert problems and all(p["type"] == "citation_calculation" for p in problems)
    assert [p["problem"] for p in problems] == body_errors


def test_rebuttal_carries_reason_and_id_but_only_independent_review_can_dismiss():
    ledger = pending_ledger()
    before = ledger.snapshot()
    note = rebuttal(ledger)
    draft = merge_repair_patch(BODY, {"ops": [], "rebuttals": [note]}, ledger.pending(), MATERIAL)
    assert draft == BODY | {"repair_rebuttals": [note]}
    assert ledger.snapshot() == before
    assert ledger.request(draft, digest(BODY))["unresolved_issues"] == ledger.pending()
    with pytest.raises(ReviewFormatError, match="复核决定 ID"):
        ledger.accept(bound(draft), draft, {}, BODY["table"], digest(BODY))
    keep = {"id": note["id"], "status": "unresolved", "reason": note["reason"]}
    pending = ledger.accept(bound(draft, resolutions=[keep]), draft, {}, BODY["table"], digest(BODY))
    assert pending[0]["id"] == note["id"]
    # Exercise the protocol's evidence-backed dismissal, without endorsing this captured criticism.
    response = bound(draft, resolutions=[note | {"status": "dismissed"}])
    assert ledger.accept(response, draft, {}, BODY["table"], digest(BODY)) == []


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "status", "empty_reason", "empty_evidence", "bad_evidence", "not_array"])
def test_malformed_rebuttals_cannot_claim_resolution(mutation):
    ledger = pending_ledger()
    note = rebuttal(ledger)
    notes = [note]
    if mutation == "unknown": note["id"] = "unknown"
    if mutation == "duplicate": notes *= 2
    if mutation == "status": note["status"] = "resolved"
    if mutation == "empty_reason": note["reason"] = " "
    if mutation == "empty_evidence": note["evidence"] = []
    if mutation == "bad_evidence": note["evidence"] = [ITEM["text"]]
    if mutation == "not_array": notes = note
    with pytest.raises(RepairFormatError, match="rebuttals"):
        merge_repair_patch(BODY, {"ops": [], "rebuttals": notes}, ledger.pending(), MATERIAL)
