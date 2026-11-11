"""Strict replay boundary for the legacy holding stop/governance checks."""
import re

from pydantic import BaseModel, ConfigDict

from a_share_research import gate
from a_share_research.policy import evaluation
from a_share_research.policy.v0 import domain, holdings_check_input
from a_share_research.policy.v0.market_quotes import PriceQuote


class Bundle(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    risk_policy: dict
    now: str
    holdings: list[list]
    defers: list[list]
    quotes: dict[str, evaluation.QuoteInput | None]
    fundamentals: dict[str, dict | None]
    market_snapshot: dict | None


def evaluate(bundle: dict, parameters: dict) -> dict:
    evaluation._json(bundle)
    evaluation.validate_parameters(parameters)
    parsed = Bundle.model_validate(bundle)
    if gate.canonical(parsed.risk_policy) != gate.canonical(parameters):
        raise evaluation.EvaluationError('bundle.risk_policy does not match registered parameters')
    now = evaluation._time(parsed.now, 'now').astimezone(domain._CST)
    if 'effective_from' in parameters:
        if (evaluation._time(parameters['effective_from'], 'effective_from') > now
                or evaluation._time(parameters['confirmed_at'], 'confirmed_at') > now
                or parameters.get('effective_to') is not None
                and now > evaluation._time(parameters['effective_to'], 'effective_to')):
            raise evaluation.EvaluationError('registered policy is not effective at the captured evaluation time')
    ids, codes = set(), []
    for row in parsed.holdings:
        if len(row) != 6:
            raise evaluation.EvaluationError('holding must have six fields')
        holding_id, code, name, cost, stop15, stop20 = row
        if type(holding_id) is not int or holding_id <= 0 or holding_id in ids:
            raise evaluation.EvaluationError('holding IDs must be unique positive integers')
        if type(code) is not str or not re.fullmatch(r'[0-9]{6}', code):
            raise evaluation.EvaluationError('holding code must be six ASCII digits')
        if name is not None and type(name) is not str:
            raise evaluation.EvaluationError('holding name must be string or null')
        if any(value is not None and type(value) not in (int, float) for value in (cost, stop15, stop20)):
            raise evaluation.EvaluationError('holding cost and stops must be numbers or null')
        ids.add(holding_id)
        codes.append(code)
    if codes != sorted(codes):
        raise evaluation.EvaluationError('holdings must be ordered by code')
    if set(parsed.quotes) != set(codes) or set(parsed.fundamentals) != set(codes):
        raise evaluation.EvaluationError('quotes and fundamentals must exactly match holding codes')
    for row in parsed.defers:
        if (len(row) != 3 or type(row[0]) is not int or row[0] <= 0
                or any(value is not None and type(value) is not str for value in row[1:])):
            raise evaluation.EvaluationError('defer requires positive holding ID and nullable evidence/review strings')
    if not codes and parsed.market_snapshot is not None:
        raise evaluation.EvaluationError('empty holdings require no market snapshot')
    quotes = {code: None if value is None else PriceQuote(**value.model_dump()) for code, value in parsed.quotes.items()}
    try:
        result = holdings_check_input.calculate(parsed.holdings, parsed.defers, quotes, parsed.fundamentals,
                                              parsed.market_snapshot, now)
    except TypeError:
        raise evaluation.EvaluationError('holding inputs cannot be evaluated by the legacy calculation') from None
    evaluation._json(result, 'holdings check result')
    return result
