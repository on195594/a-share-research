"""Invented policy/ledger contracts only; these inputs are not broker acceptance evidence."""
from copy import deepcopy
from decimal import Decimal, localcontext
from types import SimpleNamespace

import pytest

from a_share_research.account.mirror import MANIFEST
from a_share_research.account.positions import PositionSeed, PositionEntry, replay
from a_share_research.account.reconciliation import Event
from a_share_research.policy.v0.account_projection import project

PROFILE = {'stops':{'A通用':['0.85','0.80'],'C资源':['0.82','0.75']},'main_entry_ratio':'0.5'}


def opening():
    row = dict.fromkeys(MANIFEST['holdings'])
    row.update(id=1,code='600001',name='Structural',shares=100,cost_price=10,buy_date='2026-10-01',
        framework='C资源',framework_confident=1,initial_shares=100,reference_cost=10,
        main_entry_date='2026-10-01',main_entry_basis=1000,additions_since_main=0,
        stop_loss_15=8.5,stop_loss_20=8,buy_score=50,notes='Structural original note',updated_at='2026-10-01')
    return row


def ev(kind='buy', day=2, **values):
    return Event.model_validate(dict(kind=kind,code='600001',occurred_at=f'2026-11-{day:02}T10:00:00+08:00',**values))


def entry(framework='A通用', identity='v0'):
    return SimpleNamespace(policy_identity=identity,name='Structural new',framework=framework,framework_confident=True)


def run(events, *, seeded=True, entries=None, distributions=None, opening_row=None, dividend_cycles=None):
    row=opening_row or opening()
    seeds=[PositionSeed(cycle_id='old',code=row['code'],shares=row['shares'],cost_price=row['cost_price'],
        buy_date=row['buy_date'],legacy_holding_id=1,policy_identity='legacy-snapshot')] if seeded else []
    entries=entries or {}
    numeric_entries={key:PositionEntry(policy_identity=value.policy_identity,name=value.name) for key,value in entries.items()}
    economic=replay(seeds,events,numeric_entries,dividend_cycles=dividend_cycles)
    records=[dict(business_id=key,event=value,created_at=value.occurred_at.isoformat()) for key,value in events]
    cycle_ids={cycle['cycle_id']:i+1 for i,cycle in enumerate(economic['cycles'])}
    event_ids={key:100+i for i,(key,event) in enumerate(events) if event.code is not None}
    result=project({'old':row} if seeded else {},economic,records,entries,{'v0':PROFILE},'v0',cycle_ids,event_ids,distributions)
    return result,economic


def test_seed_without_events_preserves_historical_stop_and_fields():
    row=opening();before=deepcopy(row)
    result,_=run([],opening_row=row)
    assert result['holdings']==[before] and result['holding_events']==[] and row==before
    assert result['holdings'][0]['stop_loss_15']==8.5  # Current C factor would give 8.2.


def test_buy_reference_excludes_fees_and_main_entry_threshold_is_strict():
    result,_=run([('half',ev(shares=50,price='10'))])
    row=result['holdings'][0]
    assert row['main_entry_date']=='2026-10-01' and row['additions_since_main']==500
    result,_=run([('half',ev(shares=50,price='10')),('cross',ev(day=3,shares=1,price='10',fees='2'))])
    row=result['holdings'][0]
    assert row['reference_cost']==10 and row['cost_price']==pytest.approx(1512/151)
    assert row['main_entry_date']=='2026-11-03' and row['main_entry_basis']==1512
    assert row['additions_since_main']==0 and row['stop_loss_15']==8.2
    assert row['name']=='Structural' and row['buy_score']==50 and row['notes']=='Structural original note'


def test_sell_and_cash_dividend_preserve_reference_and_historical_stops():
    result,_=run([('sale',ev('sell',shares=40,price='12',fees='1',tax='2')),
                  ('div',ev('dividend',day=3,cash='20'))])
    row=result['holdings'][0]
    assert row['shares']==60 and row['cost_price']==row['reference_cost']==10
    assert row['stop_loss_15']==8.5 and row['stop_loss_20']==8
    sold,div=result['holding_events']
    assert sold['realized_pnl']==77 and sold['fees']==1 and sold['tax']==2
    assert div['cash_amount']==20 and div['shares'] is None and div['price'] is None


def test_split_and_explicit_dps_follow_distinct_cash_and_policy_inputs():
    events=[('cash',ev('dividend',cash='17')),('split',ev('adjustment',day=3,shares=100))]
    result,_=run(events,distributions={'split':Decimal('0.2')})
    row=result['holdings'][0]
    assert row['shares']==row['initial_shares']==200
    assert row['cost_price']==5 and row['reference_cost']==4.9 and row['stop_loss_15']==4.018
    assert result['holding_events'][0]['cash_amount']==17
    assert result['holding_events'][1]['price']==4.9 and result['holding_events'][1]['notes']=='dps=0.2;split_ratio=1'
    assert row['main_entry_basis']==1000 and row['additions_since_main']==0


def test_explicit_cash_only_dps_reprices_reference_without_changing_economic_cost():
    result,_=run([('cash',ev('dividend',cash='17'))],distributions={'cash':Decimal('0.2')})
    row=result['holdings'][0]
    assert row['reference_cost']==9.8 and row['cost_price']==10
    assert result['holding_events'][0]['notes']=='dps=0.2;split_ratio=0'


def test_new_cycle_requires_entry_framework_and_bound_profile():
    events=[('new',ev(shares=100,price='10',fees='2'))]
    result,_=run(events,seeded=False,entries={'new':entry()})
    row=result['holdings'][0]
    assert set(row)==set(MANIFEST['holdings']) and row['name']=='Structural new'
    assert row['cost_price']==10.02 and row['reference_cost']==10 and row['main_entry_basis']==1002
    assert row['framework_confident']==1 and row['buy_score'] is None
    assert set(result['holding_events'][0])==set(MANIFEST['holding_events'])
    with pytest.raises(ValueError,match='明确框架'):
        run(events,seeded=False,entries={'new':entry(None)})
    with pytest.raises(ValueError,match='缺少已绑定政策'):
        run(events,seeded=False,entries={'new':entry(identity='unknown')})


def test_close_then_new_cycle_uses_separate_ids_and_old_cash_dividend_binding():
    events=[('close',ev('sell',shares=100,price='11')),('new',ev(day=3,shares=100,price='12')),
            ('late',ev('dividend',day=4,cash='10'))]
    result,_=run(events,entries={'new':entry()},dividend_cycles={'late':'old'})
    old,new=result['holdings']
    assert old['exit_date']=='2026-11-02' and old['exit_price']==11 and old['shares']==0
    assert new['id']!=old['id'] and new['exit_date'] is None and new['shares']==100
    assert result['holding_events'][2]['holding_id']==old['id']
    assert old['stop_loss_15']==8.5


@pytest.mark.parametrize('distribution', [{'missing':Decimal(1)},{'buy':Decimal(1)},{'cash':Decimal(-1)},{'cash':Decimal(10)}])
def test_invalid_distribution_context_rejected(distribution):
    with pytest.raises(ValueError):
        run([('buy',ev(shares=1,price='10')),('cash',ev('dividend',day=3,cash='10'))],distributions=distribution)


def test_explicit_dps_cannot_rewrite_reference_after_close():
    with pytest.raises(ValueError,match='明确在仓基准'):
        run([('close',ev('sell',shares=100,price='11')),('late',ev('dividend',day=3,cash='10'))],
            dividend_cycles={'late':'old'},distributions={'late':Decimal('.1')})


def test_cash_only_records_do_not_generate_legacy_stock_events():
    events=[('deposit',Event(kind='deposit',cash='10',occurred_at='2026-11-02T10:00:00+08:00'))]
    result,_=run(events)
    assert result['holdings']==[opening()] and result['holding_events']==[]


def test_missing_or_duplicate_integer_mapping_is_rejected():
    economic=replay([PositionSeed(cycle_id='old',code='600001',shares=100,cost_price='10',buy_date='2026-10-01',policy_identity='legacy')],[],{})
    with pytest.raises(ValueError,match='有效稳定编号'):
        project({'old':opening()},economic,[],{}, {'v0':PROFILE},'v0',{}, {})
    event=ev('sell',shares=1,price='11')
    economic=replay([PositionSeed(cycle_id='old',code='600001',shares=100,cost_price='10',buy_date='2026-10-01',policy_identity='legacy')],[('sell',event)],{})
    with pytest.raises(ValueError,match='有效稳定编号'):
        project({'old':opening()},economic,[dict(business_id='sell',event=event,created_at='2026-11-02')],{}, {'v0':PROFILE},'v0',{'old':1}, {})


def test_arithmetic_is_independent_of_ambient_decimal_precision():
    events=[('add',ev(shares=3,price='11.23',fees='.13'))]
    normal,_=run(events)
    with localcontext() as context:
        context.prec=6
        low,_=run(events)
    assert low==normal
