"""Retain v0 position sizing from a verified monitor receipt."""
from a_share_research.policy import derived, registry
from a_share_research.policy.position_request import validate_proposal


def run(conn, principal, database, request_id, expected_activation_id, monitor_evaluation_id, proposal):
    from a_share_research.policy.position_evaluation import evaluate
    try:
        proposal = validate_proposal(proposal)
    except ValueError:
        raise registry.PolicyError('拟买入代码、价格、框架或可用资金不符合定仓参数要求', 422) from None
    return derived.run(conn, principal, database, request_id, expected_activation_id, monitor_evaluation_id, proposal,
                       action='policy.position_size', context_version=6, evaluate=evaluate)
