#!/usr/bin/env python3
"""Minimal QA discovery/execution smoke with only the standard library."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide"
SKILL = ROOT / "a-stock-qa" / "SKILL.md"
RUBRIC = (
    ROOT / "a-stock-qa" / "references" / "rubrics" / "a-stock-research.md"
)


def aggregate_verdict(fail_levels: tuple[str, ...]) -> str:
    if any(level in {"Critical", "Important"} for level in fail_levels):
        return "NON_COMPLIANT"
    if fail_levels:
        return "PARTIAL"
    return "COMPLIANT"


def verdict(report: str, skill_type: str, fail_levels: tuple[str, ...] = ()) -> str:
    if skill_type != "a-stock-research" or not RUBRIC.is_file():
        return "SKIP"
    required = ("数据", "评分", "报告")
    if not all(token in report for token in required):
        return "INVALID_RUN"
    return aggregate_verdict(fail_levels)


def main() -> int:
    assert SKILL.is_file() and RUBRIC.is_file()
    assert "a_share_research.policy.v0" not in SKILL.read_text(encoding="utf-8")
    assert (
        verdict("完整研究报告：数据完整，评分已记录", "a-stock-research") == "COMPLIANT"
    )
    assert verdict("修订摘要", "a-stock-research") == "INVALID_RUN"
    assert (
        verdict(
            "完整研究报告：数据完整，评分已记录", "a-stock-research", ("Important",)
        )
        == "NON_COMPLIANT"
    )
    assert (
        verdict("完整研究报告：数据完整，评分已记录", "a-stock-research", ("Minor",))
        == "PARTIAL"
    )
    assert verdict("anything", "a-stock-monitor") == "SKIP"
    assert not re.search(
        r"import (requests|pandas|a_share_research.policy.v0)",
        SKILL.read_text(encoding="utf-8"),
    )
    print("qa standalone smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
