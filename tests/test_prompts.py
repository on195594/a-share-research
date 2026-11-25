"""Render every research and comparison prompt with captured company data."""

import json

import pytest

from a_share_research import batches, drafts, evidence, model_profile, research, screening, valuation
from a_share_research.prompts import output_schema, render_prompt
from a_share_research.research_mcp import compose_prompt, explore_prompt, repair_prompt, review_prompt
from a_share_research.reviewing import ReviewLedger
from support.continuation import BODY
from support.valuation import DATA, assumptions, material


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


def _original_material_block(mat):
    rows = [
        f"[{r['id']}] {r['label']}（{r['unit']}）：" + ("；".join(f"{p} {v}" for p, v in r["values"].items()) or "缺失")
        for r in mat.table
    ]
    block = (
        f"公司：{mat.name}（{mat.code}）\n\n"
        f"## 财务与行情表（程序从 TuShare 生成；行情为 {mat.market_date} 收盘）\n" + "\n".join(rows)
        + "\n\n# 统一原文证据（默认 source=D0；read 为已读，listed 仅目录，不可引证；URL 由来源映射交付，无需正文手抄）\n"
        + json.dumps(evidence.projection(mat.source_registry(), catalog=True), ensure_ascii=False)
    )
    if mat.extra:
        pending = {k: mat.extra.get(k) for k in ("question", "context", "methods_used", "gaps")}
        for key in ("prior", "events"):
            if mat.extra.get(key):
                pending[key] = mat.extra[key]
        block += "\n\n# 本次问题与待核查上下文（不是事实来源）\n" + json.dumps(pending, ensure_ascii=False)
    return block + "\n\n# 程序计算依据\n" + json.dumps(
        [r for r in mat.table if r["id"].startswith("C.")], ensure_ascii=False)


@pytest.mark.parametrize("table", [
    [], BODY[37]["table"][:1], BODY[39]["table"], [r for r in BODY[37]["table"] if r["values"]], BODY[37]["table"],
], ids=["empty", "reported", "missing-values", "calculations", "calculations-and-missing-values"])
@pytest.mark.parametrize("extra_keys", [None, (), ("prior",), ("events",), ("prior", "events")],
                         ids=["no-extra", "question", "prior", "events", "prior-and-events"])
def test_material_block_matches_original_bytes(table, extra_keys):
    mat = material()
    mat.table = table
    if extra_keys is not None:
        mat.extra["question"] = BODY[39]["question"]
        if "prior" in extra_keys:
            mat.extra["prior"] = BODY[39]["prior"]
        if "events" in extra_keys:
            mat.extra["events"] = [r["raw"] for r in json.loads((DATA / "periodic-announcements.json").read_text())["items"]
                                   if r["code"] == mat.code[:6]]
    assert mat.prompt_block().encode() == _original_material_block(mat).encode()


def test_program_valuation_and_event_question_match_original_bytes():
    mat = material()
    assert drafts.program_valuation(None, mat) == ""
    report = {"valuation": assumptions()}
    summary = valuation.program_summary(report["valuation"], mat.table, mat.market_date)
    assert summary is not None
    original = ("\n# 程序估值测算（按本稿 valuation 计算，通过复核后随结论在报告页展示）\n"
                + json.dumps(summary, ensure_ascii=False)
                + "\nhurdle_price 是期望回报门槛价（等价格所等的价格）。这是程序输出，不是报告论断：报告以它作为参考价格时，"
                  "text 不重复数字也已给出价格；text 写出的价格与回报数字仍须引用 valuation 工具生成的 C.n。\n")
    assert drafts.program_valuation(report, mat).encode() == original.encode()
    assert research.EVENT_QUESTION == "公告扫描发现的 events 对研究判断、风险与检查点有什么影响？"


def test_application_sha256_includes_template_content(tmp_path, monkeypatch):
    package = tmp_path / "a_share_research"
    templates = package / "prompts"
    templates.mkdir(parents=True)
    monkeypatch.setattr(model_profile, "PROFILE_PATH", package / "model_profile.json")
    template = templates / "event_question.j2"
    template.write_text(research.EVENT_QUESTION, encoding="utf-8")
    # Each uncached calculation represents a fresh worker process.
    calculate = model_profile._application_sha256.__wrapped__
    original = calculate()
    template.write_text(research.EVENT_QUESTION + "\n", encoding="utf-8")
    assert calculate() != original


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
