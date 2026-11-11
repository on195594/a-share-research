"""Structural position cases only; no broker or investment evidence."""
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from a_share_research.account.positions import MissingBasis, PositionEntry, PositionSeed, replay
from a_share_research.account.reconciliation import Event


def seed(**overrides):
    return PositionSeed.model_validate(dict(cycle_id='legacy:7',code='600001',shares=100,cost_price='10',
        buy_date='2026-01-01',legacy_holding_id=7,policy_identity='structural:policy') | overrides)


def entry():
    return PositionEntry(policy_identity='structural:new-policy',name='Structural fixture')


def event(kind, day='2026-11-11', **overrides):
    values = dict(kind=kind,occurred_at=day+'T10:00:00+08:00')
    if kind in ('buy','sell','dividend','adjustment'):
        values['code'] = '600001'
    return Event.model_validate(values | overrides)


def test_seed_keeps_explicit_cost_and_does_not_invent_historical_cashflows():
    result = replay([seed()],[],{})
    cycle = result['cycles'][0]
    assert result['positions'] == {'600001':100}
    assert cycle['cost_price'] == '10' and cycle['legacy_holding_id'] == 7
    assert cycle['policy_identity'] == 'structural:policy'
    assert all(Decimal(cycle[field]) == 0 for field in ('invested_since_opening','sale_cash_since_opening',
        'dividends_since_opening','realized_pnl_since_opening'))
    assert result['cash_flow_scope'] == 'since_opening'


def test_seeded_partial_sale_buy_dividend_split_preserves_economic_cost():
    result = replay([seed()],[('sell',event('sell',shares=20,price='12',fees='1',tax='1')),
        ('buy',event('buy',shares=20,price='20',fees='10',tax='2')),
        ('dividend',event('dividend',cash='30')),('split',event('adjustment',shares=100))],{})
    cycle = result['cycles'][0]
    assert result['positions'] == {'600001':200}
    assert cycle['cost_price'] == '6.06'
    assert cycle['realized_pnl_since_opening'] == '38'
    assert cycle['invested_since_opening'] == '412'
    assert cycle['sale_cash_since_opening'] == '238'
    assert cycle['dividends_since_opening'] == '30'
    assert result['events'][0]['realized_pnl'] == '38'
    assert {r['cycle_id'] for r in result['events']} == {'legacy:7'}


def test_close_and_reopen_has_stable_new_identity_and_explicit_policy():
    result = replay([seed()],[('close',event('sell',shares=100,price='12')),
                             ('reopen',event('buy','2026-11-12',shares=50,price='9',fees='5'))],{'reopen':entry()})
    old,new = result['cycles']
    assert old['shares'] == 0 and old['exit_date'] == '2026-11-11' and old['exit_price'] == '12'
    assert new['cycle_id'] == 'event:reopen' and new['legacy_holding_id'] is None
    assert new['policy_identity'] == 'structural:new-policy' and new['cost_price'] == '9.1'
    assert result['positions'] == {'600001':50}


def test_new_opening_requires_metadata():
    with pytest.raises(MissingBasis,match='政策身份'):
        replay([],[('buy',event('buy',shares=100,price='10'))],{})


@pytest.mark.parametrize('records,entries',[
    ([('buy',event('buy',shares=1,price='10'))],{'buy':entry()}),
    ([('sell',event('sell',shares=1,price='10'))],{'sell':entry()}),
    ([('cash',event('deposit',cash='10'))],{'cash':entry()}),
    ([],{'missing':entry()}),
])
def test_entry_metadata_must_not_be_silently_discarded(records,entries):
    with pytest.raises(ValueError,match='开仓元数据'):
        replay([seed()],records,entries)


@pytest.mark.parametrize('records',[
    [('oversell',event('sell',shares=101,price='10'))],
    [('zero',event('adjustment',shares=-100))],
    [('negative',event('adjustment',shares=-101))],
    [('early',event('sell','2025-12-31',shares=1,price='10'))],
    [('close',event('sell',shares=100,price='10')),('late_dividend',event('dividend',cash='1'))],
])
def test_impossible_or_ambiguous_cycle_changes_fail(records):
    with pytest.raises(ValueError):
        replay([seed()],records,{})


def test_cash_events_do_not_create_cycles_and_stock_without_cycle_fails():
    result = replay([],[('deposit',event('deposit',cash='100')),('fee',event('fee',cash='1'))],{})
    assert result['cycles'] == result['events'] == [] and result['positions'] == {}
    for action in (event('sell',shares=1,price='10'),event('dividend',cash='1'),event('adjustment',shares=1)):
        with pytest.raises(ValueError,match='活动生命周期'):
            replay([],[('stock',action)],{})


def test_same_time_input_order_and_chronological_sort_are_preserved():
    sell=event('sell','2026-11-12',shares=100,price='10')
    buy=event('buy','2026-11-11',shares=100,price='10')
    result=replay([],[('sell',sell),('buy',buy)],{'buy':entry()})
    assert [r['business_id'] for r in result['events']] == ['buy','sell']
    with pytest.raises(ValueError,match='活动生命周期'):
        replay([],[('sell',event('sell',shares=100,price='10')),('buy',buy)],{'buy':entry()})


def test_duplicate_seed_ids_codes_and_business_ids_are_rejected():
    for second in (seed(cycle_id='different',legacy_holding_id=8),seed(code='600002',legacy_holding_id=8),
                   seed(code='600002',cycle_id='different')):
        with pytest.raises(ValueError,match='重复'):
            replay([seed(),second],[],{})
    with pytest.raises(ValueError,match='业务身份重复'):
        replay([seed()],[('dup',event('dividend',cash='1'))]*2,{})
    with pytest.raises(ValueError,match='身份与已有周期冲突'):
        replay([seed(cycle_id='event:reopen')],[('close',event('sell',shares=100,price='10')),
            ('reopen',event('buy',shares=100,price='10'))],{'reopen':entry()})


def test_fixed_decimal_context_and_inputs_remain_unchanged():
    opening=seed();buy=event('buy',shares=1,price='11')
    with localcontext() as context:
        context.prec=6
        a=replay([opening],[('buy',buy)],{})
    with localcontext() as context:
        context.prec=40
        b=replay([opening],[('buy',buy)],{})
    assert a==b
    assert opening.shares==100 and opening.cost_price==Decimal('10') and buy.shares==1


@pytest.mark.parametrize('overrides',[{'shares':True},{'shares':0},{'cost_price':0},{'cost_price':'NaN'},
                                     {'cycle_id':' '},{'policy_identity':''}])
def test_invalid_opening_basis_is_rejected(overrides):
    with pytest.raises(ValidationError):
        seed(**overrides)



def reopened_records():
    return [('close',event('sell',shares=100,price='12')),
            ('reopen',event('buy','2026-11-12',shares=50,price='9')),
            ('dividend',event('dividend','2026-11-13',cash='20'))]


def test_dividend_after_reopening_requires_explicit_cycle():
    with pytest.raises(MissingBasis,match='明确指定周期'):
        replay([seed()],reopened_records(),{'reopen':entry()})


def test_dividend_can_pay_closed_cycle_without_changing_live_position():
    result=replay([seed()],reopened_records(),{'reopen':entry()},dividend_cycles={'dividend':'legacy:7'})
    old,new=result['cycles']
    assert old['shares']==0 and old['exit_date']=='2026-11-11' and old['exit_price']=='12'
    assert old['dividends_since_opening']=='20' and new['dividends_since_opening']=='0'
    assert result['positions']=={'600001':50}
    assert result['events'][-1]['cycle_id']=='legacy:7'
    assert result['events'][-1]['before_shares']==result['events'][-1]['after_shares']==0


def test_explicit_dividend_to_new_cycle_is_also_preserved():
    result=replay([seed()],reopened_records(),{'reopen':entry()},dividend_cycles={'dividend':'event:reopen'})
    old,new=result['cycles']
    assert old['dividends_since_opening']=='0' and new['dividends_since_opening']=='20'


def test_dividend_to_closed_only_cycle_requires_binding():
    records=reopened_records()[::2]
    with pytest.raises(MissingBasis):
        replay([seed()],records,{})
    result=replay([seed()],records,{},dividend_cycles={'dividend':'legacy:7'})
    assert result['positions']=={} and result['cycles'][0]['dividends_since_opening']=='20'


@pytest.mark.parametrize('bindings',[
    {'missing':'legacy:7'}, {'close':'legacy:7'}, {'reopen':'legacy:7'},
    {'dividend':'missing'}, {'dividend':''},
])
def test_dividend_binding_must_name_existing_dividend_and_cycle(bindings):
    with pytest.raises(ValueError,match='分红归属'):
        replay([seed()],reopened_records(),{'reopen':entry()},dividend_cycles=bindings)


def test_dividend_binding_rejects_wrong_company_and_pre_buy_date():
    extra=seed(code='600002',cycle_id='legacy:8',legacy_holding_id=8)
    with pytest.raises(ValueError,match='公司不匹配'):
        replay([seed(),extra],reopened_records(),{'reopen':entry()},dividend_cycles={'dividend':'legacy:8'})
    with pytest.raises(ValueError,match='早于'):
        replay([seed()],[('early',event('dividend','2025-12-31',cash='1'))],{},
               dividend_cycles={'early':'legacy:7'})


def test_dividend_cannot_bind_future_unopened_cycle():
    records=[('dividend',event('dividend',cash='1')),('buy',event('buy','2026-11-12',shares=1,price='10'))]
    with pytest.raises(ValueError,match='周期不存在'):
        replay([],records,{'buy':entry()},dividend_cycles={'dividend':'event:buy'})
