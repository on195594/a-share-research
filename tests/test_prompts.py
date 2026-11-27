"""Render every research and comparison prompt with captured company data."""

import json
from copy import deepcopy

from a_share_research import batches, drafts, model_profile, research, research_mcp, screening, valuation
from a_share_research.prompts import output_schema
from a_share_research.research_tools import method_cards
from a_share_research.reviewing import ReviewLedger
from support.continuation import BODY
from support.valuation import DATA, assumptions, material


# The session computes these inputs once per receipt and passes them to every stage prompt.
def explore_prompt(mat):
    return research_mcp.explore_prompt(mat, material_block=mat.prompt_block())


def compose_prompt(mat):
    return research_mcp.compose_prompt(mat, material_block=mat.prompt_block())


def review_prompt(mat, draft, binding):
    return research_mcp.review_prompt(mat, draft, binding, material_block=mat.prompt_block(),
                                      valuation=drafts.program_valuation(draft, mat))


def repair_prompt(mat, draft, problems, *, acquire):
    return research_mcp.repair_prompt(draft, problems, acquire=acquire, material_block=mat.prompt_block(),
                                      valuation=drafts.program_valuation(draft, mat))


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
    block = mat.prompt_block()
    explore = explore_prompt(mat)
    compose = compose_prompt(mat)
    for stage in (explore, compose, review_prompt(mat, draft, binding),
                  repair_prompt(mat, draft, problems, acquire=True)):
        # Stage instructions stay in the cacheable system prompt; this run's material only enters the user prompt.
        assert stage.keys() == {"system_prompt", "prompt"} and stage["system_prompt"]
        assert stage["prompt"].endswith(block) and block not in stage["system_prompt"]
    # The captured catalog has a filing after the starting annual report; only the user prompt names it.
    assert explore["prompt"].startswith("起始年报之后已有更新定期报告 ") and "起始年报之后" not in explore["system_prompt"]
    for stage in (explore, compose):
        assert output_schema(mat) in stage["prompt"] and output_schema(mat) not in stage["system_prompt"]

    candidates = [{"code": mat.code, "name": mat.name, "report": draft}]
    for comparison, tool in ((screening.selection_prompt, "submit_selection"),
                             (screening.ranking_prompt, "submit_ranking")):
        stage = comparison(candidates, batches.RULE)
        assert stage.keys() == {"system_prompt", "prompt"}
        assert f"请调用 {tool} 提交" in stage["system_prompt"] and mat.code not in stage["system_prompt"]
        assert tool not in stage["prompt"]
        assert json.loads(stage["prompt"].split("# 候选", 1)[1].split("\n", 1)[1]) == candidates
    assert screening.QUESTION


def test_research_system_prompts_are_invariant_to_run_inputs():
    mat = material()
    draft = BODY[39]
    mat.extra["catalog"] = {key: item["document"] for key, item in draft["sources"].items()}
    feedback = json.loads((DATA / "research-methods.json").read_text())["reports"][1]["request"]["feedback"]
    events = [row["raw"] for row in json.loads((DATA / "periodic-announcements.json").read_text())["items"]
              if row["code"] == mat.code[:6]]
    cards = method_cards(DATA / "method-cards")
    problems = json.loads((DATA / "603606-review.json").read_text())["problems"]

    def stages(case):
        binding = ReviewLedger().request(draft, case.snapshot()["sha256"])
        return (explore_prompt(case), compose_prompt(case), review_prompt(case, draft, binding),
                repair_prompt(case, draft, problems, acquire=True))

    baseline = stages(mat)
    for field, value in (("question", draft["question"]), ("feedback", feedback), ("prior", draft["prior"]),
                         ("events", events), ("method_catalog", cards)):
        case = deepcopy(mat)
        assert value
        case.extra[field] = value
        rendered = stages(case)
        assert [p["system_prompt"] for p in rendered] == [p["system_prompt"] for p in baseline], field
        assert rendered[0]["prompt"] != baseline[0]["prompt"], field
        if field == "method_catalog":
            assert rendered[0]["prompt"].startswith(f"本次可检索的分析方法卡：{cards[0]['title']}。\n")
        else:
            assert output_schema(case) != output_schema(mat), field

    # Use the same captured table with the latest year removed to exercise the dynamic valuation years.
    earlier = deepcopy(mat)
    latest_year = str(valuation.years(mat.table)[0] - 1)
    for row in earlier.table:
        row["values"] = {period: value for period, value in row["values"].items() if period[:4] < latest_year}
    assert valuation.years(earlier.table) != valuation.years(mat.table)
    assert [p["system_prompt"] for p in stages(earlier)] == [p["system_prompt"] for p in baseline]
    assert output_schema(earlier) != output_schema(mat)

    replay = repair_prompt(mat, draft, problems, acquire=False)
    assert replay["system_prompt"] == baseline[3]["system_prompt"]
    assert replay["prompt"].startswith("# 问题\n")
    assert baseline[3]["prompt"].startswith("# 补证与引用\n")
    assert baseline[3]["prompt"].split("\n\n# 问题", 1)[1] == replay["prompt"].removeprefix("# 问题")


def test_comparison_system_prompts_are_invariant_to_candidates_and_rules():
    candidates = [{"code": draft["code"], "name": draft["name"], "report": draft} for draft in (BODY[39], BODY[25])]
    small = batches.small_rule(batches.NEW_RULE)
    for compare in (screening.selection_prompt, screening.ranking_prompt):
        baseline = compare(candidates[:1], batches.RULE)
        changed_candidates = compare(candidates, batches.RULE)
        assert baseline["system_prompt"] == changed_candidates["system_prompt"] == compare(candidates, small)["system_prompt"]
        assert baseline["prompt"] != changed_candidates["prompt"]
    assert "最多选 3 家，同一行业最多 1 家" in screening.selection_prompt(candidates, small)["prompt"]
    changed_benchmark = small | {"benchmarks": {"H00852.CSI": small["benchmarks"]["H00852.CSI"]}}
    rank = screening.ranking_prompt(candidates, changed_benchmark)
    assert rank["system_prompt"] == screening.ranking_prompt(candidates, batches.RULE)["system_prompt"]
    assert "2 家，相对中证1000全收益" in rank["prompt"]


def test_material_block_boundaries():
    mat = material()
    mat.table = []
    empty = mat.prompt_block()
    assert empty.startswith(f"公司：{mat.name}（{mat.code}）\n\n## 财务与行情表（程序从 TuShare 生成；行情为 {mat.market_date} 收盘）\n")
    assert "本次问题与待核查上下文" not in empty
    assert empty.endswith("\n\n# 程序计算依据\n[]")

    mat.table = BODY[37]["table"]
    mat.extra.update(question=BODY[39]["question"], prior=BODY[39]["prior"])
    block = mat.prompt_block()
    missing = next(r for r in mat.table if not r["values"])
    assert f"[{missing['id']}] {missing['label']}（{missing['unit']}）：缺失\n" in block
    head, calculations = block.split("\n\n# 程序计算依据\n")
    pending = json.loads(head.split("\n\n# 本次问题与待核查上下文（不是事实来源）\n")[1])
    assert pending == {"question": BODY[39]["question"], "context": None, "methods_used": None, "gaps": None,
                       "prior": BODY[39]["prior"]}
    assert json.loads(calculations) == [r for r in mat.table if r["id"].startswith("C.")] != []


def test_review_prompt_shows_recorded_announcement_searches_only():
    mat = material()
    draft = BODY[39]
    mat.extra.update(catalog={key: item["document"] for key, item in draft["sources"].items()},
                     question=draft["question"], gaps=[])
    binding = ReviewLedger().request(draft, mat.snapshot()["sha256"])
    heading = "\n\n# 本次公告目录检索记录（程序记录的查询范围，不是事实来源，不可引证）\n"

    # Missing or empty coverage: nothing claims a search happened.
    for extra in ({}, {"announcement_searches": []}):
        case = deepcopy(mat)
        case.extra.update(extra)
        prompt = review_prompt(case, draft, binding)["prompt"]
        assert "公告目录检索" not in prompt and prompt.endswith(case.prompt_block())

    # Captured 603606 directory searches, plus one explicitly truncated variant.
    searches = deepcopy(draft["announcement_searches"])
    truncated = searches[1] | {"query": "更正", "truncated": True, "scanned": 150}  # Marked minimal variant.
    mat.extra["announcement_searches"] = [*searches, truncated]
    prompts = review_prompt(mat, draft, binding)
    prompt = prompts["prompt"]
    section = prompt.split(mat.prompt_block(), 1)[1]
    lines = section.removeprefix(heading).split("\n")
    assert section.startswith(heading) and lines[:3] == [
        "- 603606：2026-04-30 至 2026-10-27，全部标题，扫描目录 7 条，未截断",
        "- 603606：2026-04-30 至 2026-10-27，标题关键词「中标」，扫描目录 2 条，未截断",
        "- 603606：2026-04-30 至 2026-10-27，标题关键词「更正」，扫描目录 150 条，超出扫描上限，结果被截断",
    ]
    assert "样本" not in prompt + prompts["system_prompt"] and "公告目录检索" not in prompts["system_prompt"]
    assert mat.extra["announcement_searches"] == [*searches, truncated]  # Rendering does not mutate material.

    # Other stages keep their inputs; the record only enters review.
    problems = json.loads((DATA / "603606-review.json").read_text())["problems"]
    for other in (explore_prompt(mat), compose_prompt(mat), repair_prompt(mat, draft, problems, acquire=True)):
        assert "公告目录检索记录" not in other["system_prompt"] + other["prompt"]


def test_program_valuation_and_event_question():
    mat = material()
    assert drafts.program_valuation(None, mat) == ""
    report = {"valuation": assumptions()}
    text = drafts.program_valuation(report, mat)
    summary = valuation.program_summary(report["valuation"], mat.table, mat.market_date)
    assert text.startswith("\n# 程序估值测算（按本稿 valuation 计算，通过复核后随结论在报告页展示）\n"
                           + json.dumps(summary, ensure_ascii=False) + "\nhurdle_price 是期望回报门槛价")
    assert text.endswith("仍须引用 valuation 工具生成的 C.n。\n")
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

    # Every real template is hashed by sorted relative path; comment stripping is pinned by the cases below.
    expected_real = hashlib.sha256()
    for path in sorted(prompts._PROMPTS.rglob("*")):
        if path.is_file():
            relative = path.relative_to(prompts._PROMPTS).as_posix()
            text = path.read_text(encoding="utf-8")
            expected_real.update(relative.encode() + b"\0")
            expected_real.update(prompts._strip_jinja_comments(text, relative).encode() + b"\0")
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
