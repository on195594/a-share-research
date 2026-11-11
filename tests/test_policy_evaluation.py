"""Pure replay boundary contracts; empty-account fixtures are structural, not financial evidence."""
from copy import deepcopy
from datetime import datetime

import pytest

from a_share_research.policy import evaluation
from a_share_research.policy.v0 import commands_monitor, risk_budget
from a_share_research.policy.v0.market_quotes import PriceQuote
from a_share_research.core.market_data import MarketDataResult


@pytest.fixture
def bundle():
    return dict(local=dict(holdings=[], closed_count=0), quotes={}, portfolio_value=None,
                industry_result=None, now='2026-11-11T12:00:00+08:00', risk_policy=risk_budget.policy(),
                denominator_evidence={}, account_scope_required=False,
                research_result={'status': 'not_configured', 'cards': []})


def test_empty_account_keeps_original_monitor_contract(bundle):
    before = deepcopy(bundle)
    original = bundle | {'now': datetime.fromisoformat(bundle['now'])}
    assert evaluation.evaluate(bundle, bundle['risk_policy']) == commands_monitor.build_monitor_snapshot(**original)
    assert bundle == before


def test_pack_preserves_dataclass_fields_and_clock(bundle):
    # Transport-only value, deliberately not an investment example or an account holding.
    bundle['quotes'] = {'transport': PriceQuote(0, None, None)}
    bundle['industry_result'] = MarketDataResult(None, 'failed', 'fixture', bundle['now'], error_code='TIMEOUT')
    bundle['now'] = datetime.fromisoformat(bundle['now'])
    packed = evaluation.pack(bundle)
    actual = evaluation.evaluate(packed, packed['risk_policy'])
    assert actual == commands_monitor.build_monitor_snapshot(**bundle)
    assert packed['quotes']['transport']['price'] == 0
    assert packed['industry_result']['error_code'] == 'TIMEOUT'


@pytest.mark.parametrize('change', [
    {'now': '2026-11-11'}, {'now': 'invalid'}, {'unexpected': True}, {'local': []},
    {'local': {'holdings': [], 'closed_count': True}}, {'quotes': []}, {'portfolio_value': True},
    {'industry_result': {'status': 'ok'}}, {'research_result': []}, {'account_scope_required': 1},
    {'denominator_evidence': {'value': float('nan')}}, {'denominator_evidence': {1: 'not a JSON key'}},
    {'quotes': {'transport': {'price': '1', 'quote_date': None, 'quote_time': None}}},
    {'quotes': {'transport': {'price': 0, 'quote_date': None, 'quote_time': None, 'unknown': True}}},
])
def test_invalid_input_is_not_coerced(bundle, change):
    with pytest.raises(ValueError):
        evaluation.evaluate(bundle | change, bundle['risk_policy'])


def test_missing_required_input_is_reported(bundle):
    del bundle['now']
    with pytest.raises(ValueError, match='now'):
        evaluation.evaluate(bundle, bundle['risk_policy'])


def test_parameters_cannot_drift_from_registered_version(bundle):
    changed = bundle['risk_policy'] | {'max_position_risk_pct': 3.0}
    with pytest.raises(ValueError, match='does not match'):
        evaluation.evaluate(bundle, changed)


@pytest.mark.parametrize('change', [
    {'source': []}, {'source': 'unrecognized'}, {'policy_id': ''}, {'extra': 'ignored'},
    {'max_position_risk_pct': None}, {'max_position_risk_pct': 0}, {'max_portfolio_risk_pct': 101},
    {'max_portfolio_risk_pct': float('inf')}, {'off_account_strategy_cash': -1},
    {'untradable_boards': ['invented']}, {'field_sources': []},
    {'field_sources': {'max_position_risk_pct': [], 'max_portfolio_risk_pct': 'cli'}},
])
def test_invalid_parameter_sets_cannot_be_registered(change):
    with pytest.raises(ValueError):
        evaluation.validate_parameters(risk_budget.policy() | change)


def test_unapplied_policy_keeps_scope_gap(bundle):
    marker = 'not_applied_account_scope_required'
    limits = {'policy_id': 'structural', 'source': marker, 'status': marker,
              'max_position_risk_pct': None, 'max_portfolio_risk_pct': None,
              'field_sources': {'max_position_risk_pct': marker, 'max_portfolio_risk_pct': marker}}
    bundle['risk_policy'] = limits
    with pytest.raises(ValueError, match='account_scope_required'):
        evaluation.evaluate(bundle, limits)
    bundle['account_scope_required'] = True
    bundle['denominator_evidence'] = {'account_scope': None, 'denominator_requires_review': True}
    actual = evaluation.evaluate(bundle, limits)
    assert actual == commands_monitor.build_monitor_snapshot(**(bundle | {'now': datetime.fromisoformat(bundle['now'])}))
    assert actual['account']['risk_policy']['status'] == marker
    assert actual['account']['denominator_requires_review'] is True


def file_parameters():
    # Explicitly synthetic policy metadata used only to check temporal/provenance validation.
    return risk_budget.policy() | {
        'source': 'policy_file', 'account_scope': 'boundary-fixture', 'currency': 'CNY',
        'effective_from': '2026-01-01T00:00:00+08:00', 'effective_to': '2026-12-31T23:59:59+08:00',
        'confirmed_at': '2026-01-01T00:00:00+08:00', 'confirmation_ref': 'structural-fixture',
        'field_sources': {'max_position_risk_pct': 'policy_file', 'max_portfolio_risk_pct': 'policy_file'}}


def test_policy_file_parameters_preserve_identity_fields(bundle):
    limits = file_parameters()
    bundle['risk_policy'] = limits
    evaluation.validate_parameters(limits)
    assert evaluation.evaluate(bundle, limits)['schema_version'] == 1


@pytest.mark.parametrize('field,value', [
    ('effective_from', '2026-12-01T00:00:00+08:00'),
    ('effective_to', '2026-02-01T00:00:00+08:00'),
    ('confirmed_at', '2026-12-01T00:00:00+08:00'),
])
def test_evaluation_checks_captured_time_not_wall_clock(bundle, field, value):
    limits = file_parameters() | {field: value}
    bundle['risk_policy'] = limits
    with pytest.raises(ValueError, match='captured evaluation time'):
        evaluation.evaluate(bundle, limits)


def test_parameter_validation_does_not_read_host_configuration(monkeypatch):
    monkeypatch.setattr(evaluation.risk_policy, '_load_policy', lambda *a, **k: pytest.fail('must not read files'))
    monkeypatch.setattr(evaluation.risk_policy, '_load_account_snapshot', lambda: pytest.fail('must not read account'))
    evaluation.validate_parameters(file_parameters())
