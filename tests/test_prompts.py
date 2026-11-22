"""Render every research and comparison prompt with captured company data."""

import json

from a_share_research import batches, screening
from a_share_research.prompts import output_schema, render_prompt
from a_share_research.research_mcp import compose_prompt, explore_prompt, repair_prompt, review_prompt
from a_share_research.reviewing import ReviewLedger
from support.continuation import BODY
from support.valuation import DATA, material


def test_render_prompts_from_captured_material():
    mat = material()
    draft = BODY[39]
    mat.extra.update(
        catalog={key: item["document"] for key, item in draft["sources"].items()},
        question=draft["question"], prior=draft["prior"],
        events=[row["raw"] for row in json.loads((DATA / "periodic-announcements.json").read_text())["items"]
                if row["code"] == mat.code[:6]],
    )
    binding = ReviewLedger().request(draft, mat.snapshot()["sha256"])
    problems = json.loads((DATA / "603606-review.json").read_text())["problems"]
    assert explore_prompt(mat)
    assert compose_prompt(mat)
    assert review_prompt(mat, draft, binding)
    assert repair_prompt(mat, draft, problems, acquire=True)
    assert output_schema(mat)

    candidates = [{"code": mat.code, "name": mat.name, "report": draft}]
    assert screening.selection_prompt(candidates, batches.RULE)
    assert screening.ranking_prompt(candidates, batches.RULE)
    assert screening.QUESTION
    assert render_prompt("correction.j2", problems=json.dumps(problems, ensure_ascii=False),
                         correction=json.dumps(draft, ensure_ascii=False))
