"""Shared benchmark, daily brief and stop evaluation inputs."""

from copy import deepcopy
from dataclasses import asdict

from a_share_research.policy.v0 import benchmark_input, market_quotes, risk_budget

STOP1, STOP2 = 24.824, 23.364


def stop_bundle(price=24.0, now='2026-10-22T10:31:00+08:00', quote_time='10:30:00', state=None):
    return dict(risk_policy=risk_budget.policy(), now=now,
                holdings=[dict(code='000963', name='华东医药', stop_loss_15=STOP1, stop_loss_20=STOP2)],
                quotes={'000963': asdict(market_quotes.PriceQuote(price, '2026-10-22', quote_time, 'boundary-fixture'))},
                previous_state={} if state is None else state)


DAILY_NOW = '2026-10-22T15:30:05+08:00'


def provider(rows):
    return dict(rows=rows, source='tushare.dividend', status='ok' if rows is not None else 'failed',
                fetched_at=DAILY_NOW, source_as_of=None, error_code=None if rows is not None else 'TIMEOUT',
                error_type=None if rows is not None else 'TimeoutError')


EVENTS = [
    ['600900', 'buy', '2026-05-27', 100, 100., 5., 0., None, 0],
    ['600900', 'dividend', '2026-07-09', None, None, 0., 0., 300., 0],
    ['600900', 'sell', '2026-09-30', 50, 120., 5., 3., None, 0],
    ['600900', 'adjustment', '2026-09-30', 0, 99., 0., 0., None, 0],
]
CLOSES = {'2026-05-28': 100., '2026-07-09': 120., '2026-09-30': 150., '2026-10-21': 160.}
GOLDEN = ('账本 vs 指数（2026-05-27 至 2026-10-21 收盘）| 累计买入 10,005 元 | 在仓市值 6,500 元\n'
          '  账本净盈亏：+2,787 元（+27.86%）\n'
          '  同样现金流买沪深300全收益：+5,504 元（+55.01%） | 账本超额 -2,717 元\n'
          '  同样现金流买中证红利全收益：+5,504 元（+55.01%） | 账本超额 -2,717 元\n'
          '  样本期短、交易少时只说明期间相对表现，不证明方法有效\n')


def response(**data):
    return dict(status='ok', source='structural-fixture', fetched_at='2026-10-22T10:00:00+08:00',
                source_as_of='2026-10-21', error_code=None, error_type=None, fallback_reason=None,
                freshness_days=0) | data


def benchmark_bundle():
    return dict(risk_policy=risk_budget.policy(), now='2026-10-22T10:00:00+08:00', end='2026-10-22',
                events=deepcopy(EVENTS), indices={code: response(rows=[dict(date=d, close=p) for d,p in CLOSES.items()])
                                                for code,_ in benchmark_input.BENCHMARKS},
                prices={'600900': response(value=130.)})
