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


def test_prompts_sha256_ignores_jinja_comments(tmp_path, monkeypatch):
    import hashlib
    from a_share_research import prompts

    expected_real = hashlib.sha256()
    for path in sorted(prompts._PROMPTS.rglob("*")):
        if path.is_file():
            expected_real.update(path.relative_to(prompts._PROMPTS).as_posix().encode() + b"\0")
            expected_real.update(path.read_bytes() + b"\0")
    assert prompts.prompts_sha256() == expected_real.hexdigest()

    template = tmp_path / "rule.j2"
    monkeypatch.setattr(prompts, "_PROMPTS", tmp_path)

    template.write_text("A  B\n", encoding="utf-8")
    plain_space = prompts.prompts_sha256()
    template.write_text("A {# c #} B\n", encoding="utf-8")
    assert prompts.prompts_sha256() == plain_space
    assert prompts._ENV.from_string("A {# c #} B\n").render() == "A  B\n"

    template.write_text("AB\n", encoding="utf-8")
    trimmed = prompts.prompts_sha256()
    template.write_text("A {#- c -#} B\n", encoding="utf-8")
    assert prompts.prompts_sha256() == trimmed
    assert prompts.prompts_sha256() != plain_space
    assert prompts._ENV.from_string("A {#- c -#} B\n").render() == "AB\n"

    template.write_text("{% raw %}{# lit #}{% endraw %}{{ '{# lit #}' }}\n", encoding="utf-8")
    raw_before = prompts.prompts_sha256()
    template.write_text("{% raw %}{# changed #}{% endraw %}{{ '{# lit #}' }}\n", encoding="utf-8")
    assert prompts.prompts_sha256() != raw_before
    template.write_text("{% raw %}{# lit #}{% endraw %}{{ '{# changed #}' }}\n", encoding="utf-8")
    assert prompts.prompts_sha256() != raw_before
