"""Holdings check and state-machine tests, not market or investment evidence.

The market snapshot provider currently returns None. The 500/501 values below
are explicitly synthetic boundaries, not observations of a liquidity shock.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json

import pytest

from a_share_research.policy import holdings_check_evaluation
from a_share_research.policy.v0 import risk_budget
from a_share_research.policy.v0.market_quotes import PriceQuote

NOW = '2026-09-07T11:00:00+08:00'
START = '2026-09-07T10:00:00+08:00'
DUE = '2026-09-08T10:00:00+08:00'


def bundle(price=40.):
    return dict(risk_policy=risk_budget.policy(), now=NOW,
                holdings=[[1, '600036', '招商银行', 40., 34., 32.]], defers=[],
                quotes={'600036': asdict(PriceQuote(price, '2026-09-07', '11:00:00', 'sina', False, False, 'trading'))},
                market_snapshot=None)


def active(value):
    value['defers'] = [[1, json.dumps(dict(defer_started_at=START, review_due=DUE, original_price=31, original_stop_loss_20=32)), DUE]]
    return value


def evaluate(value):
    return holdings_check_evaluation.evaluate(value, value['risk_policy'])


def market(value, count):
    value['market_snapshot'] = dict(limit_down_count=count, universe=5000, eligible_count=4800,
                                  coverage='full', status='final', source='official exchange', as_of=value['now'], is_final=True)
    return value


@pytest.mark.parametrize('deferred', [False, True])
def test_normal_and_deferred_checks_use_the_captured_stop_lines(deferred):
    value = active(bundle(31.)) if deferred else bundle()
    original = deepcopy(value)
    result = evaluate(value)
    assert result['status'] == 'ready' and value == original
    check, = result['checks']
    assert check['is_alert'] is deferred
    assert check['p3']['status'] == ('deferred' if deferred else 'not_applicable')
    assert '招商银行(600036)' in result['stdout']
    assert ('暂不可执行' in result['stdout']) is deferred


def test_no_holdings_keeps_closed_defers_without_fetch_inputs():
    value = bundle()
    value.update(holdings=[], quotes={}, defers=[[99, 'malformed', DUE]])
    assert evaluate(value) == dict(status='no_holdings', exit_code=0, stdout='暂无持仓\n', stderr='', checks=[])


def test_old_timestamp_and_missing_quote_remain_observations():
    value = bundle(31.)
    value['quotes']['600036']['quote_date'] = '2026-09-04'
    result = evaluate(value)
    assert '上一交易日收盘价观察提醒' in result['stdout']
    assert result['checks'][0]['is_alert'] is False
    value['quotes']['600036'] = None
    result = evaluate(value)
    assert '无实时价格' in result['stdout'] and result['checks'][0]['is_alert'] is False
    assert evaluate(active(value))['checks'][0]['p3']['reason_code'] == 'quote_or_stop_line_missing'


@pytest.mark.parametrize('price,locked,reason', [(31., False, 'stop_loss_20_reconfirmed'),
    (33., False, 'stop_loss_20_recovered'), (31., True, 'review_quote_untradeable')])
def test_persisted_defer_at_24_hours(price, locked, reason):
    value = active(bundle(price))
    value['now'] = DUE
    value['quotes']['600036'].update(quote_date='2026-09-08', quote_time='10:00:00', limit_down_locked=locked)
    result = evaluate(value)['checks'][0]['p3']
    assert result['reason_code'] == reason
    assert result['defer_started_at'] == START
    assert result['action_eligible'] is (reason == 'stop_loss_20_reconfirmed')


def test_review_due_fallback_and_malformed_evidence(monkeypatch):
    from a_share_research.policy.v0 import risk_gates
    original = risk_gates.liquidity_shock_gate
    seen = []
    def record(payload):
        seen.append(payload['existing_defer'])
        return original(payload)
    monkeypatch.setattr(risk_gates, 'liquidity_shock_gate', record)
    value = bundle(31.)
    value['defers'] = [[1, json.dumps({'defer_started_at': START}), DUE]]
    assert evaluate(value)['checks'][0]['p3']['status'] == 'deferred'
    assert seen[-1] == dict(defer_started_at=START, review_due=DUE)
    for evidence in ('malformed', '[]', None):
        value['defers'] = [[1, evidence, DUE]]
        assert evaluate(value)['checks'][0]['p3']['status'] == 'incomplete'
    assert seen[-1] == {'review_due': DUE}


def test_structural_market_boundary_not_live_evidence():
    assert evaluate(market(bundle(31.), 500))['checks'][0]['p3']['status'] == 'clear'
    assert evaluate(market(bundle(31.), 501))['checks'][0]['p3']['status'] == 'deferred'
    assert evaluate(bundle(31.))['checks'][0]['p3']['status'] == 'incomplete'


def test_explicit_timezone_conversion():
    value = bundle()
    expected = evaluate(value)
    value['now'] = datetime.fromisoformat(NOW).astimezone(timezone.utc).isoformat()
    assert evaluate(value) == expected


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(fundamentals={'600036': None}),
    lambda b: b['holdings'].append(deepcopy(b['holdings'][0])),
    lambda b: b['holdings'][0].__setitem__(3, float('nan')),
])
def test_invalid_boundary(mutation):
    value = bundle()
    mutation(value)
    with pytest.raises(ValueError):
        evaluate(value)


def test_policy_identity_effectiveness_and_holding_order():
    value = bundle()
    with pytest.raises(ValueError, match='registered parameters'):
        holdings_check_evaluation.evaluate(value, value['risk_policy'] | {'policy_id':'other'})
    value['risk_policy'].update(effective_from='2026-09-08T00:00:00+08:00', confirmed_at=START)
    with pytest.raises(ValueError, match='not effective'):
        evaluate(value)
    value = bundle()
    value['holdings'].append([2,'000001',None,10.,8.5,8.])
    value['quotes']['000001'] = None
    with pytest.raises(ValueError, match='ordered'):
        evaluate(value)

@pytest.mark.parametrize('cost', [None, 0., -10.])
def test_unused_nullable_cost_preserves_output(cost):
    value = bundle()
    expected = evaluate(value)
    value['holdings'][0][3] = cost
    assert evaluate(value) == expected


def test_missing_quote_with_nullable_stops_preserves_legacy_observation():
    value = bundle()
    value['quotes']['600036'] = None
    expected = evaluate(value)
    value['holdings'][0][3:] = [None, None, None]
    assert evaluate(value) == expected
    assert '无实时价格' in expected['stdout']


@pytest.mark.parametrize('stop15,stop20', [(0., 0.), (-1., -2.)])
def test_zero_negative_stops_preserve_legacy_format(stop15, stop20):
    # Nullable/nonpositive stored fields are structural schema variants, not policy recommendations.
    value = bundle()
    value['holdings'][0][4:] = [stop15, stop20]
    result = evaluate(value)
    assert f'{stop15:.3f}' in result['stdout'] and f'{stop20:.3f}' in result['stdout']
    assert result['checks'][0]['is_alert'] is False


def test_fresh_quote_with_missing_stop_rejects_uncalculable_input():
    value = bundle()
    value['holdings'][0][4] = None
    with pytest.raises(ValueError, match='legacy calculation'):
        evaluate(value)


def test_json_evidence_nonfinite_result_rejected_at_evaluation_boundary():
    value = active(bundle(31.))
    value['defers'][0][1] = json.dumps(dict(defer_started_at=START, original_price=float('nan')))
    with pytest.raises(ValueError, match='finite JSON'):
        evaluate(value)
