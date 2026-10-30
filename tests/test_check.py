"""Mechanical report checks, on excerpts and figures from 贵州茅台2025年年度报告 and TuShare."""

import json

import pytest

from a_share_research.research import Material, ResearchError, check
from support.reports import MATERIAL, SALES, report


def test_number_missing_from_cited_source_is_flagged():
    bad = report(risks=[{"text": "经营现金流降至600.00亿元。", "cites": [{"ref": "T.op_cash"}]}])
    assert [p["where"] for p in check(bad, MATERIAL)] == ["risks[0]"]


def test_quote_not_in_cited_page_is_flagged():
    bad = report(business=[{"text": "公司加大了渠道投入。", "cites": [{"page": 9, "quote": "本期大幅增加渠道建设投入和经销商返利"}]}])
    assert [p["where"] for p in check(bad, MATERIAL)] == ["business[0]"]


def test_years_and_months_are_not_checked_as_figures():
    ok = report(business=[{"text": "2025年12月销售费用增加主要因为广告宣传。", "cites": [SALES]}])
    assert check(ok, MATERIAL) == []


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
