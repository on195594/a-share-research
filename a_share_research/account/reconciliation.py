"""Pure account quantity/cash reconciliation; tolerance is supplied by the caller."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, field_validator, model_validator


def money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('金额须为有限数字')
    try:
        result = Decimal(str(value))
    except Exception:
        raise ValueError('金额须为有限数字') from None
    if not result.is_finite():
        raise ValueError('金额须为有限数字')
    return result


Money = Annotated[Decimal, BeforeValidator(money)]
Code = Annotated[str, Field(pattern=r'^[0-9]{6}$')]
Quantity = Annotated[StrictInt, Field(ge=0)]


class Value(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Position(Value):
    code: Code
    shares: Quantity


class Opening(Value):
    positions: dict[Code, Quantity]
    cash: Annotated[Money, Field(ge=0)]


Projection = Opening


class Snapshot(Opening):
    total_assets: Annotated[Money, Field(ge=0)]
    market_values: dict[Code, Annotated[Money, Field(ge=0)]]

    @model_validator(mode='after')
    def complete_values(self):
        if {k for k, n in self.positions.items() if n} != {k for k, n in self.market_values.items() if n}:
            raise ValueError('持仓与非零市值代码必须一致，不能缺市值')
        return self


class Event(Value):
    kind: Literal['buy', 'sell', 'dividend', 'adjustment', 'deposit', 'withdrawal', 'fee']
    code: Code | None = None
    occurred_at: datetime
    broker_id: str | None = None
    shares: StrictInt = 0
    price: Annotated[Money, Field(ge=0)] = Decimal(0)
    fees: Annotated[Money, Field(ge=0)] = Decimal(0)
    tax: Annotated[Money, Field(ge=0)] = Decimal(0)
    cash: Annotated[Money, Field(ge=0)] = Decimal(0)
    report_id: Annotated[StrictInt, Field(gt=0)] | None = None
    purpose: Literal['investment', 'stop_loss', 'rebalance'] = 'investment'

    @field_validator('occurred_at', mode='before')
    @classmethod
    def time_value(cls, value):
        if not isinstance(value, (str, datetime)) or isinstance(value, str) and 'T' not in value and ' ' not in value:
            raise ValueError('事件必须提供完整成交日期时间')
        return value

    @field_validator('occurred_at')
    @classmethod
    def timezone(cls, value):
        return (value.replace(tzinfo=ZoneInfo('Asia/Shanghai')) if value.tzinfo is None else value).astimezone(ZoneInfo('Asia/Shanghai'))

    @field_validator('broker_id')
    @classmethod
    def broker(cls, value):
        if value is not None and (not value.strip() or len(value) > 128):
            raise ValueError('券商编号不能为空或超过128字符')
        return value.strip() if value else value

    @model_validator(mode='after')
    def shape(self):
        stock = self.kind in ('buy', 'sell', 'dividend', 'adjustment')
        if stock != (self.code is not None):
            raise ValueError('股票事件须有代码，现金事件不能有代码')
        if self.kind in ('buy', 'sell'):
            if self.shares <= 0 or self.price <= 0 or self.cash:
                raise ValueError('买卖须有正股数和价格，不接受另填现金净额')
        elif self.kind == 'adjustment':
            if not self.shares or self.price or self.cash or self.fees or self.tax:
                raise ValueError('送转调整仅允许非零股数变动')
        elif self.shares or self.price or self.fees or self.tax or self.cash <= 0:
            raise ValueError('现金事件仅允许明确正金额')
        if self.kind not in ('buy', 'sell') and (self.report_id is not None or self.purpose != 'investment'):
            raise ValueError('只有成交可填写研究依据和动作类型')
        return self


def identity(event: Event, account_id: str) -> str:
    if not account_id.strip():
        raise ValueError('账户编号不能为空')
    if event.broker_id:
        parts = [account_id, 'broker', event.broker_id]
    else:
        parts = [account_id, event.occurred_at.isoformat(), event.code, event.kind, event.shares,
                 str(event.price.normalize()), str(event.cash.normalize())]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def project(opening: Opening, events: list[Event]) -> Projection:
    positions, cash = dict(opening.positions), opening.cash
    for event in sorted(events, key=lambda item: item.occurred_at):
        if event.kind in ('buy', 'sell'):
            sign = 1 if event.kind == 'buy' else -1
            positions[event.code] = positions.get(event.code, 0) + sign * event.shares
            cash -= sign * event.shares * event.price + event.fees + event.tax
        elif event.kind == 'adjustment':
            positions[event.code] = positions.get(event.code, 0) + event.shares
        else:
            cash += event.cash if event.kind in ('deposit', 'dividend') else -event.cash
        if cash < 0 or any(n < 0 for n in positions.values()):
            raise ValueError('事件重放产生负现金或负持仓')
    return Projection(positions={k: v for k, v in positions.items() if v}, cash=cash)


def reconcile(projected: Projection, snapshot: Snapshot, tolerance: Decimal) -> list[dict]:
    tolerance = money(tolerance)
    if tolerance < 0:
        raise ValueError('容差不能为负')
    differences = []
    def compare(field, expected, actual, limit=Decimal(0)):
        if abs(expected - actual) > limit:
            differences.append(dict(field=field, expected=str(expected), actual=str(actual)))
    for code in sorted(set(projected.positions) | set(snapshot.positions)):
        compare('positions.' + code, projected.positions.get(code, 0), snapshot.positions.get(code, 0))
    compare('cash', projected.cash, snapshot.cash, tolerance)
    compare('total_assets', snapshot.cash + sum(snapshot.market_values.values()), snapshot.total_assets, tolerance)
    return differences
