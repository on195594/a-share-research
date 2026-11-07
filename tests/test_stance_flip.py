"""Exit-cap sensitivity from 24 captured production reports; variants are test-only."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from a_share_research import db, reading, service, stance
from support.templates import ENV, render

SAMPLES = json.loads((Path(__file__).parent / 'data/stance-flip-samples-20261107.json').read_text())


@pytest.mark.parametrize('sample', SAMPLES, ids=lambda s: str(s['report_id']))
def test_real_distance_reaches_close_and_matches_hurdle_side(sample):
    calc = sample['valuation_calc']
    original = deepcopy(calc)
    distance = stance.flip_distance(calc)
    assert distance is not None
    margin = stance.hurdle_margin(calc)
    assert (distance > 0) == (margin > 0)
    assert (distance < 0) == (margin < 0)
    assert (sample['action'] == '等价格') == (distance > 0)
    # Substitute the uniform scaling into the original price formula, including frozen dividends.
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
    # Minimal, explicitly invalid variants of report 116; never used as research evidence.
    calc = deepcopy(SAMPLES[0]['valuation_calc'])
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


def render_views(sample, variant):
    calc = deepcopy(sample['valuation_calc'])
    action = '回避' if variant == 'avoid' else sample['action']
    if variant == 'none':
        del calc['scenarios']
    body = {'stance': {'action': action}, 'valuation_calc': calc, 'market_date': calc['as_of']}
    report = {'id': sample['report_id'], 'code': sample['code'], 'name': sample['code'],
              'created_at': calc['as_of'], 'passed': True, 'verdict': None}
    card = reading.card(report, body)
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
    return [render('report.html', report=report, body=body, rows={}, comparison=None, stance_decision=decision),
            str(cards.summary_card(card)), str(cards.full_card(card))]


@pytest.mark.parametrize('sample', SAMPLES[:2], ids=lambda s: str(s['report_id']))
@pytest.mark.parametrize('variant', ['value', 'none', 'avoid', 'legacy_projection'])
def test_report_and_both_card_shapes(sample, variant):
    for html in render_views(sample, variant):
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
        else:
            assert ('立场可能由现在分批转为等价格' if distance < 0
                    else '立场可能由等价格转为现在分批') in html


def test_exact_hurdle_has_zero_distance_and_no_direction_claim():
    # Test-only equality variant: zero discount rate and cap equal to weighted terminal value.
    calc = deepcopy(SAMPLES[0]['valuation_calc'])
    calc['r'] = 0
    calc['market_cap'] = sum(w * (calc['scenarios'][n]['exit_cap'] + calc['scenarios'][n]['dividends'])
                             for n, w in calc['v2']['probabilities'].items())
    calc['v2']['support_below'] = calc['close']
    assert stance.flip_distance(calc) == 0
    view = reading.stance_decision({'action': '现在分批'}, {}, calc)
    html = str(ENV.get_template('_macros.html').module.flip_sensitivity(view))
    assert '报告收盘价已等于门槛价' in html
    assert '立场可能由' not in html


@pytest.mark.parametrize('price_factor,day', [(1, '2026-11-05'), (1.2, '2027-01-30')])
def test_service_projects_report_and_current_basis_without_writing(tmp_path, price_factor, day):
    sample = SAMPLES[0]
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
    # at() prices using the report's share count, not the current price with the report's cap.
    new_hurdle = calc['close'] * total / (calc['market_cap'] * (1 + calc['r']) ** current['horizon'])
    assert new_hurdle == pytest.approx(close, rel=1e-6)
    assert (distance > 0) == (close > current['v2']['support_below'])
    assert json.loads(conn.execute('SELECT body FROM reports').fetchone()[0]) == body
    assert conn.total_changes == before
    conn.close()
