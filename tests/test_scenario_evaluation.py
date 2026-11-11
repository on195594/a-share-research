"""Captured monitor fixture replay; explicit boundary variants are not financial evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from a_share_research import gate
from a_share_research.policy import scenario_evaluation, scenario_request
from a_share_research.policy.v0 import portfolio_scenario


@pytest.fixture
def snapshot():
    return json.loads((Path(__file__).parent / 'policy_v0/fixtures/monitor/agent-behavior-snapshots.json').read_text())['no_action']


def bundle(snapshot, **proposal):
    return dict(risk_policy=deepcopy(snapshot['account']['risk_policy']), snapshot=deepcopy(snapshot),
                monitor_evaluation=dict(id=1, input_hash='a'*64, result_hash=gate.digest(snapshot)),
                proposal=dict(sales={'600000': 100}, fees=5., cash=None) | proposal)


def evaluate(value):
    return scenario_evaluation.evaluate(value, value['risk_policy'])


def cli(snapshot, proposal, path, capsys):
    path.write_text(json.dumps(snapshot))
    args = ['--snapshot', str(path), '--fees', str(proposal['fees'])]
    for code, shares in proposal['sales'].items():
        args.extend(['--sell', f'{code}:{shares}'])
    if proposal.get('cash') is not None:
        args.extend(['--cash', str(proposal['cash'])])
    code = portfolio_scenario.cmd_portfolio_scenario(args)
    output = capsys.readouterr()
    return code, output


def test_exact_cli_and_no_double_counting(snapshot, tmp_path, capsys):
    value = bundle(snapshot, cash=500.)
    original = deepcopy(value)
    code, output = cli(snapshot, value['proposal'], tmp_path / 'snapshot.json', capsys)
    result = evaluate(value)
    assert code == result['exit_code'] == 0
    assert result['stdout'] == output.out and result['stderr'] == output.err == ''
    assert value == original
    scenario = result['scenario']
    assert scenario == json.loads(output.out)
    assert scenario['before']['portfolio_value'] == 100000
    assert scenario['after']['portfolio_value'] == 99995
    assert scenario['cash_delta'] == 1225 and scenario['cash_after'] == 1725
    assert scenario['after']['stock_market_value'] == 11070
    assert scenario['after']['total_stop_risk'] == pytest.approx(270)
    assert scenario['action_status'] == 'not_formed' and scenario['execution_status'] == 'not_checked'
    assert scenario['writes'] is False
    assert scenario['source_denominator']['denominator_components'] == snapshot['account']['denominator_components']


def test_full_exit_unknown_cash_preserves_source_gaps(snapshot):
    # Minimal status variant copied from the legacy scenario test; never a new account observation.
    snapshot.update(data_status='partial', action_status='review_candidate', review_status='review_required', stop_reason=None)
    snapshot['manifest']['stop_reason'] = None
    result = evaluate(bundle(snapshot, sales={'600000': 1000}, fees=0.))['scenario']
    assert result['after']['total_stop_risk'] == 0
    assert result['positions_after'][0]['shares'] == 0
    assert result['cash_before'] is None and result['cash_after'] is None
    assert result['source_data_status'] == 'partial'
    assert result['source_review_status'] == 'review_required'
    assert result['source_action_status'] == 'review_candidate'
    assert result['source_data_gaps'] == snapshot['data_gaps']
    assert result['source_escalations'] == snapshot['escalations']


@pytest.mark.parametrize('proposal', [dict(sales={'600000': 1001}), dict(sales={'600001': 100}),
                                     dict(fees=1500.), dict(fees=100000.), dict(cash=100000.)])
def test_semantic_failures_keep_old_cli_error(snapshot, tmp_path, capsys, proposal):
    value = bundle(snapshot, **proposal)
    code, output = cli(snapshot, value['proposal'], tmp_path / 'snapshot.json', capsys)
    assert code == 1
    assert evaluate(value) == dict(status='unavailable', exit_code=1, stdout='', stderr=output.err, scenario=None)
    assert output.out == '' and output.err.startswith('情景不可计算：')


def test_unconfirmed_denominator_and_inconsistent_totals_unavailable(snapshot):
    snapshot['account']['denominator_requires_review'] = True
    assert '分母' in evaluate(bundle(snapshot))['stderr']
    snapshot['account'].update(denominator_requires_review=False, total_stop_risk=500,
                               known_stop_risk_lower_bound=500, total_stop_risk_pct=0.5)
    result = evaluate(bundle(snapshot))
    assert result['status'] == 'unavailable' and '汇总' in result['stderr']


def test_snapshot_time_preserved_stale_quote_rejected(snapshot):
    result = evaluate(bundle(snapshot))['scenario']
    assert result['snapshot_as_of'] == snapshot['as_of']
    snapshot['holdings'][0]['quote']['status'] = 'stale'
    # This minimal corruption leaves clean_fast_gate inconsistent with quote freshness.
    with pytest.raises(ValueError, match='fresh holding quotes'):
        evaluate(bundle(snapshot))



@pytest.mark.parametrize('proposal', [dict(sales={}), dict(sales={'６０００００': 100}), dict(sales={'600000\n': 100}),
    dict(sales={'600000': True}), dict(sales={'600000': 0}), dict(sales={'600000': -1}),
    dict(sales={'600000': 1.5}), dict(sales={'600000': '100'}), dict(fees=-1), dict(fees=True),
    dict(fees=float('nan')), dict(fees='0'), dict(cash=-1), dict(cash=True), dict(cash=float('inf')), dict(extra=True)])
def test_strict_proposal(snapshot, proposal):
    with pytest.raises(ValueError):
        evaluate(bundle(snapshot, **proposal))


def test_fees_explicit_and_cash_default_null():
    with pytest.raises(ValueError):
        scenario_request.validate_proposal(dict(sales={'600000': 100}))
    assert scenario_request.validate_proposal(dict(sales={'600000': 100}, fees=0.)) == dict(sales={'600000': 100}, fees=0., cash=None)


@pytest.mark.parametrize('mutation', [lambda b: b.update(extra=True),
    lambda b: b['monitor_evaluation'].update(id=True), lambda b: b['monitor_evaluation'].update(input_hash='A'*64),
    lambda b: b['monitor_evaluation'].update(result_hash='b'*64),
    lambda b: b['snapshot'].update(version='wrong'),
    lambda b: b['snapshot']['account'].update(portfolio_value=123456.),
    lambda b: b['snapshot']['account']['risk_policy'].update(policy_id='different'),
    lambda b: b['risk_policy'].update(policy_id='different')])
def test_bad_provenance_rejected(snapshot, mutation):
    value = bundle(snapshot)
    mutation(value)
    with pytest.raises(ValueError):
        evaluate(value)


def test_registered_parameters_must_match(snapshot):
    value = bundle(snapshot)
    parameters = deepcopy(value['risk_policy']) | {'policy_id': 'other'}
    with pytest.raises(ValueError, match='registered parameters'):
        scenario_evaluation.evaluate(value, parameters)
