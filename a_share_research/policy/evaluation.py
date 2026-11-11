"""Replay a captured JSON monitor input under an explicitly registered v0 policy.

This module neither acquires facts nor opens an account. Results retain monitor-v1
semantics; persistence and the policy identity are owned by the caller.
"""
from dataclasses import asdict, fields, is_dataclass, MISSING
from datetime import datetime
import json
from typing import get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

from a_share_research import gate
from a_share_research.core.market_data import MarketDataResult
from a_share_research.policy.v0 import commands_monitor, monitor_contract, risk_budget, risk_policy
from a_share_research.policy.v0.market_quotes import PriceQuote


class EvaluationError(ValueError):
    """The captured input cannot be replayed without changing its meaning."""


def _json(value, where='input'):
    """Reject Python-only objects and non-string keys rather than coercing them."""
    if value is None or type(value) in (str, bool):
        return
    if type(value) in (int, float) and risk_budget.finite_number(value):
        return
    if type(value) is list:
        for item in value:
            _json(item, where)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json(item, where)
        return
    raise EvaluationError(f'{where} must contain only finite JSON values and string object keys')


def _time(value, where):
    if not isinstance(value, str):
        raise EvaluationError(f'{where} must be a timezone-aware ISO timestamp')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise EvaluationError(f'{where} must be a timezone-aware ISO timestamp') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationError(f'{where} must be timezone-aware')
    return parsed


def validate_parameters(parameters: dict) -> None:
    """Validate effective v0 parameters without consulting files, time or defaults."""
    _json(parameters, 'parameters')
    required = {'policy_id', 'source', 'max_position_risk_pct', 'max_portfolio_risk_pct', 'field_sources'}
    optional = {'status', 'account_scope', 'currency', 'effective_from', 'effective_to', 'confirmed_at',
                'confirmation_ref', 'drawdown_observation_target_pct', 'off_account_strategy_cash', 'untradable_boards'}
    if not isinstance(parameters, dict) or not required <= parameters.keys() or parameters.keys() - required - optional:
        raise EvaluationError('parameters must contain the effective v0 risk policy fields only')
    if not isinstance(parameters['policy_id'], str) or not parameters['policy_id'].strip():
        raise EvaluationError('parameters.policy_id must be a nonempty string')
    sources = {'compatibility_default', 'cli_override', 'policy_file', 'not_applied_account_scope_required'}
    if not isinstance(parameters['source'], str) or parameters['source'] not in sources:
        raise EvaluationError('parameters.source is unsupported')
    unapplied = parameters['source'] == 'not_applied_account_scope_required'
    if ('status' in parameters or unapplied) and parameters.get('status') != 'not_applied_account_scope_required':
        raise EvaluationError('parameters.status does not describe an unapplied policy')
    if parameters.get('status') == 'not_applied_account_scope_required' and not unapplied:
        raise EvaluationError('parameters.status conflicts with source')
    for field in ('max_position_risk_pct', 'max_portfolio_risk_pct'):
        if unapplied:
            if parameters[field] is not None:
                raise EvaluationError('unapplied policy cannot provide effective risk limits')
        else:
            risk_policy._limit(parameters[field], field)
    sources_by_field = parameters['field_sources']
    allowed_fields = {'max_position_risk_pct', 'max_portfolio_risk_pct', 'drawdown_observation_target_pct',
                      'off_account_strategy_cash', 'untradable_boards'}
    if (not isinstance(sources_by_field, dict) or not {'max_position_risk_pct', 'max_portfolio_risk_pct'} <= sources_by_field.keys()
            or sources_by_field.keys() - allowed_fields
            or any(not isinstance(value, str) or value not in {'compatibility_default', 'cli', 'policy_file', 'not_applied_account_scope_required'}
                   for value in sources_by_field.values())):
        raise EvaluationError('parameters.field_sources is invalid')
    for field in ('max_position_risk_pct', 'max_portfolio_risk_pct'):
        if (sources_by_field[field] == 'not_applied_account_scope_required') != unapplied:
            raise EvaluationError('risk limit provenance conflicts with application status')
    if 'drawdown_observation_target_pct' in parameters:
        risk_policy._limit(parameters['drawdown_observation_target_pct'], 'drawdown_observation_target_pct')
    if 'off_account_strategy_cash' in parameters:
        cash = parameters['off_account_strategy_cash']
        if not risk_budget.finite_number(cash) or cash < 0:
            raise EvaluationError('off_account_strategy_cash must be finite and nonnegative')
    if 'untradable_boards' in parameters:
        risk_policy.untradable_boards(parameters['untradable_boards'])
    if parameters['source'] == 'policy_file' or 'account_scope' in parameters:
        for field in ('account_scope', 'confirmation_ref'):
            if not isinstance(parameters.get(field), str) or not parameters[field].strip():
                raise EvaluationError(f'parameters.{field} must be nonempty')
        if parameters.get('currency') != 'CNY':
            raise EvaluationError('parameters.currency must be CNY')
        start = _time(parameters.get('effective_from'), 'parameters.effective_from')
        _time(parameters.get('confirmed_at'), 'parameters.confirmed_at')
        if parameters.get('effective_to') is not None and _time(parameters['effective_to'], 'parameters.effective_to') <= start:
            raise EvaluationError('parameters.effective_to must follow effective_from')


def _dataclass_model(cls, overrides=None):
    hints = get_type_hints(cls) | (overrides or {})
    return create_model(cls.__name__ + 'Input', __config__=ConfigDict(strict=True, extra='forbid', allow_inf_nan=False),
                        **{f.name: (hints[f.name], ... if f.default is MISSING else f.default) for f in fields(cls)})


QuoteInput = _dataclass_model(PriceQuote)
IndustryInput = _dataclass_model(MarketDataResult, {'value': dict[str, str] | None})


class Bundle(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    local: dict
    quotes: dict[str, dict | None]
    portfolio_value: float | None
    industry_result: dict | None
    now: str
    risk_policy: dict
    denominator_evidence: dict
    account_scope_required: bool
    research_result: dict


def evaluate(bundle: dict, parameters: dict) -> dict:
    """Return the unchanged monitor-v1 result for one explicit, fixed JSON input."""
    try:
        _json(bundle)
        validate_parameters(parameters)
        parsed = Bundle.model_validate(bundle)
        if gate.canonical(parsed.risk_policy) != gate.canonical(parameters):
            raise EvaluationError('bundle.risk_policy does not match registered parameters')
        current = _time(parsed.now, 'now')
        if parameters['source'] == 'not_applied_account_scope_required' and not parsed.account_scope_required:
            raise EvaluationError('unapplied policy requires account_scope_required=true')
        if 'effective_from' in parameters:
            if (_time(parameters['effective_from'], 'effective_from') > current
                    or _time(parameters['confirmed_at'], 'confirmed_at') > current
                    or parameters.get('effective_to') is not None and current > _time(parameters['effective_to'], 'effective_to')):
                raise EvaluationError('registered policy is not effective at the captured evaluation time')
        local = parsed.local
        if (set(local) != {'holdings', 'closed_count'} or not isinstance(local['holdings'], list)
                or any(not isinstance(item, dict) for item in local['holdings'])
                or type(local['closed_count']) is not int or local['closed_count'] < 0):
            raise EvaluationError('local requires holdings objects and a nonnegative closed_count')
        quotes = {code: None if value is None else PriceQuote(**QuoteInput.model_validate(value).model_dump())
                  for code, value in parsed.quotes.items()}
        industry = None if parsed.industry_result is None else MarketDataResult(**IndustryInput.model_validate(parsed.industry_result).model_dump())
        result = commands_monitor.build_monitor_snapshot(local, quotes, parsed.portfolio_value,
            industry_result=industry, now=current, risk_policy=parsed.risk_policy,
            denominator_evidence=parsed.denominator_evidence, account_scope_required=parsed.account_scope_required,
            research_result=parsed.research_result)
        return monitor_contract.validate_monitor_snapshot(result)
    except (TypeError, KeyError, AttributeError) as exc:
        raise EvaluationError(f'invalid monitor input structure: {exc}') from None


def pack(bundle: dict) -> dict:
    """Encode an already trusted in-memory capture; never deserialize executable data."""
    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if is_dataclass(value) and not isinstance(value, type):
            return asdict(value)
        raise TypeError(f'unsupported captured value: {type(value).__name__}')
    return json.loads(json.dumps(bundle, default=encode, ensure_ascii=False, allow_nan=False))
