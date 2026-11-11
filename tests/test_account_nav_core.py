"""Public GIPS method example and labelled structural cases, not account acceptance."""
from datetime import datetime
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from fractions import Fraction
import json

import pytest
from pydantic import ValidationError

from a_share_research.core.account_nav import Flow, Inputs, Valuation, evaluate


def valuation(day, amount, clock='15:00:00'):
    return dict(as_of=f'{day}T{clock}+08:00', total_assets=amount)


def flow(event_id=1, cash='50', amount='100', day='2026-11-03', clock='12:00:00'):
    before = valuation(day, amount, clock)
    return dict(event_id=event_id, cash=cash, occurred_at=before['as_of'], before=before)


def structural(**changes):
    """Invented round numbers exercise arithmetic, never real financial evidence."""
    return dict(start=valuation('2026-11-02', '100'), end=valuation('2026-11-06', '110'),
                flows=[], benchmark_start='100', benchmark_end='105') | changes


def test_public_gips_true_twr_example():
    # CFA Institute, GIPS calculation methodology (2006), Applications 3, PDF p11:
    # https://www.gipsstandards.org/wp-content/uploads/2021/03/calculation_methodology_gs_2006.pdf
    # Original EUR amounts and dates. Source has dates, not intraday times: the
    # Shanghai times below are an explicit test adapter, not source timestamps.
    # Benchmark 1 -> 1 is structural; this is neither CSI800 nor account evidence.
    inputs = Inputs(start=valuation('1999-12-31', '500000'), end=valuation('2000-03-31', '570000'),
                    flows=[flow(1, '50000', '513000', '2000-02-19'),
                           flow(2, '-20000', '585000', '2000-03-12')],
                    benchmark_start='1', benchmark_end='1')
    result = evaluate(inputs)
    # Independent geometric sub-period linking, rather than replicating units.
    linked = Fraction(513000, 500000) * Fraction(585000, 563000) * Fraction(570000, 565000) - 1
    assert abs(Fraction(result['account_return']) - linked) < Fraction(1, 10**32)
    # The source's 7.48% quarter figure links coarsely rounded monthly returns;
    # the original amounts give 7.55% when rounded only at final presentation.
    assert (Decimal(result['account_return']) * 100).quantize(Decimal('.01')) == Decimal('7.55')
    assert json.loads(json.dumps(result)) == result


def test_no_flows_and_zero_terminal_value():
    result = evaluate(Inputs(**structural()))
    assert Decimal(result['account_return']) == Decimal('.1')
    assert Decimal(result['benchmark_return']) == Decimal('.05')
    assert Decimal(result['excess_percentage_points']) == Decimal('5')
    assert result['initial_nav'] == '1' and result['flows'] == []
    result = evaluate(Inputs(**structural(end=valuation('2026-11-06', 0))))
    assert Decimal(result['end_nav']) == 0 and Decimal(result['account_return']) == -1


@pytest.mark.parametrize(('cash', 'ending'), [('50', '150'), ('-50', '50')])
def test_pure_external_flows_do_not_create_returns(cash, ending):
    result = evaluate(Inputs(**structural(flows=[flow(cash=cash)], end=valuation('2026-11-06', ending))))
    assert Decimal(result['account_return']) == 0
    assert Decimal(result['end_units']) == Decimal(ending)
    audit = result['flows'][0]
    assert Decimal(audit['before_nav']) == Decimal(audit['after_assets']) / Decimal(audit['after_units'])


def test_returns_before_and_after_flows_and_order_independence():
    flows = [flow(2, '-60', '180', '2026-11-04'), flow(1, '55', '110')]
    inputs = structural(flows=flows, end=valuation('2026-11-06', '132'))
    result = evaluate(Inputs(**inputs))
    assert result == evaluate(Inputs(**(inputs | dict(flows=list(reversed(flows))))))
    assert Decimal(result['end_nav']) == Decimal('1.32')
    assert [item['event_id'] for item in result['flows']] == [1, 2]
    assert result['flows'][0]['before_nav'] == '1.1'


def test_decimal_global_context_cannot_change_output():
    inputs = Inputs(**structural(flows=[flow(cash='7', amount='103')]))
    expected = evaluate(inputs)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        assert evaluate(inputs) == expected


@pytest.mark.parametrize('amount', [True, False, float('nan'), float('inf'), '-Infinity',
                                   Decimal('sNaN'), 'bad', {}, None])
def test_invalid_amounts_rejected(amount):
    with pytest.raises(ValidationError):
        Valuation(**valuation('2026-11-02', amount))
    with pytest.raises(ValidationError):
        Flow(**flow(cash=amount))
    with pytest.raises(ValidationError):
        Inputs(**structural(benchmark_start=amount))


@pytest.mark.parametrize('amount', ['12.5', 12.5, Decimal('12.5'), 12])
def test_supported_amount_types(amount):
    assert Valuation(**valuation('2026-11-02', amount)).total_assets == Decimal(str(amount))


@pytest.mark.parametrize('timestamp', ['2026-11-02', '2026-11-02T15:00:00',
                                      datetime(2026, 11, 2, 15), 1793602800, True])
def test_unknown_timezone_or_partial_dates_rejected(timestamp):
    with pytest.raises(ValidationError):
        Valuation(as_of=timestamp, total_assets='100')


def test_normalizes_timezone_before_close_check():
    value = Inputs(**structural(start=dict(as_of='2026-11-02T07:00:00Z', total_assets='100')))
    assert value.start.as_of.isoformat() == '2026-11-02T15:00:00+08:00'


@pytest.mark.parametrize('changes', [
    dict(start=valuation('2026-11-02', '0')),
    dict(end=valuation('2026-11-06', '-1')),
    dict(end=valuation('2026-11-02', '110')),
    dict(end=valuation('2026-11-01', '110')),
    dict(start=valuation('2026-11-02', '100', '14:59:59')),
    dict(end=valuation('2026-11-06', '110', '15:00:00.000001')),
    dict(benchmark_start='0'), dict(benchmark_end='-1'),
    dict(flows=[flow(day='2026-11-02', clock='15:00:00')]),
    dict(flows=[flow(day='2026-11-06', clock='15:00:00')]),
    dict(flows=[flow(day='2026-11-01')]),
    dict(flows=[flow(), flow(event_id=2)]),
    dict(flows=[flow(), flow(day='2026-11-04')]),
    dict(schema_version=True), dict(schema_version=1.0), dict(schema_version=2),
    dict(method='modified_dietz'), dict(unexpected='value'),
])
def test_invalid_intervals_and_contracts_rejected(changes):
    with pytest.raises(ValidationError):
        Inputs(**structural(**changes))


@pytest.mark.parametrize('changes', [dict(event_id=True), dict(event_id=1.0), dict(event_id='1'),
                                    dict(event_id=0), dict(cash=0), dict(cash='-100'), dict(cash='-101'),
                                    dict(before=valuation('2026-11-03', '0', '12:00:00')),
                                    dict(before=valuation('2026-11-03', '100', '11:59:59')),
                                    dict(unexpected=1)])
def test_invalid_flow_boundary_rejected(changes):
    with pytest.raises(ValidationError):
        Flow(**(flow() | changes))


def test_valuation_forbids_extra_fields_and_evaluation_revalidates():
    with pytest.raises(ValidationError):
        Valuation(**(valuation('2026-11-02', '100') | dict(guessed=True)))
    inputs = Inputs(**structural())
    inputs.flows.append(Flow(**flow(day='2026-11-09')))
    with pytest.raises(ValidationError):
        evaluate(inputs)
