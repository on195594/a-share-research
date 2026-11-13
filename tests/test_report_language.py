"""Prompt wiring on captured reports; these tests do not claim model writing quality."""
from copy import deepcopy
from test_repair import BODY, MATERIAL

from a_share_research import prompts, research


def test_writing_guidance_reaches_compose_and_repair_without_mutating_history():
    original = deepcopy(BODY)
    class Capture:
        def ask(self, stage, prompt):
            self.stage, self.prompt = stage, prompt
            return '{}'
    runner = Capture()
    research.compose(runner, MATERIAL)
    assert runner.stage == 'compose'
    assert prompts.RULES in runner.prompt
    assert prompts.output_schema(MATERIAL) in runner.prompt
    patch_prompt = research.repair_prompt(MATERIAL, BODY, [])
    assert prompts.RULES in patch_prompt
    assert research.PATCH_SCHEMA in patch_prompt
    assert '没有问题的条目保持原样' in patch_prompt
    assert BODY == original


def test_writing_guidance_preserves_evidence_and_necessary_uncertainty():
    # Desired prompt contract, not a word-frequency linter for reports.
    for requirement in ('先说当前判断或经营变化', '资料缺口写清缺哪项资料',
                        '逐项风险追踪字段照常保留', '保留实质反证',
                        '否定词用于需要明确排除的事实或结论', '不按词频或固定句数改写',
                        '保持原论断力度、数字、因果关系和适用范围',
                        '派生数字必须来自程序计算工具的记录',
                        '稻草人否定句尾'):
        assert requirement in prompts.RULES
    assert '不把“风险未解除/检查点未到期”当作主句' in prompts.SCHEMA
    assert '不写“这是研究假设、不是公司指引/分红承诺”之类模板免责' in prompts.VALUATION
    assert '不逐条重复 prior_status' in prompts.PRIOR
    assert '反复重申“风险未解除/检查点未到期”' in prompts.PRIOR
