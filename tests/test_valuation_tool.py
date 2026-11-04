"""valuation tool: citable program hurdle price, bound to the scenarios it priced.

Root case: 601899 run 121 (2026-11-04). The writer could not see the program hurdle price, the
reviewer flagged the unanswered "参考价格", and repair invented its own price line that contradicted
the program. Table: captured 603606 report 37; scenarios: support.valuation test-only hypotheses.
"""
from copy import deepcopy

import pytest

from a_share_research import checking, research, stance, valuation
from a_share_research.material import Material
from a_share_research.research_tools import ResearchTools, _fetches
from a_share_research.sources import Document
from support.valuation import BODY, assumptions, with_probabilities


def material():
    mat = Material(BODY['code'], BODY['name'], Document(**BODY['document']), {}, {},
                   deepcopy(BODY['table']), BODY['market_date'])
    mat.extra['question'] = '近期可以买入吗？参考价格是多少'
    return mat


def draft(rows, text):
    return {'verdict': '值得继续研究', 'summary': '值得继续研究：测试。', 'valuation': assumptions(),
            'answer': [{'text': text, 'cites': [{'ref': r['id']} for r in rows]}]}


def answer_problems(report, mat):
    return [p for p in checking.check(report, mat) if p['where'] == 'answer[0]']


def test_tool_rows_equal_program_wait_price_and_are_citable():
    mat = material()
    rows = ResearchTools(mat, None).run('valuation', {'valuation': assumptions()})
    price, _ = stance.wait_price(assumptions(), BODY['table'], BODY['market_date'])
    calc = valuation.compute(assumptions(), BODY['table'], BODY['market_date'])
    assert [r['id'] for r in rows] == ['C.4', 'C.5'] and rows == mat.table[-2:]
    assert float(rows[0]['exact_values']['result']) == pytest.approx(price) == pytest.approx(30.502715198880022)
    assert float(rows[1]['exact_values']['result']) == pytest.approx(calc['v2']['annual_return'] * 100)
    annual = rows[1]['values']['result']
    good = draft(rows, f'参考买入上限约为30.50元；按现价36.03元期望年化回报为{annual}%。')
    assert answer_problems(good, mat) == []
    # Rationale wording is not a pricing input.
    good['valuation']['rationale'][0]['text'] = '改写理由措辞。'
    assert answer_problems(good, mat) == []
    # Mutation: a price the program did not compute still needs a source.
    assert any('30.8' in p['problem'] for p in answer_problems(draft(rows, '参考买入上限约为30.80元。'), mat))


@pytest.mark.parametrize('change', [
    lambda v: with_probabilities(v, (40, 40, 20)),
    lambda v: v | {'payout_pct': 10},
    lambda v: v | {'basis': 'core_profit'},
])
def test_changed_scenarios_cannot_keep_old_price(change):
    mat = material()
    rows = ResearchTools(mat, None).run('valuation', {'valuation': assumptions()})
    report = draft(rows, '参考买入上限约为30.50元。')
    report['valuation'] = change(report['valuation'])
    assert any('重新调用 valuation' in p['problem'] for p in answer_problems(report, mat))


def test_stale_price_through_a_derived_calculation_is_caught():
    mat = material()
    tools = ResearchTools(mat, None)
    hurdle, _ = tools.run('valuation', {'valuation': assumptions()})
    gap = tools.run('calculate', {'label': '现价高于门槛价', 'operation': 'difference',
                                  'left': {'ref': 'M.close', 'period': BODY['market_date']}, 'right': {'ref': hurdle['id']}})
    report = {'verdict': '值得继续研究', 'summary': '值得继续研究：测试。', 'valuation': assumptions(),
              'answer': [{'text': f"现价高于门槛价{gap['values']['result']}元。", 'cites': [{'ref': gap['id']}]}]}
    assert answer_problems(report, mat) == []
    report['valuation'] = with_probabilities(report['valuation'], (5, 90, 5))
    assert any(hurdle['id'] in p['problem'] for p in answer_problems(report, mat))


def test_tool_errors_are_actionable_and_not_fetches():
    mat = material()
    with pytest.raises(ValueError, match='not_priced'):
        ResearchTools(mat, None).run('valuation', {'valuation': {'not_priced': {'text': 'x', 'cites': []}}})
    broken = assumptions()
    broken['scenarios']['保守']['probability'] = 80
    with pytest.raises(ValueError, match='probability'):
        ResearchTools(mat, None).run('valuation', {'valuation': broken})
    assert _fetches([{'tool': 'valuation'}, {'tool': 'calculate'}, {'tool': 'read_disclosure'}]) == 1


def test_reviewer_and_repair_see_the_page_price():
    mat = material()
    report = draft([], '参考买入上限采用程序生成的门槛价。')
    block = research.program_valuation(report, mat)
    assert '"hurdle_price": 30.5' in block and '"close": 36.03' in block
    assert research.program_valuation(report, mat) in research.repair_prompt(mat, report, [])
    assert research.program_valuation({'valuation': {'not_priced': {}}}, mat) == ''
