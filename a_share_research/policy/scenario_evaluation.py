"""Pure static sell comparison from a verified saved monitor snapshot."""
from copy import deepcopy
import json

from pydantic import BaseModel, ConfigDict

from a_share_research import gate
from a_share_research.policy import evaluation
from a_share_research.policy.position_request import Reference
from a_share_research.policy.scenario_request import Proposal
from a_share_research.policy.v0 import monitor_contract, portfolio_scenario


class Bundle(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    risk_policy: dict
    snapshot: dict
    monitor_evaluation: Reference
    proposal: Proposal


def evaluate(bundle: dict, parameters: dict) -> dict:
    evaluation._json(bundle)
    evaluation.validate_parameters(parameters)
    parsed = Bundle.model_validate(bundle)
    if gate.canonical(parsed.risk_policy) != gate.canonical(parameters):
        raise evaluation.EvaluationError('bundle.risk_policy does not match registered parameters')
    snapshot = deepcopy(parsed.snapshot)
    monitor_contract.validate_monitor_snapshot(snapshot)
    if gate.canonical(snapshot['account']['risk_policy']) != gate.canonical(parameters):
        raise evaluation.EvaluationError('snapshot risk policy does not match registered parameters')
    if gate.digest(snapshot) != parsed.monitor_evaluation.result_hash:
        raise evaluation.EvaluationError('snapshot does not match saved monitor result hash')
    proposal = parsed.proposal
    try:
        scenario = portfolio_scenario.compare(snapshot, proposal.sales, fees=proposal.fees, cash=proposal.cash)
        stdout = json.dumps(scenario, ensure_ascii=False, allow_nan=False) + '\n'
    except (ValueError, OverflowError) as exc:
        return dict(status='unavailable', exit_code=1, stdout='', stderr=f'情景不可计算：{exc}\n', scenario=None)
    return dict(status='ready', exit_code=0, stdout=stdout, stderr='', scenario=scenario)
