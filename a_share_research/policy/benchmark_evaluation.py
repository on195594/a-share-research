"""Replay a captured benchmark report without I/O or an implicit clock."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from a_share_research import gate
from a_share_research.policy import evaluation
from a_share_research.policy.v0 import benchmark_input


class Metadata(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    status: Literal['ok', 'degraded', 'failed']
    source: str
    fetched_at: str
    source_as_of: str | None
    error_code: str | None
    error_type: str | None
    fallback_reason: str | None
    freshness_days: int | None = Field(ge=0)


class IndexResponse(Metadata):
    rows: list[dict] | None


class PriceResponse(Metadata):
    value: float | None = Field(gt=0)


class Bundle(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    risk_policy: dict
    now: str
    end: str
    events: list[list]
    indices: dict[str, IndexResponse]
    prices: dict[str, PriceResponse]


def _day(value):
    if type(value) is not str:
        raise evaluation.EvaluationError('date must be YYYY-MM-DD')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise evaluation.EvaluationError('date must be YYYY-MM-DD') from None
    if parsed.isoformat() != value:
        raise evaluation.EvaluationError('date must be YYYY-MM-DD')
    return value


def _events(rows, end):
    previous = ''
    for row in rows:
        if len(row) != 9:
            raise evaluation.EvaluationError('event must have exactly nine fields')
        code, kind, day, shares, price, fees, tax, cash, inferred = row
        if type(code) is not str or not code or kind not in ('buy', 'sell', 'dividend', 'adjustment'):
            raise evaluation.EvaluationError('invalid event code or type')
        if _day(day) > end or day < previous:
            raise evaluation.EvaluationError('events must be ordered and no later than end')
        previous = day
        if shares is not None and type(shares) is not int:
            raise evaluation.EvaluationError('event shares must be integer or null')
        for value in (price, fees, tax, cash):
            if value is not None and type(value) not in (int, float):
                raise evaluation.EvaluationError('event amounts must be numbers or null')
        if type(inferred) is not int or inferred not in (0, 1):
            raise evaluation.EvaluationError('inferred must be zero or one')
        if kind in ('buy', 'sell') and (shares is None or shares <= 0 or price is None or price <= 0):
            raise evaluation.EvaluationError('trade requires positive shares and price')
        if any(value is not None and value < 0 for value in (fees, tax, cash)):
            raise evaluation.EvaluationError('event costs and cash must be nonnegative')


def _failure(kind, code, response=None):
    return dict(kind=kind, code=code, error_code=response.error_code if response else None,
                error_type=response.error_type if response else None)


def index_closes(indices: dict, end: str) -> dict | None:
    """Validate captured index responses, preserving last-row duplicate semantics."""
    evaluation._json(indices)
    end = _day(end)
    if type(indices) is not dict or set(indices) != {c for c, _ in benchmark_input.BENCHMARKS}:
        raise evaluation.EvaluationError('indices must contain exactly the registered benchmarks')
    benchmarks = {}
    unavailable = False
    for code, data in indices.items():
        response = IndexResponse.model_validate(data)
        evaluation._time(response.fetched_at, 'fetched_at')
        if response.source_as_of is not None:
            _day(response.source_as_of)
        closes = {}
        for row in response.rows or []:
            if 'date' not in row or 'close' not in row:
                raise evaluation.EvaluationError('index rows require date and close')
            day, close = _day(row['date']), row['close']
            if day > end or type(close) not in (int, float) or close <= 0:
                raise evaluation.EvaluationError('invalid index date or close')
            closes[day] = close
        unavailable = unavailable or response.status == 'failed' or not closes
        benchmarks[code] = closes
    return None if unavailable else benchmarks


def evaluate(bundle: dict, parameters: dict) -> dict:
    evaluation._json(bundle)
    evaluation.validate_parameters(parameters)
    parsed = Bundle.model_validate(bundle)
    if gate.canonical(parsed.risk_policy) != gate.canonical(parameters):
        raise evaluation.EvaluationError('bundle.risk_policy does not match registered parameters')
    now = evaluation._time(parsed.now, 'now')
    if 'effective_from' in parameters:
        if (evaluation._time(parameters['effective_from'], 'effective_from') > now
                or evaluation._time(parameters['confirmed_at'], 'confirmed_at') > now
                or parameters.get('effective_to') is not None
                and now > evaluation._time(parameters['effective_to'], 'effective_to')):
            raise evaluation.EvaluationError('registered policy is not effective at the captured evaluation time')
    end = _day(parsed.end)
    _events(parsed.events, end)
    for response in [*parsed.indices.values(), *parsed.prices.values()]:
        evaluation._time(response.fetched_at, 'fetched_at')
        if response.source_as_of is not None:
            _day(response.source_as_of)
    result = dict(status='unavailable', exit_code=1, stdout='', stderr='', metrics=None, failures=[])
    if not parsed.events:
        if parsed.indices or parsed.prices:
            raise evaluation.EvaluationError('empty events require empty indices and prices')
        return result | dict(status='no_events', stderr='错误：账本没有交易事件\n')
    if set(parsed.indices) != {code for code, _ in benchmark_input.BENCHMARKS}:
        raise evaluation.EvaluationError('indices must contain exactly the registered benchmarks')
    benchmarks = index_closes(bundle['indices'], end)
    failures = [_failure('index', code, response) for code, response in parsed.indices.items()
                if response.status == 'failed' or not response.rows]
    if failures:
        if parsed.prices:
            raise evaluation.EvaluationError('unavailable indices require empty prices')
        return result | dict(stderr='错误：指数行情不可用，无法比较账本\n', failures=failures)
    try:
        prepared = benchmark_input.prepare(parsed.events, benchmarks)
    except ValueError:
        if parsed.prices:
            raise evaluation.EvaluationError('unavailable valuation requires empty prices') from None
        return result | dict(stderr='错误：账本或指数资料无法完成比较\n', failures=[_failure('calculation', None)])
    required = {code for code, shares in prepared['open_shares'].items() if shares}
    if set(parsed.prices) != required:
        raise evaluation.EvaluationError('prices must match nonzero ledger holdings exactly')
    failures = [_failure('stock', code, response) for code, response in parsed.prices.items()
                if response.status == 'failed' or response.value is None]
    if failures:
        return result | dict(stderr='错误：持仓收盘价不可用，无法比较账本\n', failures=failures)
    warnings = []
    for code in prepared['open_shares']:
        response = parsed.prices.get(code)
        if response is not None and response.status == 'degraded':
            warnings.append(f"价格参考：{code} {prepared['valuation_day']} 使用 {response.source_as_of} 收盘价，"
                            f"相差 {response.freshness_days} 天（{response.source}；{response.fallback_reason}）\n")
    try:
        metrics = benchmark_input.calculate(parsed.events, benchmarks, {c: r.value for c, r in parsed.prices.items()})
    except (ValueError, OverflowError):
        return result | dict(stderr=''.join(warnings) + '错误：账本或指数资料无法完成比较\n',
                             failures=[_failure('calculation', None)])
    stdout = metrics.pop('stdout')
    return result | dict(status='ready', exit_code=0, stdout=stdout, stderr=''.join(warnings), metrics=metrics)
