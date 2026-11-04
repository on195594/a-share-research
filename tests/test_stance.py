"""Research stance (V4): structure, the program-owned wait price, and frozen scoring inputs.

Tables are the captured 603606 report 37 and 600519 annual-report material; scenario inputs come from
support.valuation (real report_rc estimates used as test-only hypotheses).
"""
from copy import deepcopy

import pytest

from a_share_research import checking, prompts, research, reviewing, stance, valuation
from a_share_research.material import Material
from a_share_research.sources import Document
from support.reports import MATERIAL, report
from support.valuation import BODY, assumptions, with_probabilities


def material(table=None):
    return Material(BODY['code'], BODY['name'], Document(**BODY['document']), {}, {},
                    deepcopy(table or BODY['table']), BODY['market_date'])


EVIDENCE = {'action': '等证据', 'text': '上半年经营现金流转负，先看下一期是否转正。',
            'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}],
            'trigger': {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>=', 'value': 0}}


def stance_problems(value, table=None, valuation_value=None):
    return stance.problems(value, table or BODY['table'], valuation_value or assumptions(), BODY['market_date'])


def test_fixture_stance_passes_and_schema_names_next_period():
    assert not [p for p in checking.check(report(), MATERIAL) if p['where'] == 'stance']
    assert stance_problems(EVIDENCE) == []
    schema = prompts.output_schema(material())
    assert '"stance"' in schema and '"period": "2026-09-30"' in schema and 'probability' in schema


def test_missing_stance_required_except_feedback():
    draft = report()
    del draft['stance']
    assert any(p['where'] == 'stance' for p in checking.check(draft, MATERIAL))
    mat = deepcopy(MATERIAL)
    mat.extra['feedback'] = {'location': 'summary', 'text': '核查'}
    assert not any(p['where'] == 'stance' for p in checking.check(draft, mat))


@pytest.mark.parametrize('change,needle', [
    ({'action': '持有'}, 'stance.action'),
    ({'trigger': None}, 'trigger'),
    ({'trigger': {'metric': 'T.nope', 'period': '2026-09-30', 'op': '>=', 'value': 0}}, 'T.* 行标识'),
    ({'trigger': {'metric': 'T.op_cash', 'period': '2026-12-31', 'op': '>=', 'value': 0}}, '2026-09-30'),
    ({'trigger': {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>', 'value': 0}}, '>= 或 <='),
    ({'trigger': {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>=', 'value': '0'}}, '有限数字'),
    ({'trigger': {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>=', 'value': True}}, '有限数字'),
])
def test_evidence_trigger_contract(change, needle):
    assert any(needle in p for p in stance_problems(EVIDENCE | change))


def test_wait_price_is_program_owned_and_must_be_coherent():
    wait = {'action': '等价格', 'text': '期望回报尚未达到门槛。', 'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}]}
    # Captured close 36.03 is above the v2 hurdle price (~30.50 at 25/50/25): a coherent price wait.
    price, problem = stance.wait_price(assumptions(), BODY['table'], BODY['market_date'])
    assert problem is None and price == pytest.approx(30.502715198880022)
    assert stance_problems(wait) == []
    assert any('不要填写' in p for p in stance_problems(wait | {'price': 30}))
    assert any('未定价' in p for p in stance_problems(wait, valuation_value=report()['valuation']))
    # Mutation: the same close against a 30% smaller market value clears the hurdle, so waiting is incoherent.
    cheap = deepcopy(BODY['table'])
    row = next(r for r in cheap if r['id'] == 'M.total_mv')
    row['values'] = {k: str(round(float(v) * .7, 2)) for k, v in row['values'].items()}
    assert any('等价格不成立' in p for p in stance_problems(wait, table=cheap))
    # The hurdle follows the probabilities: weight on the richest case (中性 here) raises it.
    low, _ = stance.wait_price(with_probabilities(assumptions(), (90, 5, 5)), BODY['table'], BODY['market_date'])
    high, _ = stance.wait_price(with_probabilities(assumptions(), (5, 90, 5)), BODY['table'], BODY['market_date'])
    assert low is not None and high is not None and high > price > low


def test_stance_text_figures_still_need_sources():
    draft = report()
    draft['stance']['text'] = '经营现金流需恢复到888.88亿元以上。'
    assert any(p['where'] == 'stance' and '888.88' in p['problem'] for p in checking.check(draft, MATERIAL))
    draft['stance']['cites'] = []
    assert any(p['where'] == 'stance' and '没有出处' in p['problem'] for p in checking.check(draft, MATERIAL))


def test_stance_calc_freezes_scoring_inputs():
    calc = valuation.compute(assumptions(), BODY['table'], BODY['market_date'])
    frozen = research.stance_calc({'action': '等价格', 'text': 't', 'cites': []}, calc, '2026-10-26')
    assert frozen == {'action': '等价格', 'price': calc['v2']['support_below'], 'valid_until': '2027-04-26'}
    assert research.stance_calc({'action': '等价格'}, calc, '2026-08-31')['valid_until'] == '2027-02-28'
    assert research.stance_calc(EVIDENCE, calc, '2026-10-26') == {'action': '等证据', 'trigger': EVIDENCE['trigger']}
    assert research.stance_calc({'action': '现在分批'}, calc, '2026-10-26') == {'action': '现在分批'}


def test_trigger_met_against_disclosed_text_values():
    up = {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>=', 'value': 0}
    assert stance.met(up, '0.00') is True and stance.met(up, '-1.57') is False
    assert stance.met(up | {'op': '<='}, '-1.57') is True
    assert stance.met(up, None) is None and stance.met(up, 'n/a') is None and stance.met(up, True) is None


def test_patch_publish_review_target_and_describe():
    merged = research.merge_repair_patch(report(), {'stance': deepcopy(EVIDENCE)}, [], MATERIAL)
    assert merged['stance'] == EVIDENCE and research.published(merged)['stance'] == EVIDENCE
    assert reviewing.target_exists('stance', merged)
    assert stance.describe(EVIDENCE, BODY['table']).startswith('等证据：2026-09-30 ')
    assert '≥ 0' in stance.describe(EVIDENCE, BODY['table'])
