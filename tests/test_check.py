"""Mechanical report checks, on excerpts and figures from 贵州茅台2025年年度报告 and TuShare."""

import json
from pathlib import Path

import pytest

from a_share_research.research import Material, ResearchError, check
from a_share_research.sources import Document
from support.reports import MATERIAL, SALES, report


def test_number_missing_from_cited_source_is_flagged():
    bad = report(risks=[{"text": "经营现金流降至600.00亿元。", "cites": [{"ref": "T.op_cash"}]}])
    assert [p["where"] for p in check(bad, MATERIAL)] == ["risks[0]"]


def test_quote_not_in_cited_page_is_flagged():
    bad = report(business=[{"text": "公司加大了渠道投入。", "cites": [{"page": 9, "quote": "本期大幅增加渠道建设投入和经销商返利"}]}])
    assert [p["where"] for p in check(bad, MATERIAL)] == ["business[0]"]


def test_years_and_months_are_not_checked_as_figures():
    ok = report(business=[{"text": "截至2025-12-31，600519在2025年12月销售费用增加主要因为广告宣传。", "cites": [SALES]}])
    assert check(ok, MATERIAL) == []
    # A code is skipped as an identifier, never accepted as a figure at some scale (600519 × 1e-4 ≈ 60.05).
    bad = report(business=[{"text": "600519销售费用率约60.05%。", "cites": [SALES]}])
    assert [p["where"] for p in check(bad, MATERIAL)] == ["business[0]"]


def test_cited_calculation_supports_its_verified_inputs_and_input_periods():
    from a_share_research.research_tools import calculate
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    calc = calculate(material.table, "现金流差额", "difference",
                     {"ref": "T.op_cash", "period": "2025-12-31"},
                     {"ref": "T.op_cash", "period": "2024-12-31"})
    material.table.append(calc)
    ok = report(financial=[{"text": f"经营现金流由924.64亿元变为615.22亿元，差额为{calc['values']['result']}亿元。",
                            "cites": [{"ref": "C.1", "period": "2025-12-31"}]}])
    assert check(ok, material) == []


def test_reference_ids_in_text_are_flagged():
    bad = report(summary="需要谨慎：营业总收入1720.54亿元（出处：T.revenue）。")
    assert [p["where"] for p in check(bad, MATERIAL)] == ["summary"]


def test_snapshot_preserves_prompt_and_checks():
    snapshot = json.loads(json.dumps(MATERIAL.snapshot(), ensure_ascii=False))
    restored = Material.from_snapshot(snapshot)
    assert restored.prompt_block() == MATERIAL.prompt_block()
    assert restored.raw_pages == MATERIAL.raw_pages
    # Check unit conversion and wrapped quotes on both original and replayed material.
    assert check(report(), restored) == check(report(), MATERIAL) == []
    snapshot["data"]["table"].pop()
    with pytest.raises(ResearchError, match="哈希"):
        Material.from_snapshot(snapshot)


@pytest.mark.parametrize("include_supplements", [False, True])
def test_material_prompt_preserves_captured_base_disclosure_identity(include_supplements):
    # Source-metadata regression on production report 37, not a replay of failed report 43.
    records = json.loads((Path(__file__).parent / "data/continuation-records.json").read_text())["records"]
    body = next(r["report"]["body"] for r in records if r["report"]["id"] == 37)
    doc = Document(**body["document"])
    supplements = {key: {"document": value["document"], "pages": {}}
                   for key, value in body["sources"].items()}
    # These captures contain source identities, not the complete page texts. Do not invent pages.
    material = Material(doc.code, doc.name, doc, {}, {}, body["table"], body["market_date"],
                        {"sources": supplements} if include_supplements else {})
    before = material.snapshot()
    prompt = material.prompt_block()
    identity = material.source_registry()["D0"]
    assert identity["document"] == body["document"] and identity["read_pages"] == []
    assert json.dumps(body["document"], ensure_ascii=False) in prompt
    assert doc.url and doc.sha256 and doc.announced
    if include_supplements:
        for key, value in supplements.items():
            assert material.source_registry()[key]["document"] == value["document"]
            assert json.dumps(value["document"], ensure_ascii=False) in prompt
    assert material.snapshot() == before
    restored = Material.from_snapshot(json.loads(json.dumps(before)))
    assert restored.prompt_block() == prompt and restored.snapshot()["sha256"] == before["sha256"]


def test_material_prompt_keeps_unknown_url_and_actual_read_pages():
    # Normal Moutai excerpt fixture has no captured URL; exposing metadata must not invent one.
    prompt = MATERIAL.prompt_block()
    identity = MATERIAL.source_registry()["D0"]
    assert identity["document"] == MATERIAL.doc.info()
    assert identity["document"]["url"] == ""
    assert identity["read_pages"] == sorted(MATERIAL.pages)
    for page, text in MATERIAL.pages.items():
        assert json.dumps(text, ensure_ascii=False) in prompt
    assert check(report(), MATERIAL) == []


def test_checkpoint_thresholds_must_come_from_cited_figures():
    bad = report(checkpoints=[{"text": "下一期经营现金流若低于700.00亿元，说明现金转化未恢复。",
                               "cites": [{"ref": "T.op_cash", "period": "2025-12-31"}]}])
    assert [p["where"] for p in check(bad, MATERIAL)] == ["checkpoints[0]"]
    # Reports written before checkpoints existed fail the current contract on replay.
    legacy = report()
    del legacy["checkpoints"]
    assert [p["where"] for p in check(legacy, MATERIAL)] == ["checkpoints"]


def test_feedback_reports_do_not_require_checkpoints():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra = {"feedback": {"location": "risks[0]", "text": "核查现金流口径"}}
    result = report(answer=[{"text": "经营现金流从924.64亿元降至615.22亿元。", "cites": [{"ref": "T.op_cash"}]}],
                    methods=[])
    del result["checkpoints"]
    assert check(result, material) == []


def test_question_thresholds_and_report_ids_are_terms_not_claims():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["question"] = "相对报告37，经营现金流是否仍高于600亿元？"
    ok = report(answer=[{"text": "相对报告37，经营现金流615.22亿元，仍高于600亿元。", "cites": [{"ref": "T.op_cash"}]}])
    assert check(ok, material) == []
    # Mutation: a figure the question does not set as a threshold still needs a source.
    bad = report(answer=[{"text": "相对报告37，经营现金流580.00亿元，低于600亿元。", "cites": [{"ref": "T.op_cash"}]}])
    assert [p["where"] for p in check(bad, material)] == ["answer[0]"]
    # Mutation: a report id is dropped only as an identifier; the same digits as a figure are still checked.
    bad = report(answer=[{"text": "经营现金流占营收约37%。", "cites": [{"ref": "T.op_cash"}]}])
    assert [p["where"] for p in check(bad, material)] == ["answer[0]"]


def test_facts_supplied_in_the_question_still_need_sources():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["question"] = "经营现金流由924.64亿元降至600.50亿元的原因是什么？"
    bad = report(answer=[{"text": "经营现金流降至600.50亿元。", "cites": [{"ref": "T.op_cash"}]}])
    assert [p["where"] for p in check(bad, material)] == ["answer[0]"]


@pytest.mark.parametrize("section", ["answer", "summary"])
@pytest.mark.parametrize("text", ["经营现金流600亿元。", "经营现金流600亿元，未高于600亿元。"])
def test_repeating_a_question_threshold_does_not_exempt_the_same_factual_amount(section, text):
    # Boundary mutation of the real Moutai amounts: 600 is not the recorded 615.22.
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["question"] = "相对报告37，经营现金流是否仍高于600亿元？"
    draft = report(answer=[{"text": "经营现金流615.22亿元。", "cites": [{"ref": "T.op_cash"}]}])
    draft[section] = (draft["verdict"] + "：" + text if section == "summary"
                      else [{"text": text, "cites": [{"ref": "T.op_cash"}]}])
    problems = check(draft, material)
    assert len(problems) == 1 and "数字 600 " in problems[0]["problem"]
    assert problems[0]["where"] == ("summary" if section == "summary" else "answer[0]")


@pytest.mark.parametrize("threshold", ["600万元", "600%", "-600亿元", "600.1亿元"])
def test_threshold_exemption_keeps_unit_sign_and_exact_value(threshold):
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["question"] = "经营现金流是否高于600亿元？"
    draft = report(answer=[{"text": f"经营现金流低于{threshold}。", "cites": [{"ref": "T.op_cash"}]}])
    assert [p["where"] for p in check(draft, material)] == ["answer[0]"]


def test_explicit_150_threshold_and_report_37_remain_readable_without_invented_sources():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["question"] = "相对报告37，经营现金流是否高于150亿元？"
    draft = report(answer=[{"text": "相对报告37，经营现金流615.22亿元，高于150.00亿元。",
                            "cites": [{"ref": "T.op_cash"}]}])
    assert check(draft, material) == []


@pytest.mark.parametrize("prefix", ["大于", "小于", "不大于", "不小于", "高于", "低于", "不高于", "不低于"])
def test_chinese_comparative_threshold_prefixes_are_exempt(prefix):
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["question"] = f"相对报告37，经营现金流是否{prefix}150亿元？"
    draft = report(answer=[{"text": f"相对报告37，经营现金流615.22亿元，{prefix}150.00亿元。",
                            "cites": [{"ref": "T.op_cash"}]}])
    assert check(draft, material) == []


def test_unrecognized_threshold_units_are_not_silently_treated_as_unitless():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["question"] = "经营现金流是否高于600美元？"
    draft = report(answer=[{"text": "经营现金流低于600港元。", "cites": [{"ref": "T.op_cash"}]}])
    assert [p["where"] for p in check(draft, material)] == ["answer[0]"]
