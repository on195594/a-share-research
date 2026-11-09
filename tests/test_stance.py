"""Research stance (V4): structure, the program-owned wait price, and frozen scoring inputs.

Tables are the captured 603606 report 37 and 600519 annual-report material; scenario inputs come from
support.valuation (real report_rc estimates used as test-only hypotheses).
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from a_share_research import checking, db, prompts, reading, research, reviewing, service, stance, validation, valuation
from a_share_research.material import Material
from a_share_research.sources import Document
from support.reports import MATERIAL, report
from support.templates import ENV, render
from support.valuation import BODY, assumptions, with_probabilities

BOUNDARY_RUNS = json.loads((Path(__file__).parent / "data/stance-boundary-20261105.json").read_text())
FLIP_SAMPLES = json.loads((Path(__file__).parent / "data/stance-flip-samples-20261107.json").read_text())


def material(table=None):
    return Material(BODY['code'], BODY['name'], Document(**BODY['document']), {}, {},
                    deepcopy(table or BODY['table']), BODY['market_date'])


EVIDENCE = {'action': '等证据', 'text': '上半年经营现金流转负，先看下一期是否转正。',
            'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}],
            'trigger': {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>=', 'value': 0}}


ADD = {k: deepcopy(v) for k, v in EVIDENCE.items() if k != 'trigger'}
ADD.update(action='现在分批', add_trigger=deepcopy(EVIDENCE['trigger']))


def stance_problems(value, table=None, valuation_value=None):
    return stance.problems(value, table or BODY['table'], valuation_value or assumptions(), BODY['market_date'])


def test_fixture_stance_passes_and_schema_names_next_period():
    assert not [p for p in checking.check(report(), MATERIAL) if p['where'] == 'stance']
    assert stance_problems(ADD) == []
    assert any('stance.action' in p for p in stance_problems(EVIDENCE))
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
    change = {('add_trigger' if k == 'trigger' else k): v for k, v in change.items()}
    assert any(needle in p for p in stance_problems(ADD | change))


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


def test_wait_price_rejects_non_positive_hurdle():
    zero_val = deepcopy(assumptions())
    for s in zero_val['scenarios'].values():
        s['profit'] = [0, 0, 0]
    zero_val['payout_pct'] = 0
    price, problem = stance.wait_price(zero_val, BODY['table'], BODY['market_date'])
    assert price == 0 and problem == "期望回报门槛价（0.00 元）非正数，等价格不成立：请选择现在分批或回避，并说明依据与风险"
    wait = {'action': '等价格', 'text': '期望回报尚未达到门槛。', 'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}]}
    probs = stance_problems(wait, valuation_value=zero_val)
    assert probs == ["期望回报门槛价（0.00 元）非正数，等价格不成立：请选择现在分批或回避，并说明依据与风险"]
    now = {'action': '现在分批', 'text': '分批建仓。', 'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}]}
    assert stance_problems(now, valuation_value=zero_val) == []


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
    frozen_add = research.stance_calc(ADD, calc, '2026-10-26')
    assert frozen_add == {'action': '现在分批', 'add_trigger': ADD['add_trigger']}
    assert frozen_add['add_trigger'] is not ADD['add_trigger']
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


def test_add_checkpoint_only_belongs_to_now_and_is_optional():
    assert stance_problems({k: v for k, v in ADD.items() if k != 'add_trigger'}) == []
    for action in ('回避', '等价格'):
        assert any('只含' in p for p in stance_problems(ADD | {'action': action}))


# --- Boundary Stance Tests (absorbed from test_stance_boundary) ---

def test_flipping_reruns_against_the_five_percent_band():
    xinru = [r for r in BOUNDARY_RUNS if r["code"] == "002946.SZ"]
    assert [r["action"] for r in xinru] == ["现在分批", "等价格", "等价格"]
    assert [stance.on_boundary(r["valuation_calc"]) for r in xinru] == [True, True, False]
    assert all(stance.on_boundary(r["valuation_calc"]) for r in BOUNDARY_RUNS if r["code"] == "601899.SH")


def test_a_wide_margin_is_not_a_boundary():
    shenhua = next(r for r in BOUNDARY_RUNS if r["code"] == "601088.SH")
    assert stance.hurdle_margin(shenhua["valuation_calc"]) == pytest.approx(0.433, abs=0.001)
    assert stance.on_boundary(shenhua["valuation_calc"]) is False


@pytest.mark.parametrize("calc", [None, {}, {"close": 16.9}, {"close": 16.9, "v2": {"support_below": None}},
                                  {"close": 16.9, "v2": {"support_below": -3}}, {"close": 0, "v2": {"support_below": 17}},
                                  {"close": True, "v2": {"support_below": 1}}])
def test_unpriced_or_invalid_hurdle_is_unknown_not_boundary(calc):
    assert stance.on_boundary(calc) is None


def test_edges_of_the_band():
    assert stance.on_boundary({"close": 105, "v2": {"support_below": 100}}) is True
    assert stance.on_boundary({"close": 105.01, "v2": {"support_below": 100}}) is False
    assert stance.on_boundary({"close": 94.99, "v2": {"support_below": 100}}) is False


def test_page_view_flags_boundary_only_inside_the_band():
    for run in BOUNDARY_RUNS:
        view = reading.stance_decision({"action": run["action"]}, {}, run["valuation_calc"])
        assert view["boundary"] is (stance.on_boundary(run["valuation_calc"]) is True)


def test_boundary_stances_are_summarized_apart():
    rows = [{"stance": "现在分批", "boundary": True, "returns": {"latest": {"excess": 0.10}}},
            {"stance": "现在分批", "boundary": False, "returns": {"latest": {"excess": -0.02}}},
            {"stance": "现在分批", "boundary": None, "returns": {"latest": {"excess": 0.04}}}]
    summary = validation.summarize_stances(rows)
    assert summary["现在分批·边界"] == {"n": 1, "scored": 1, "mean_excess": pytest.approx(0.10)}
    assert summary["现在分批"] == {"n": 2, "scored": 2, "mean_excess": pytest.approx(0.01)}


# --- Stance Flip Sensitivity Tests (absorbed from test_stance_flip) ---

@pytest.mark.parametrize('sample', FLIP_SAMPLES, ids=lambda s: str(s['report_id']))
def test_real_distance_reaches_close_and_matches_hurdle_side(sample):
    calc = sample['valuation_calc']
    original = deepcopy(calc)
    distance = stance.flip_distance(calc)
    assert distance is not None
    margin = stance.hurdle_margin(calc)
    assert (distance > 0) == (margin > 0)
    assert (distance < 0) == (margin < 0)
    assert (sample['action'] == '等价格') == (distance > 0)
    total = sum(weight * (calc['scenarios'][name]['exit_cap'] * (1 + distance)
                          + calc['scenarios'][name]['dividends'])
                for name, weight in calc['v2']['probabilities'].items())
    hurdle = calc['close'] * total / (calc['market_cap'] * (1 + calc['r']) ** calc['horizon'])
    assert hurdle == pytest.approx(calc['close'], rel=1e-6)
    assert calc == original
    view = reading.stance_decision({'action': sample['action']}, {}, calc)
    assert view['flip_distance'] == distance


@pytest.mark.parametrize('variant', [
    'missing_v2', 'missing_scenarios', 'zero_exit', 'negative_exit', 'unpriced',
    'missing_cap', 'missing_dividend', 'missing_weight', 'bad_weights', 'negative_weight',
    'nan', 'infinity', 'boolean', 'zero_horizon', 'bad_rate', 'unreachable', 'overflow',
])
def test_invalid_variants_return_none(variant):
    calc = deepcopy(FLIP_SAMPLES[0]['valuation_calc'])
    if variant.startswith('missing_'):
        if variant == 'missing_v2':
            del calc['v2']
        elif variant == 'missing_scenarios':
            del calc['scenarios']
        elif variant == 'missing_cap':
            del calc['market_cap']
        elif variant == 'missing_dividend':
            del calc['scenarios']['中性']['dividends']
        else:
            del calc['v2']['probabilities']['中性']
    elif variant in ('zero_exit', 'negative_exit'):
        for scenario in calc['scenarios'].values():
            scenario['exit_cap'] = 0 if variant == 'zero_exit' else -1
    elif variant == 'unpriced':
        calc['not_priced'] = True
    elif variant == 'bad_weights':
        calc['v2']['probabilities']['中性'] = 0.6
    elif variant == 'negative_weight':
        calc['v2']['probabilities'].update({'保守': -0.25, '中性': 1})
    elif variant in ('nan', 'infinity', 'boolean'):
        calc['market_cap'] = {'nan': float('nan'), 'infinity': float('inf'), 'boolean': True}[variant]
    elif variant == 'zero_horizon':
        calc['horizon'] = 0
    elif variant == 'bad_rate':
        calc['r'] = -1
    elif variant == 'unreachable':
        for scenario in calc['scenarios'].values():
            scenario['dividends'] = calc['market_cap'] * 10
    elif variant == 'overflow':
        calc['horizon'] = 1e308
    assert stance.flip_distance(calc) is None


@pytest.mark.parametrize('calc', [None, {}, [], {'v2': None}])
def test_missing_or_malformed_calc(calc):
    assert stance.flip_distance(calc) is None


def _render_views(sample, variant):
    calc = deepcopy(sample['valuation_calc'])
    action = '回避' if variant == 'avoid' else sample['action']
    if variant == 'none':
        del calc['scenarios']
    body = {'stance': {'action': action}, 'valuation_calc': calc, 'market_date': calc['as_of']}
    rep = {'id': sample['report_id'], 'code': sample['code'], 'name': sample['code'],
           'created_at': calc['as_of'], 'passed': True, 'verdict': None}
    card = reading.card(rep, body)
    assert card['flip_distance'] == stance.flip_distance(calc)

    def decision(*args):
        view = reading.stance_decision(*args)
        if variant == 'legacy_projection':
            del view['flip_distance']
        return view

    if variant == 'legacy_projection':
        del card['flip_distance']
        del card['stance']['flip_distance']
    cards = ENV.get_template('_card.html').module
    return [render('report.html', report=rep, body=body, rows={}, comparison=None, stance_decision=decision),
            str(cards.summary_card(card)), str(cards.full_card(card))]


@pytest.mark.parametrize('sample', FLIP_SAMPLES[:2], ids=lambda s: str(s['report_id']))
@pytest.mark.parametrize('variant', ['value', 'none', 'avoid', 'legacy_projection'])
def test_report_and_both_card_shapes(sample, variant):
    for html in _render_views(sample, variant):
        if variant in ('none', 'legacy_projection'):
            assert '假设敏感度' not in html
            continue
        assert html.count('假设敏感度（报告时点）') == 1
        assert '分红不变' in html
        distance = stance.flip_distance(sample['valuation_calc'])
        assert f'{abs(distance) * 100:.1f}%</span>' in html
        assert ('整体下调' if distance < 0 else '整体上调') in html
        if variant == 'avoid':
            assert '立场可能由' not in html
            assert '立场易受估值微调扰动' not in html
        else:
            assert ('立场可能由现在分批转为等价格' if distance < 0
                    else '立场可能由等价格转为现在分批') in html
            if abs(distance) <= 0.02:
                assert '距离不足 2%，属于假设临界敏感区间，立场易受估值微调扰动' in html


def test_exact_hurdle_has_zero_distance_and_no_direction_claim():
    calc = deepcopy(FLIP_SAMPLES[0]['valuation_calc'])
    calc['r'] = 0
    calc['market_cap'] = sum(w * (calc['scenarios'][n]['exit_cap'] + calc['scenarios'][n]['dividends'])
                             for n, w in calc['v2']['probabilities'].items())
    calc['v2']['support_below'] = calc['close']
    assert stance.flip_distance(calc) == 0
    view = reading.stance_decision({'action': '现在分批'}, {}, calc)
    html = str(ENV.get_template('_macros.html').module.flip_sensitivity(view))
    assert '报告收盘价已等于门槛价' in html
    assert '立场可能由' not in html
    assert '距离不足 2%' not in html


def test_critical_sensitivity_boundary_note():
    view_116 = reading.stance_decision({'action': '现在分批'}, {}, FLIP_SAMPLES[0]['valuation_calc'])
    html_116 = str(ENV.get_template('_macros.html').module.flip_sensitivity(view_116))
    assert '距离不足 2%，属于假设临界敏感区间，立场易受估值微调扰动' in html_116

    view_avoid = reading.stance_decision({'action': '回避'}, {}, FLIP_SAMPLES[0]['valuation_calc'])
    html_avoid = str(ENV.get_template('_macros.html').module.flip_sensitivity(view_avoid))
    assert '距离不足 2%' not in html_avoid
    assert '立场易受估值微调扰动' not in html_avoid
    assert '到门槛价。' in html_avoid

    sample_132 = next(s for s in FLIP_SAMPLES if s['report_id'] == 132)
    view_132 = reading.stance_decision({'action': '等价格'}, {}, sample_132['valuation_calc'])
    html_132 = str(ENV.get_template('_macros.html').module.flip_sensitivity(view_132))
    assert '距离不足 2%' not in html_132
    assert '8.7%' in html_132


@pytest.mark.parametrize('price_factor,day', [(1, '2026-11-05'), (1.2, '2027-01-30')])
def test_service_projects_report_and_current_basis_without_writing(tmp_path, price_factor, day):
    sample = FLIP_SAMPLES[0]
    calc = sample['valuation_calc']
    conn = db.connect(tmp_path / 'flip.db')
    code = sample['code'][:6]
    run = service.submit(conn, code, 'flip-sample')
    body = {'valuation_calc': calc, 'stance': {'action': sample['action']}}
    db.save_report(conn, run, sample['code'], code, '2026-09-30', True, '需要谨慎', body)
    db.update_run(conn, run, status='done')
    close = calc['close'] * price_factor
    conn.execute("INSERT INTO scans(status,requested_at,summary) VALUES ('done',?,?)",
                 (day, json.dumps({'closes': {code: {'last_close': close, 'last_close_date': day}}})))
    conn.execute('PRAGMA query_only=ON')
    before = conn.total_changes
    card = service.company_card(conn, code)
    assert card['valuation_calc']['flip_distance'] == stance.flip_distance(calc)
    current = card['current']['valuation']
    distance = current['flip_distance']
    assert distance is not None
    total = sum(w * (s['exit_cap'] * (1 + distance) + s['dividends'])
                for name, w in calc['v2']['probabilities'].items() for s in [calc['scenarios'][name]])
    new_hurdle = calc['close'] * total / (calc['market_cap'] * (1 + calc['r']) ** current['horizon'])
    assert new_hurdle == pytest.approx(close, rel=1e-6)
    assert (distance > 0) == (close > current['v2']['support_below'])
    assert json.loads(conn.execute('SELECT body FROM reports').fetchone()[0]) == body
    assert conn.total_changes == before
    conn.close()
