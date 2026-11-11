"""Pure replay of the v0 daily brief and evidenced, per-holding dividend checks."""
from datetime import date
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from a_share_research.policy import evaluation
from a_share_research.policy.v0 import commands_holdings, commands_monitor, domain
from a_share_research.policy.v0.dividend_input import implemented_rows


def _day(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
        raise ValueError('daily input date must be YYYY-MM-DD')
    date.fromisoformat(value)
    return value


class Provider(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    rows: list[dict] | None
    source: str = Field(min_length=1)
    status: Literal['ok', 'degraded', 'failed']
    fetched_at: str
    source_as_of: str | None
    error_code: str | None
    error_type: str | None

    @field_validator('fetched_at')
    @classmethod
    def timestamp(cls, value):
        evaluation._time(value, 'dividend provider fetched_at')
        return value

    @field_validator('source_as_of')
    @classmethod
    def as_of(cls, value):
        return None if value is None else _day(value)

    @field_validator('rows')
    @classmethod
    def row_dates(cls, rows):
        if rows is not None:
            for row in rows:
                for field in ('record_date', 'ex_date', 'pay_date'):
                    if isinstance(row.get(field), str):
                        _day(row[field])
        return rows


Event = TypeAdapter(tuple[str, str, int | None, float | None, float | None, float | None, float | None, int])


class DividendInput(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', allow_inf_nan=False)
    holding_id: int = Field(gt=0)
    code: str = Field(min_length=1)
    name: str | None
    current_shares: int | None
    events: list[list]
    provider: Provider

    @field_validator('events')
    @classmethod
    def lifecycle_events(cls, rows):
        for row in rows:
            parsed = Event.validate_python(tuple(row), strict=True)
            if not parsed[0]:
                raise ValueError('lifecycle event type must be nonempty')
            _day(parsed[1])
        if [row[1] for row in rows] != sorted(row[1] for row in rows):
            raise ValueError('lifecycle events must retain event_date/id query order')
        return rows


class Bundle(evaluation.Bundle):
    dividend_today: str
    brief_today: str
    dividends: list[DividendInput]

    @field_validator('dividend_today', 'brief_today')
    @classmethod
    def days(cls, value):
        return _day(value)


def evaluate(bundle: dict, parameters: dict) -> dict:
    """Recompute the original monitor, entitlement checks and report without acquiring facts."""
    evaluation._json(bundle)
    parsed = Bundle.model_validate(bundle)
    if parsed.research_result != {'status': 'not_configured'}:
        raise evaluation.EvaluationError('daily brief does not fetch research cards')
    captured_day = evaluation._time(parsed.now, 'now').astimezone(domain._CST).date().isoformat()
    if not captured_day <= parsed.dividend_today <= parsed.brief_today:
        raise evaluation.EvaluationError('daily capture dates cannot precede the monitor or dividend check')
    # v0 annotates nested L3 dictionaries while building its result. Keep that
    # behavior inside a detached input copy so the captured evidence stays fixed.
    monitor_input = parsed.model_dump(mode='json', exclude={'dividend_today', 'brief_today', 'dividends'})
    snapshot = evaluation.evaluate(monitor_input, parameters)
    try:
        expected = sorted([(row['id'], row['code'], row['name'], row['shares']) for row in parsed.local['holdings']],
                          key=lambda row: (row[1], row[0]))
    except (TypeError, KeyError):
        raise evaluation.EvaluationError('local holdings need identity, code, name and shares') from None
    if any(type(hid) is not int or hid < 1 or not isinstance(code, str) or not code
           or name is not None and not isinstance(name, str)
           or shares is not None and type(shares) is not int for hid, code, name, shares in expected):
        raise evaluation.EvaluationError('local dividend holding identity fields have invalid types')
    actual = [(row.holding_id, row.code, row.name, row.current_shares) for row in parsed.dividends]
    if (len({row[0] for row in expected}) != len(expected)
            or actual != expected):
        raise evaluation.EvaluationError('dividend checks must match every monitor holding in code/id order')
    findings, failures = [], []
    for holding in parsed.dividends:
        provider = holding.provider
        if provider.rows is None:
            failures.append(dict(holding_id=holding.holding_id, code=holding.code, name=holding.name,
                                 error_code=provider.error_code, error_type=provider.error_type))
            continue
        for item in commands_holdings.unrecorded_dividends(
                holding.events, implemented_rows(provider.rows), parsed.dividend_today):
            findings.append(dict(holding_id=holding.holding_id, code=holding.code, name=holding.name,
                                 current_shares=holding.current_shares, item=item))
    extra = [f"{row['code']} 疑似漏记分红：登记日{row['item']['record_date']} 应收{row['item']['cash']:.2f}元"
             '（运行 dividend-check 核对）' for row in findings]
    extra += [f"{row['code']} 分红未核对：数据获取失败" for row in failures]
    brief = commands_monitor.format_daily_brief(snapshot, extra, parsed.brief_today)
    return dict(status='ready' if brief else 'silent', exit_code=0, stdout=brief + '\n' if brief else '', stderr='',
                snapshot=snapshot, dividends=dict(checked=len(parsed.dividends), findings=findings, failures=failures))
