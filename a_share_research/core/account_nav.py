"""Pure unit NAV calculation; callers establish evidence, flow coverage and index dates.

Only external cash flows enter this calculation. Trading costs and investment income
remain in asset valuations. This module neither certifies an account nor fetches data.
"""
from datetime import datetime, time
from decimal import Context, Decimal, DecimalException, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, field_validator, model_validator


_SHANGHAI = ZoneInfo('Asia/Shanghai')


def _context():
    return localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                                capitals=1, clamp=0, traps=[InvalidOperation, DivisionByZero, Overflow]))


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('amount must be a finite decimal number')
    try:
        amount = Decimal(str(value))
    except DecimalException as exc:
        raise ValueError('amount must be a finite decimal number') from exc
    if not amount.is_finite():
        raise ValueError('amount must be a finite decimal number')
    return amount


def _timestamp(value):
    if isinstance(value, str):
        if 'T' not in value and ' ' not in value:
            raise ValueError('timestamp requires a full date, time and timezone')
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError('timestamp requires a full date, time and timezone') from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timestamp requires a full date, time and timezone')
    return value.astimezone(_SHANGHAI)


Money = Annotated[Decimal, BeforeValidator(_money)]
Timestamp = Annotated[datetime, BeforeValidator(_timestamp)]


class _Value(BaseModel):
    model_config = ConfigDict(extra='forbid', revalidate_instances='always')


class Valuation(_Value):
    as_of: Timestamp
    total_assets: Annotated[Money, Field(ge=0)]


class Flow(_Value):
    event_id: Annotated[StrictInt, Field(gt=0)]
    occurred_at: Timestamp
    cash: Money
    before: Valuation

    @model_validator(mode='after')
    def flow_boundary(self):
        if self.cash == 0:
            raise ValueError('external cash flow must be nonzero')
        if self.before.as_of != self.occurred_at:
            raise ValueError('flow requires a valuation immediately before the same instant')
        if self.before.total_assets <= 0:
            raise ValueError('pre-flow assets must be positive')
        # Comparison avoids rounding away a small positive remainder.
        if self.cash < 0 and self.cash.copy_abs() >= self.before.total_assets:
            raise ValueError('flow exhausts assets; end the segment before withdrawal')
        return self


class Inputs(_Value):
    schema_version: Literal[1] = 1
    method: Literal['unit_nav_v1'] = 'unit_nav_v1'
    start: Valuation
    end: Valuation
    flows: list[Flow]
    benchmark_start: Annotated[Money, Field(gt=0)]
    benchmark_end: Annotated[Money, Field(gt=0)]

    @field_validator('schema_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError('schema_version must be integer 1')
        return value

    @model_validator(mode='after')
    def interval(self):
        if self.start.as_of >= self.end.as_of or self.start.total_assets <= 0:
            raise ValueError('interval requires ordered endpoints and positive initial assets')
        if any(point.as_of.time() != time(15) for point in (self.start, self.end)):
            raise ValueError('endpoints must be Shanghai close at 15:00:00')
        if len({flow.event_id for flow in self.flows}) != len(self.flows):
            raise ValueError('duplicate external flow event_id')
        if len({flow.occurred_at for flow in self.flows}) != len(self.flows):
            raise ValueError('simultaneous external flows have ambiguous ordering')
        if any(not self.start.as_of < flow.occurred_at < self.end.as_of for flow in self.flows):
            raise ValueError('external flows must be strictly inside the interval')
        return self


def evaluate(inputs: Inputs) -> dict:
    """Return decimal strings and chronological flow audit; reject invalid inputs.

No daily timing approximation or inference of missing flows is performed. Invalid
inputs raise ValueError (including Pydantic ValidationError); unsupported arithmetic
also raises ValueError. Calculation precision is independent of the caller's context.
"""
    inputs = Inputs.model_validate(inputs)
    try:
        with _context():
            units = inputs.start.total_assets
            audit = []
            for flow in sorted(inputs.flows, key=lambda item: item.occurred_at):
                before_units = units
                before_nav = flow.before.total_assets / units
                after_assets = flow.before.total_assets + flow.cash
                units = units * after_assets / flow.before.total_assets
                if units <= 0 or before_nav <= 0:
                    raise ValueError('arithmetic cannot represent positive units and NAV')
                audit.append(dict(event_id=flow.event_id, occurred_at=flow.occurred_at.isoformat(),
                                  cash=str(flow.cash), before_assets=str(flow.before.total_assets),
                                  after_assets=str(after_assets), before_nav=str(before_nav),
                                  before_units=str(before_units), after_units=str(units)))
            end_nav = inputs.end.total_assets / units
            account_return = end_nav - 1
            benchmark_return = inputs.benchmark_end / inputs.benchmark_start - 1
            return dict(method=inputs.method, initial_nav='1', end_nav=str(end_nav),
                        account_return=str(account_return), benchmark_return=str(benchmark_return),
                        excess_percentage_points=str((account_return - benchmark_return) * 100),
                        initial_units=str(inputs.start.total_assets), end_units=str(units), flows=audit)
    except DecimalException as exc:
        raise ValueError('amounts exceed supported calculation range') from exc
