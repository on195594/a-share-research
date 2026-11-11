"""Invented structural bookkeeping cases; these numbers are not investment evidence."""
from datetime import date
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from a_share_research.account.lifecycle import Movement, replay


def event(identity, kind, **values):
    return Movement(id=identity, kind=kind, day='2026-11-11', **values)


def test_weighted_cost_partial_sale_addition_and_separate_dividends():
    result = replay([
        event(1, 'buy', shares=100, price='10', fees='5', tax='1'),
        event(2, 'sell', shares=40, price='12', fees='2', tax='1'),
        event(3, 'buy', shares=40, price='9', fees='4'),
        event(4, 'dividend', cash='30', inferred=True),
    ])
    assert result.shares == 100 and result.initial_shares == 100
    assert result.cost_price == Decimal('9.676')
    assert result.invested == Decimal('1370')
    assert result.sale_cash == Decimal('477')
    assert result.realized_pnl == Decimal('74.60')
    assert result.dividends == Decimal('30') and result.contains_inferred
    assert result.exit_date is None and result.exit_price is None
    assert result.steps[1].before_cost == result.steps[1].after_cost == Decimal('10.06')
    assert result.steps[-1].before_cost == result.steps[-1].after_cost


def test_split_and_reverse_split_conserve_book_cost_and_round_initial_shares():
    result = replay([
        event(1, 'buy', shares=3, price='10'),
        event(2, 'buy', shares=3, price='10'),
        event(3, 'adjustment', shares=3, price='999'),
        event(4, 'adjustment', shares=-3),
    ])
    assert result.shares == 6 and result.cost_price == Decimal('10')
    assert result.initial_shares == 3  # 3 * 1.5 = 4.5 -> 4, then 4 / 1.5 -> 3.
    assert result.steps[2].after_shares == 9
    assert result.steps[2].after_cost * 9 == Decimal('60.00000000000000000000000000')
    assert result.invested == Decimal('60') and not result.dividends


def test_policy_only_adjustment_ignores_reference_price_and_keeps_cost():
    result = replay([event(1, 'buy', shares=100, price='10'), event(2, 'adjustment', price='9')])
    assert result.cost_price == 10 and result.shares == 100
    assert result.invested == 1000 and result.realized_pnl == 0


def test_full_sale_closes_cycle_and_late_dividend_keeps_exit_date():
    result = replay([event(1, 'buy', shares=100, price='10'),
                     event(2, 'sell', shares=100, price='8', fees='3', tax='2'),
                     event(3, 'dividend', cash='20')])
    assert result.shares == 0 and result.cost_price == 10
    assert result.exit_date == date(2026, 11, 11) and result.exit_price == 8
    assert result.realized_pnl == -205 and result.dividends == 20
    assert result.sale_cash + result.dividends - result.invested == -185


def test_order_is_recorded_date_then_id_and_inputs_remain_unchanged():
    first = event(5, 'buy', shares=10, price='10').model_copy(update={'day': date(2026, 11, 10)})
    second = event(2, 'sell', shares=5, price='11')
    last = event(3, 'sell', shares=5, price='12')
    inputs = [last, first, second]
    result = replay(inputs)
    assert [step.movement.id for step in result.steps] == [5, 2, 3]
    assert [item.id for item in inputs] == [3, 5, 2]
    assert result.buy_date == date(2026, 11, 10) and result.realized_pnl == 15


@pytest.mark.parametrize('inputs,match', [
    ([], '缺少'),
    ([event(1, 'dividend', cash=1)], '从买入'),
    ([event(1, 'buy', shares=1, price=1), event(1, 'adjustment')], '编号重复'),
    ([event(1, 'buy', shares=1, price=1), event(2, 'sell', shares=2, price=1)], '超过'),
    ([event(1, 'buy', shares=1, price=1), event(2, 'adjustment', shares=-1)], '前后'),
    ([event(1, 'buy', shares=1, price=1), event(2, 'sell', shares=1, price=1), event(3, 'buy', shares=1, price=1)], '新生命周期'),
    ([event(1, 'buy', shares=1, price=1), event(2, 'sell', shares=1, price=1), event(3, 'adjustment', shares=1)], '前后'),
])
def test_incomplete_or_inconsistent_cycles_are_rejected(inputs, match):
    with pytest.raises(ValueError, match=match):
        replay(inputs)


@pytest.mark.parametrize('kind,values', [
    ('buy', dict(shares=True, price=1)),
    ('buy', dict(shares=1, price=True)),
    ('buy', dict(shares=1, price='NaN')),
    ('buy', dict(shares=1, price=1, cash=1)),
    ('sell', dict(shares=0, price=1)),
    ('sell', dict(shares=1, price=1, fees=-1)),
    ('dividend', dict(cash=-1)),
    ('dividend', dict(cash=1, shares=1)),
    ('adjustment', dict(shares=1, cash=1)),
    ('adjustment', dict(shares=1, fees=1)),
    ('adjustment', dict(inferred=1)),
])
def test_movement_shape_rejects_invalid_amounts_and_boolean_coercion(kind, values):
    with pytest.raises(ValidationError):
        event(1, kind, **values)


def test_decimal_context_is_local_and_negative_sale_net_is_visible():
    movements = [event(1, 'buy', shares=3, price='1', fees='1'),
                 event(2, 'sell', shares=1, price='0.1', fees='1')]
    expected = replay(movements)
    with localcontext() as context:
        context.prec = 4
        actual = replay(movements)
        assert actual == expected
        assert context.prec == 4
    assert actual.sale_cash == Decimal('-0.9')
