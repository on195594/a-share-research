"""Pure replay of v0 stop notifications with an explicit clock, policy and prior state."""
from pydantic import BaseModel, ConfigDict, Field

from a_share_research import gate
from a_share_research.policy import evaluation
from a_share_research.policy.stop_state import StopState
from a_share_research.policy.v0 import commands_holdings, domain, risk_budget, risk_gates
from a_share_research.policy.v0.market_quotes import PriceQuote


class Holding(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    code: str = Field(min_length=1)
    name: str | None
    stop_loss_15: float | None
    stop_loss_20: float | None


class Bundle(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    risk_policy: dict
    now: str
    holdings: list[Holding]
    quotes: dict[str, evaluation.QuoteInput | None]
    previous_state: StopState


def evaluate(bundle: dict, parameters: dict) -> dict:
    """Return legacy output and next notification state; never read a clock, file or provider."""
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
    previous = parsed.previous_state.model_dump(mode='json', exclude_unset=True)
    result = dict(status='outside_session', exit_code=0, stdout='', stderr='', state=previous, alerts=[])
    if not domain.is_a_share_trading_hours(now):
        return result
    # Match the legacy SELECT ORDER BY code, retaining input order for equal codes.
    rows = sorted(parsed.holdings, key=lambda row: row.code)
    if not rows:
        return result | {'status': 'no_holdings'}
    today = now.date().isoformat()
    quotes = {code: None if value is None else PriceQuote(**value.model_dump())
              for code, value in parsed.quotes.items()}
    fresh = {}
    for row in rows:
        quote = quotes.get(row.code)
        stamp = quote and risk_gates._parse_time(quote.quote_as_of)
        if risk_budget.valid_quote(quote, today) and stamp and abs((now - stamp).total_seconds()) <= 900:
            fresh[row.code] = quote
    if not fresh:
        return result | dict(status='unavailable', exit_code=1,
                             stderr='止损预警：本时段所有持仓行情无效或过期，未能检查\n')
    levels = {row.code: level for row in rows if row.code in fresh
              and (level := commands_holdings.stop_level(fresh[row.code].price, row.stop_loss_15, row.stop_loss_20))}
    alerts, state = commands_holdings.new_stop_alerts(levels, previous, today)
    if not alerts:
        return result | {'status': 'no_alerts'}
    names = {row.code: row for row in rows}
    lines = []
    for code, level in alerts:
        row, quote = names[code], fresh[code]
        where = f'（{quote.quote_time}，{quote.source}）'
        if level == 'near':
            lines.append(f'⚠️ 接近止损 {row.name}({code}) 现价{quote.price:.2f}，距第一档{row.stop_loss_15:.3f}'
                         f'仅{(quote.price - row.stop_loss_15) / quote.price:+.1%}{where}')
        else:
            stop = row.stop_loss_20 if level == 'stop2' else row.stop_loss_15
            label = '第二档' if level == 'stop2' else '第一档'
            lines.append(f'🚨 止损预警 {row.name}({code}) 现价{quote.price:.2f} 已跌破{label}止损{stop:.3f}{where}')
    lines.append('按 a-stock-monitor 复核止损与 L3；预警不等于自动卖出。')
    return result | dict(status='alerts', stdout='\n'.join(lines) + '\n', state=state,
                         alerts=[dict(code=code, level=level) for code, level in alerts])
