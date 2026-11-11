"""Retain a caller-specified static sale scenario from a verified monitor receipt."""
from a_share_research.policy import derived, registry
from a_share_research.policy.scenario_request import validate_proposal


def run(conn, principal, database, request_id, expected_activation_id, monitor_evaluation_id, proposal):
    from a_share_research.policy.scenario_evaluation import evaluate
    try:
        proposal = validate_proposal(proposal)
    except ValueError:
        raise registry.PolicyError('须明确提供拟卖出代码、正整数股数和非负费用；可选现金须为非负数', 422) from None
    return derived.run(conn, principal, database, request_id, expected_activation_id, monitor_evaluation_id, proposal,
                       action='policy.portfolio_scenario', context_version=7, evaluate=evaluate)
