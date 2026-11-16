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


