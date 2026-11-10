"""Scenario arithmetic on production report 37 and captured 603606 sell-side estimates."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json

import pytest

from a_share_research import checking, prompts, research, reviewing, stance, valuation
from a_share_research.material import Material
from a_share_research.research_tools import ResearchTools, _fetches
from a_share_research.sources import Document
from support.reports import MATERIAL, report
from support.valuation import BODY, ROWS, STORED, assumptions, with_probabilities


def material():
    return Material(BODY['code'], BODY['name'], Document(**BODY['document']), {}, {},
                    deepcopy(BODY['table']), BODY['market_date'])


@pytest.mark.parametrize('record', [r for r in STORED if not r['valuation_calc'].get('not_priced')])
def test_at_reproduces_real_stored_compute_exactly(record):
    calc = record['valuation_calc']
    original = deepcopy(calc)
    computed = valuation.compute(with_probabilities(record['valuation']), record['table'], record['market_date'])
    rolled = valuation.at(calc, calc['close'], calc['as_of'])
    assert rolled['annual_returns'] == {n: calc['scenarios'][n]['annual_return'] for n in valuation.SCENARIOS}
    assert rolled['annual_returns'] == {n: computed['scenarios'][n]['annual_return'] for n in valuation.SCENARIOS}
    assert {k: rolled[k] for k in calc['zones']} == computed['zones'] == calc['zones']
    assert rolled['zone'] == computed['zone'] == calc['zone']
    assert rolled['horizon'] == calc['horizon']
    assert (rolled['as_of'], rolled['close']) == (calc['as_of'], calc['close'])
    assert calc == original
    json.dumps(rolled, allow_nan=False)


def test_at_rolls_lines_and_returns_without_changing_terminal_totals():
    calc = STORED[2]['valuation_calc']  # Captured 600900 report, initially stretched.
    rolled = valuation.at(calc, calc['close'], '2027-10-30')
    assert rolled['support_below'] > calc['zones']['support_below']
    assert rolled['stretched_above'] > calc['zones']['stretched_above']
    assert calc['zone'] == '测算透支区' and rolled['zone'] == '中性区'
    assert rolled['horizon'] < calc['horizon']
    assert rolled['annual_returns']['中性'] > calc['scenarios']['中性']['annual_return']
    # Price mutation isolates cap scaling from the date roll; zone lines do not move with price.
    lower = valuation.at(calc, calc['close'] / 2, '2027-10-30')
    assert lower['support_below'] == rolled['support_below']
    assert lower['stretched_above'] == rolled['stretched_above']
    assert lower['zone'] == '测算支持区'
    for name, scenario in calc['scenarios'].items():
        expected = ((scenario['exit_cap'] + scenario['dividends']) / (calc['market_cap'] / 2)) ** (1 / rolled['horizon']) - 1
        assert lower['annual_returns'][name] == pytest.approx(expected, abs=1e-14)
    for offset in (0, 1):
        day = (date.fromisoformat(calc['exit_date']) + timedelta(days=offset)).isoformat()
        expired = valuation.at(calc, calc['close'], day)
        assert expired['zone'] is None and expired['annual_returns'] is None
        assert '退出日' in expired['reason']


@pytest.mark.parametrize('mutation', ['missing', 'error', 'not_priced', 'negative_total', 'bad_rate', 'bad_close', 'bad_date'])
def test_at_uncomputable_real_record_mutations(mutation):
    calc = deepcopy(STORED[0]['valuation_calc'])
    close, day = calc['close'], calc['as_of']
    if mutation == 'missing':
        del calc['scenarios']['中性']['exit_cap']
    elif mutation == 'error':
        calc['error'] = '缺少行情'
    elif mutation == 'not_priced':
        calc = STORED[1]['valuation_calc']
    elif mutation == 'negative_total':
        calc['scenarios']['保守']['exit_cap'] *= -1
    elif mutation == 'bad_rate':
        calc['r'] = -1
    elif mutation == 'bad_close':
        close = float('nan')
    else:
        day = 'invalid'
    result = valuation.at(calc, close, day)
    assert all(result[k] is None for k in ('annual_returns', 'support_below', 'stretched_above', 'inverted', 'zone', 'horizon'))
    assert result['reason']
    json.dumps(result, allow_nan=False)


def test_at_inverted_and_zero_total_mutations():
    calc = deepcopy(STORED[0]['valuation_calc'])
    calc['scenarios']['保守']['exit_cap'] = calc['scenarios']['乐观']['exit_cap']
    result = valuation.at(calc, calc['close'], calc['as_of'])
    assert result['inverted'] is True and result['zone'] is None
    assert result['annual_returns'] is not None and 'reason' not in result
    calc['scenarios']['保守']['exit_cap'] = -calc['scenarios']['保守']['dividends']
    result = valuation.at(calc, calc['close'], calc['as_of'])
    assert result['annual_returns']['保守'] == -1 and result['support_below'] == 0


def test_complete_hand_calculation():
    value = assumptions()
    calc = valuation.compute(value, BODY['table'], BODY['market_date'])
    # Independent worksheet: report37 close=36.03, cap=297.18亿元, 2025 profit=12.71亿元.
    # 2026-10-26 -> 2029-04-30 is 917 days, hence 917/365.25 = 2.510609171800137 years.
    # Neutral: 22.43*14.6=327.478; only Y1+Y2 dividends=(16.5+17.13)*.0129=.433827.
    # Total=327.911827; annual=(327.911827/297.18)^(365.25/917)-1=.03997467111448931.
    # Required price at 10%=36.03*327.911827/(297.18*1.1^(917/365.25))=31.29542902910213.
    assert calc['rule'] == 'valuation-v1'
    assert (calc['as_of'], calc['close'], calc['market_cap']) == ('2026-10-26', 36.03, 297.18)
    assert (calc['basis'], calc['base_year'], calc['base_profit'], calc['years']) == ('net_profit', 2025, 12.71, [2026, 2027, 2028])
    assert calc['exit_date'] == '2029-04-30'
    assert calc['horizon'] == pytest.approx(917 / 365.25, rel=1e-13)
    for name, expected in {
        '保守': [302.5242, .37797, .007625436113148957, 28.908543649436677],
        '中性': [327.478, .433827, .03997467111448931, 31.29542902910213],
        '乐观': [319.232, .465432, .029518635700347698, 30.511459087879146],
    }.items():
        assert list(calc['scenarios'][name].values()) == pytest.approx(expected, rel=1e-12)
    assert calc['zones']['support_below'] == pytest.approx(28.908543649436677)
    assert calc['zones']['stretched_above'] == pytest.approx(36.91241930826684)
    assert calc['zones']['inverted'] is False and calc['zone'] == '中性区'
    # Implied: (297.18*1.1^(917/365.25)-.433827)/14.6 = 25.827847994457166.
    assert calc['implied'] == pytest.approx({'profit': 25.827847994457166, 'cagr': .2666235458671805})
    assert calc['bridge'] == pytest.approx({'profit_change': 22.43/12.71,
                                           'pe_change': 14.6/(297.18/12.71), 'dividend_share': .433827/297.18})
    assert calc['sensitivity']['profit_factors'] == [.8, .9, 1, 1.1, 1.2]
    assert calc['sensitivity']['exit_pe'] == [13.89, 14.6, 12.8]
    for factor, row in zip([.8, .9, 1, 1.1, 1.2], calc['sensitivity']['returns']):
        assert row == pytest.approx([((22.43*factor*pe+.433827)/297.18)**(365.25/917)-1
                                    for pe in (13.89, 14.6, 12.8)], abs=1e-14)
    # These explicit alternative calculations expose the two requested mutations.
    wrong_dividend = sum(value['scenarios']['中性']['profit']) * .0129
    assert calc['scenarios']['中性']['dividends'] != pytest.approx(wrong_dividend)
    assert calc['scenarios']['中性']['annual_return'] != pytest.approx((327.911827/297.18)**(1/3)-1)
    assert any('股本不变' in s for s in calc['assumptions'])
    assert calc['pe_reference'] is None and calc['consensus']['status'] == 'none'
    json.dumps(calc, allow_nan=False)


def test_consensus_real_rows_unit_deduplication_and_point_in_time():
    result = valuation.consensus(ROWS, [2026, 2027, 2028], '2026-10-31')
    assert result['status'] == 'ok'
    assert result['years'] == {
        '2026': {'median': 15.815, 'n_orgs': 14, 'min': 13.72, 'max': 19.49, 'oldest_days': 87},
        '2027': {'median': 20.065, 'n_orgs': 14, 'min': 15.58, 'max': 24.69, 'oldest_days': 87},
        '2028': {'median': 24.16, 'n_orgs': 13, 'min': 21.78, 'max': 28.95, 'oldest_days': 87},
    }
    # Same institution's October update supersedes August; input order/duplicates cannot count twice.
    assert valuation.consensus(list(reversed(ROWS)) + ROWS, [2026, 2027, 2028], '2026-10-31') == result
    earlier = valuation.consensus(ROWS, [2026, 2027, 2028], '2026-10-26')
    assert earlier['years']['2028'] == {'median': 24.485, 'n_orgs': 12, 'min': 22.43, 'max': 28.95, 'oldest_days': 82}
    huatai = [r for r in ROWS if r['org_name'] == '华泰证券']
    assert valuation.consensus(huatai, [2028], '2026-10-31')['years']['2028']['median'] == 21.78
    assert valuation.consensus(huatai, [2028], '2026-10-26')['years']['2028']['median'] == 24.03
    newest = next(r for r in huatai if r['quarter'] == '2028Q4' and r['report_date'] == '20261030')
    assert newest['np'] / newest['eps'] / 10000 == pytest.approx(8.25)
    assert valuation.consensus(ROWS, [2029], '2026-10-31') == {'status': 'none', 'years': {}, 'coverage': None}
    assert valuation.consensus(ROWS, [2026], '2027-02-01')['status'] == 'none'
    assert valuation.consensus([newest], [2028], '2027-01-28')['status'] == 'ok'  # day 90
    assert valuation.consensus([newest], [2028], '2027-01-29')['status'] == 'none'


def test_comparison_does_not_change_valuation_inputs():
    compare = valuation.consensus(ROWS, [2026, 2027, 2028], BODY['market_date'])
    frozen = deepcopy(compare)
    result = valuation.compute(assumptions(), BODY['table'], BODY['market_date'], consensus=compare)
    plain = valuation.compute(assumptions(), BODY['table'], BODY['market_date'])
    assert compare == frozen
    assert result['scenarios'] == plain['scenarios'] and result['zones'] == plain['zones']
    assert result['consensus']['neutral_vs_median']['2028'] == pytest.approx(22.43 / 24.485 - 1)
    unavailable = valuation.compute(assumptions(), BODY['table'], BODY['market_date'],
                                    consensus={'status': 'unavailable', 'reason': '限频'})
    assert unavailable['consensus']['status'] == 'unavailable'
    assert unavailable['scenarios'] == plain['scenarios']


def test_contract_reuses_citation_and_number_checks_and_requires_years():
    draft = report(valuation=assumptions())
    mat = material()
    assert not [p for p in checking.check(draft, mat) if p['where'] == 'valuation']
    draft['valuation']['rationale'][0]['text'] = '基期归母净利润为9999.99亿元。'
    errors = checking.check(draft, mat)
    assert any(p['where'] == 'valuation' and '9999.99' in p['problem'] for p in errors)
    draft['valuation']['rationale'][0]['cites'][0]['period'] = '2022-12-31'
    assert any(p['type'] == 'citation_calculation' for p in checking.check(draft, mat) if p['where'] == 'valuation')
    wrong = assumptions()
    wrong['years'] = [2025, 2026, 2027]
    assert any('[2026, 2027, 2028]' in p for p in valuation.problems(wrong, mat.table))
    wrong = assumptions()
    wrong['scenarios']['保守']['profit'][2] = wrong['scenarios']['乐观']['profit'][2] + 1
    assert any('保守 ≤ 中性 ≤ 乐观' in p for p in valuation.problems(wrong, mat.table))
    assert valuation.problems(assumptions(), mat.table) == []  # exit_pe need not be monotone
    draft = report(valuation=assumptions())
    draft['valuation']['rationale'] = []
    assert {p['where'] for p in checking.check(draft, MATERIAL)} == {'valuation'}


@pytest.mark.parametrize('field,value', [('payout_pct', -1), ('payout_pct', 101), ('payout_pct', True),
                                         ('basis', 'tp'), ('years', None), ('rationale', [])])
def test_bad_structure(field, value):
    draft = assumptions()
    draft[field] = value
    assert valuation.problems(draft, BODY['table'])


@pytest.mark.parametrize('value', [0, 201, float('nan'), float('inf'), True, '14.6'])
def test_invalid_exit_pe(value):
    draft = assumptions()
    draft['scenarios']['中性']['exit_pe'] = value
    assert any('exit_pe' in p for p in valuation.problems(draft, BODY['table']))


def test_missing_contract_and_feedback_exemption():
    draft = report()
    del draft['valuation']
    assert any(p['where'] == 'valuation' for p in checking.check(draft, MATERIAL))
    mat = deepcopy(MATERIAL)
    mat.extra['feedback'] = {'location': 'summary', 'text': '核查'}
    assert not any(p['where'] == 'valuation' for p in checking.check(draft, mat))
    schema = prompts.output_schema(material())
    assert '"years": [2026, 2027, 2028]' in schema
    assert 'report_rc' not in schema
    assert prompts.VALUATION not in prompts.output_schema(mat)


def test_patch_publish_and_review_target():
    before = report()
    merged = research.merge_repair_patch(before, {'valuation': assumptions()}, [], MATERIAL)
    assert merged['valuation'] == assumptions() and before == report()
    assert research.published(merged)['valuation'] == assumptions()
    assert reviewing.target_exists('valuation', merged)
    assert not reviewing.target_exists('valuation', {'summary': '旧报告'})


def test_not_priced_error_and_replay_degrade_without_fetching(caplog):
    value = report()['valuation']
    result = valuation.compute(value, BODY['table'], BODY['market_date'])
    assert result == {'rule': 'valuation-v1', 'not_priced': True, 'as_of': '2026-10-26', 'close': 36.03}
    assert research.calculate_valuation(value, material()) == result  # not_priced needs no report_rc call
    replay = research.calculate_valuation(assumptions(), material(), replay=True)
    assert replay['consensus']['status'] == 'unavailable' and 'error' not in replay
    mat = material()
    mat.table = [r for r in mat.table if r['id'] != 'M.total_mv']
    error = research.calculate_valuation(assumptions(), mat, replay=True)
    assert error['rule'] == 'valuation-v1' and 'M.total_mv' in error['error']
    assert 'valuation 603606.SH failed' in caplog.text


def test_zero_base_core_basis_and_inverted_zones():
    value = assumptions()
    value['basis'] = 'core_profit'
    result = valuation.compute(value, BODY['table'], BODY['market_date'])
    assert result['base_profit'] == 12.54
    assert any('口径不同' in s for s in result['assumptions'])
    table = deepcopy(BODY['table'])
    next(r for r in table if r['id'] == 'T.core_profit')['values']['2025-12-31'] = '0'
    result = valuation.compute(value, table, BODY['market_date'])
    assert result['implied']['cagr'] is None
    assert result['bridge']['profit_change'] is None and result['bridge']['pe_change'] is None
    # Swap real recorded multiples to force overlap without imposing PE monotonicity.
    value = assumptions()
    value['scenarios']['保守']['exit_pe'] = next(r['pe'] for r in ROWS if r['quarter'] == '2026Q4' and r['pe'])
    result = valuation.compute(value, BODY['table'], BODY['market_date'])
    assert result['zones']['inverted'] is True and result['zone'] is None


def test_zone_boundaries_use_report_close():
    # Mutation of the real market cap tests classification on either side of the reference range.
    for factor, zone in [(Decimal('.8'), '测算支持区'), (Decimal('1.2'), '测算透支区')]:
        table = deepcopy(BODY['table'])
        row = next(r for r in table if r['id'] == 'M.total_mv')
        row['values'][BODY['market_date']] = str(Decimal('297.18') * factor)
        assert valuation.compute(assumptions(), table, BODY['market_date'])['zone'] == zone


def test_neutral_dividend_sensitivity_preserves_real_roll_and_rejects_negative_cash():
    calc = deepcopy(STORED[2]['valuation_calc'])
    result = valuation.at(calc, calc['close'], calc['as_of'])
    expected = (calc['scenarios']['中性']['exit_cap'] / calc['market_cap']) ** (1 / result['horizon']) - 1
    assert result['neutral_return_without_dividends'] == pytest.approx(expected)
    assert expected < result['annual_returns']['中性']
    calc['scenarios']['中性']['dividends'] = 0  # Zero-payout boundary of the captured calc.
    zero = valuation.at(calc, calc['close'], calc['as_of'])
    assert zero['neutral_return_without_dividends'] == zero['annual_returns']['中性']
    calc['scenarios']['中性']['dividends'] = -1
    negative = valuation.at(calc, calc['close'], calc['as_of'])
    assert negative['neutral_return_without_dividends'] is None
    assert negative['annual_returns'] is not None


def test_v2_expected_return_hand_calculation_and_v1_independence():
    calc = valuation.compute(assumptions(), BODY['table'], BODY['market_date'])
    # Worksheet (25/50/25): totals 302.90217, 327.911827, 319.697432 → expected 319.605814亿元.
    h = 917 / 365.25
    total = .25 * 302.90217 + .5 * 327.911827 + .25 * 319.697432
    assert total == pytest.approx(319.605814)
    v2 = calc['v2']
    assert v2['rule'] == 'valuation-v2'
    assert v2['probabilities'] == {'保守': .25, '中性': .5, '乐观': .25}
    assert v2['annual_return'] == pytest.approx((total / 297.18) ** (1 / h) - 1, rel=1e-12)
    assert v2['support_below'] == pytest.approx(36.03 * total / (297.18 * 1.1 ** h), rel=1e-12)
    assert v2['stretched_above'] == pytest.approx(36.03 * total / (297.18 * 1.03 ** h), rel=1e-12)
    assert v2['annual_return'] < .03 and v2['zone'] == '测算透支区'  # 2.9% expected < r_low, v1 says 中性区
    assert calc['zone'] == '中性区'
    # Mutation: moving probability changes only v2; every v1 field stays byte-identical.
    shifted = valuation.compute(with_probabilities(assumptions(), (10, 30, 60)), BODY['table'], BODY['market_date'])
    assert {k: v for k, v in shifted.items() if k not in ('v2',)} == {k: v for k, v in calc.items() if k not in ('v2',)}
    assert shifted['v2']['annual_return'] != pytest.approx(v2['annual_return'])
    # The expected-value line sits above v1's conservative-only line whenever the other cases are richer.
    assert v2['support_below'] > calc['zones']['support_below']
    json.dumps(calc, allow_nan=False)


def test_v2_rolls_with_close_and_date_like_v1():
    calc = valuation.compute(assumptions(), BODY['table'], BODY['market_date'])
    same = valuation.at(calc, calc['close'], calc['as_of'])
    numeric = ('annual_return', 'support_below', 'stretched_above')
    assert {k: same['v2'][k] for k in numeric} == pytest.approx({k: calc['v2'][k] for k in numeric}, rel=1e-13)
    assert {k: same['v2'][k] for k in ('rule', 'probabilities', 'zone')} == {
        k: calc['v2'][k] for k in ('rule', 'probabilities', 'zone')}
    cheap = valuation.at(calc, calc['v2']['support_below'] * .99, calc['as_of'])
    assert cheap['v2']['zone'] == '测算支持区' and cheap['v2']['annual_return'] > .10
    assert cheap['zone'] == '中性区'  # v1 still demands the conservative case alone clears 10%
    later = valuation.at(calc, calc['close'], '2027-10-26')
    assert later['v2']['support_below'] > calc['v2']['support_below']
    # Stored pre-v2 reports keep rolling without inventing probabilities.
    assert valuation.at(STORED[2]['valuation_calc'], 30, '2026-11-03')['v2'] is None


@pytest.mark.parametrize('weights,needle', [((25, 50, 30), '之和须为 100'), ((0, 50, 50), '5–90'),
                                            ((95, 3, 2), '5–90'), ((25.0, 50, 25), '整数'), (('25', 50, 25), '整数')])
def test_v2_probability_contract(weights, needle):
    draft = with_probabilities(assumptions(), weights)
    assert any(needle in p for p in valuation.problems(draft, BODY['table']))
    missing = assumptions()
    del missing['scenarios']['乐观']['probability']
    assert any('probability' in p for p in valuation.problems(missing, BODY['table']))


# --- Valuation Tool & Integration Tests ---

def _tool_material():
    mat = Material(BODY['code'], BODY['name'], Document(**BODY['document']), {}, {},
                   deepcopy(BODY['table']), BODY['market_date'])
    mat.extra['question'] = '近期可以买入吗？参考价格是多少'
    return mat


def _tool_draft(rows, text):
    return {'verdict': '值得继续研究', 'summary': '值得继续研究：测试。', 'valuation': assumptions(),
            'answer': [{'text': text, 'cites': [{'ref': r['id']} for r in rows]}]}


def _answer_problems(rep, mat):
    return [p for p in checking.check(rep, mat) if p['where'] == 'answer[0]']


def test_tool_rows_equal_program_wait_price_and_are_citable():
    mat = _tool_material()
    rows = ResearchTools(mat, None).run('valuation', {'valuation': assumptions()})
    price, _ = stance.wait_price(assumptions(), BODY['table'], BODY['market_date'])
    calc = valuation.compute(assumptions(), BODY['table'], BODY['market_date'])
    assert [r['id'] for r in rows] == [f'C.{n}' for n in range(4, 12)] and rows == mat.table[-8:]
    assert float(rows[0]['exact_values']['result']) == pytest.approx(price) == pytest.approx(30.502715198880022)
    assert float(rows[1]['exact_values']['result']) == pytest.approx(calc['v2']['annual_return'] * 100)
    annual = rows[1]['values']['result']
    good = _tool_draft(rows, f'参考买入上限约为30.50元；按现价36.03元期望年化回报为{annual}%。')
    assert _answer_problems(good, mat) == []
    good['valuation']['rationale'][0]['text'] = '改写理由措辞。'
    assert _answer_problems(good, mat) == []
    assert any('30.8' in p['problem'] for p in _answer_problems(_tool_draft(rows, '参考买入上限约为30.80元。'), mat))


@pytest.mark.parametrize('change', [
    lambda v: with_probabilities(v, (40, 40, 20)),
    lambda v: v | {'payout_pct': 10},
    lambda v: v | {'basis': 'core_profit'},
])
def test_changed_scenarios_cannot_keep_old_price(change):
    mat = _tool_material()
    rows = ResearchTools(mat, None).run('valuation', {'valuation': assumptions()})
    rep = _tool_draft(rows, '参考买入上限约为30.50元。')
    rep['valuation'] = change(rep['valuation'])
    assert any('重新调用 valuation' in p['problem'] for p in _answer_problems(rep, mat))


def test_stale_price_through_a_derived_calculation_is_caught():
    mat = _tool_material()
    tools = ResearchTools(mat, None)
    hurdle = tools.run('valuation', {'valuation': assumptions()})[0]
    gap = tools.run('calculate', {'label': '现价高于门槛价', 'operation': 'difference',
                                  'left': {'ref': 'M.close', 'period': BODY['market_date']}, 'right': {'ref': hurdle['id']}})
    rep = {'verdict': '值得继续研究', 'summary': '值得继续研究：测试。', 'valuation': assumptions(),
           'answer': [{'text': f"现价高于门槛价{gap['values']['result']}元。", 'cites': [{'ref': gap['id']}]}]}
    assert _answer_problems(rep, mat) == []
    rep['valuation'] = with_probabilities(rep['valuation'], (5, 90, 5))
    assert any(hurdle['id'] in p['problem'] for p in _answer_problems(rep, mat))


def test_tool_errors_are_actionable_and_not_fetches():
    mat = _tool_material()
    with pytest.raises(ValueError, match='not_priced'):
        ResearchTools(mat, None).run('valuation', {'valuation': {'not_priced': {'text': 'x', 'cites': []}}})
    broken = assumptions()
    broken['scenarios']['保守']['probability'] = 80
    with pytest.raises(ValueError, match='probability'):
        ResearchTools(mat, None).run('valuation', {'valuation': broken})
    assert _fetches([{'tool': 'valuation'}, {'tool': 'calculate'}, {'tool': 'read_disclosure'}]) == 1


def test_reviewer_and_repair_see_the_page_price():
    mat = _tool_material()
    rep = _tool_draft([], '参考买入上限采用程序生成的门槛价。')
    block = research.program_valuation(rep, mat)
    assert '"hurdle_price": 30.5' in block and '"close": 36.03' in block
    assert research.program_valuation(rep, mat) in research.repair_prompt(mat, rep, [])
    assert research.program_valuation({'valuation': {'not_priced': {}}}, mat) == ''
