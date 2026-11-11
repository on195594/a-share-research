"""Replay captured monitor evidence; boundary variants are not financial evidence."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from a_share_research import gate
from a_share_research.policy import position_evaluation, position_request
from a_share_research.policy.v0 import monitor_research, position_size

FIXTURES = Path(__file__).parent / 'policy_v0/fixtures/monitor'


@pytest.fixture
def snapshot():
    return json.loads((FIXTURES / 'agent-behavior-snapshots.json').read_text())['no_action']


def bundle(snapshot, **proposal):
    return dict(risk_policy=deepcopy(snapshot['account']['risk_policy']), snapshot=deepcopy(snapshot),
                monitor_evaluation=dict(id=1, input_hash='a' * 64, result_hash=gate.digest(snapshot)),
                proposal=dict(code='600900', price=29.5, framework='A', cash=None) | proposal)


def evaluate(value):
    return position_evaluation.evaluate(value, value['risk_policy'])


def test_exact_original_cli_stdout_and_immutability(snapshot, tmp_path, capsys):
    path = tmp_path / 'captured.json'
    path.write_text(json.dumps(snapshot))
    assert position_size.cmd_position_size(['--snapshot', str(path), '--code', '600900', '--price', '29.5', '--framework', 'A']) == 0
    legacy = capsys.readouterr()
    value = bundle(snapshot)
    original = deepcopy(value)
    result = evaluate(value)
    assert value == original
    assert result['status'] == 'ready' and result['exit_code'] == 0
    assert result['stdout'] == legacy.out and result['stderr'] == legacy.err == ''
    sizing = result['sizing']
    assert sizing == json.loads(legacy.out)
    assert sizing['max_shares'] == 300
    assert [t['cumulative_shares'] for t in sizing['tranches']] == [100, 200, 300]
    assert sizing['action_status'] == 'not_formed' and sizing['execution_status'] == 'not_checked'


def test_existing_risk_cash_and_board_lots(snapshot):
    existing = evaluate(bundle(snapshot, code='600000', price=12.3, framework='B'))['sizing']
    assert existing['existing_position_stop_risk'] == 300
    assert existing['limits']['single_stock_remaining'] == 1700
    assert evaluate(bundle(snapshot, code='688578', price=83.))['sizing']['max_shares'] == 0
    # Explicit policy/cash boundary variants of the recorded account.
    snapshot['account']['risk_policy'].update(policy_id='personal', max_position_risk_pct=6., max_portfolio_risk_pct=18.)
    star = evaluate(bundle(snapshot, code='688578', price=83.))['sizing']
    assert star['max_shares'] == 361
    assert [t['cumulative_shares'] for t in star['tranches']] == [0, 240, 361]
    cash = evaluate(bundle(snapshot, cash=5000.))['sizing']
    assert cash['binding_limit'] == 'cash' and cash['max_shares'] == 100
    assert [t['cumulative_shares'] for t in cash['tranches']] == [0, 0, 100]
    assert evaluate(bundle(snapshot, cash=0.))['sizing']['max_shares'] == 0


@pytest.mark.parametrize('variant', ['denominator', 'permission', 'rounded_stop'])
def test_valid_snapshot_unavailable_matches_cli(snapshot, tmp_path, capsys, variant):
    proposal = {}
    if variant == 'denominator':
        snapshot['account']['denominator_requires_review'] = True
    elif variant == 'permission':
        snapshot['account']['risk_policy']['untradable_boards'] = ['STAR']
        snapshot['account']['risk_policy']['field_sources']['untradable_boards'] = 'policy_file'
        proposal = dict(code='688578', price=83.)
    else:
        proposal = dict(price=0.001)
    value = bundle(snapshot, **proposal)
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(snapshot))
    assert position_size.cmd_position_size(['--snapshot', str(path), '--code', value['proposal']['code'],
                                           '--price', str(value['proposal']['price']), '--framework', 'A']) == 1
    old = capsys.readouterr()
    assert evaluate(value) == dict(status='unavailable', exit_code=1, stdout='', stderr=old.err, sizing=None)
    assert old.out == '' and old.err.startswith('定仓不可计算：')


@pytest.mark.parametrize('code', ['300750', '002946', '002648'])
def test_captured_research_checkpoint_semantics(snapshot, code):
    cards = json.loads((FIXTURES / 'reference-evidence-cards.json').read_text())
    card = next(c for c in cards['cards'] if c['code'] == code)
    research, prompts, _ = monitor_research.summarize({'status': 'ok', 'cards': [card], 'missing': []}, ['600000'], date(2026, 11, 4))
    snapshot.update(research=research, review_prompts=prompts)
    result = evaluate(bundle(snapshot, code=code, price=card['current']['last_close']))['sizing']
    if code == '002648':
        assert '加仓检查点' not in result['note']
    else:
        assert f"首批 tranches[0]：{result['tranches'][0]['cumulative_shares']} 股" in result['note']
        assert '参考，不阻止首批' in result['note']


@pytest.mark.parametrize('proposal', [dict(code='１２３４５６'), dict(code='12345'), dict(code='600900\n'),
    dict(price=True), dict(price='29.5'), dict(price=0), dict(price=float('inf')), dict(framework='a'),
    dict(framework='X'), dict(cash=-1), dict(cash=True), dict(cash=float('nan')), dict(extra=True)])
def test_proposal_strict_boundary(snapshot, proposal):
    with pytest.raises(ValueError):
        evaluate(bundle(snapshot, **proposal))


def test_proposal_canonical_null_default():
    assert position_request.validate_proposal(dict(code='600900', price=29.5, framework='A')) == dict(code='600900', price=29.5, framework='A', cash=None)


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(extra=True),
    lambda b: b['monitor_evaluation'].update(id=True),
    lambda b: b['monitor_evaluation'].update(id=0),
    lambda b: b['monitor_evaluation'].update(input_hash='A'*64),
    lambda b: b['monitor_evaluation'].update(result_hash='b'*64),
    lambda b: b['monitor_evaluation'].update(extra=True),
    lambda b: b['snapshot'].update(version='wrong'),
    lambda b: b['snapshot']['account'].update(portfolio_value=123456.),
    lambda b: b['snapshot']['account']['risk_policy'].update(policy_id='different'),
    lambda b: b['risk_policy'].update(policy_id='different'),
])
def test_tampering_rejected_before_sizing(snapshot, mutation):
    value = bundle(snapshot)
    mutation(value)
    with pytest.raises(ValueError):
        evaluate(value)


def test_parameters_must_match_registered_policy(snapshot):
    value = bundle(snapshot)
    parameters = deepcopy(value['risk_policy'])
    parameters['policy_id'] = 'other'
    with pytest.raises(ValueError, match='registered parameters'):
        position_evaluation.evaluate(value, parameters)
