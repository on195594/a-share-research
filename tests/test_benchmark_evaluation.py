"""Structural fixtures copied from the legacy benchmark test, NOT financial evidence.

Golden stdout was captured by executing the original pre-extraction command with
an in-memory ledger and these fixed index samples. They assert arithmetic/parity,
not investment performance, trading calendar accuracy, or live provider acceptance.
"""
from contextlib import contextmanager
from copy import deepcopy
import sqlite3

import pytest

from a_share_research.policy import benchmark_evaluation
from a_share_research.policy.v0 import benchmark_input, commands_holdings, risk_budget

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


def bundle():
    return dict(risk_policy=risk_budget.policy(), now='2026-10-22T10:00:00+08:00', end='2026-10-22',
                events=deepcopy(EVENTS), indices={code: response(rows=[dict(date=d, close=p) for d,p in CLOSES.items()])
                                                for code,_ in benchmark_input.BENCHMARKS},
                prices={'600900': response(value=130.)})


def evaluate(value):
    return benchmark_evaluation.evaluate(value, value['risk_policy'])


def test_original_golden_and_current_command(monkeypatch, capsys):
    @contextmanager
    def local():
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute('CREATE TABLE holding_events(id INTEGER,code TEXT,event_type TEXT,event_date TEXT,shares INTEGER,price REAL,fees REAL,tax REAL,cash_amount REAL,inferred INTEGER)')
            conn.executemany('INSERT INTO holding_events VALUES(?,?,?,?,?,?,?,?,?,?)', [(i,*r) for i,r in enumerate(EVENTS)])
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(commands_holdings.db, 'read_only_db_session', local)
    monkeypatch.setattr(commands_holdings, '_index_closes', lambda *_: CLOSES)
    seen = []
    monkeypatch.setattr(commands_holdings, '_close_on', lambda c,d: seen.append((c,d)) or 130.)
    commands_holdings.cmd_benchmark_report(['--end', '2026-10-22'])
    assert capsys.readouterr().out == GOLDEN
    assert seen == [('600900', '2026-10-21')]
    value = bundle()
    original = deepcopy(value)
    result = evaluate(value)
    assert value == original
    assert result['stdout'] == GOLDEN
    assert result['status'] == 'ready' and result['exit_code'] == 0
    assert result['stderr'] == '' and result['failures'] == []
    assert result['metrics']['pnl'] == 2787
    assert result['metrics']['flows'] == [['2026-05-27', -10005.], ['2026-07-09', 300.], ['2026-09-30', 5992.]]


def test_common_day_ignores_later_events_but_retains_inferred_notice():
    value = bundle()
    value['indices']['H00922.CSI']['rows'].pop()
    value['events'].append(['600900', 'buy', '2026-10-22', 10, 100., 0., 0., None, 1])
    result = evaluate(value)
    assert result['metrics']['valuation_day'] == '2026-09-30'
    assert result['metrics']['open_shares'] == {'600900': 50}
    assert result['metrics']['contains_inferred'] is True
    assert '含推断补录' in result['stdout']


def test_adjustment_multiple_codes_and_closed_position():
    value = bundle()
    value['events'] += [
        ['600900', 'adjustment', '2026-10-01', 50, None, 0., 0., None, 0],
        ['600900', 'sell', '2026-10-02', 100, 130., 0., 0., None, 0],
        ['000963', 'buy', '2026-10-03', 10, 100., 0., 0., None, 0],
    ]
    value['prices'] = {'000963': response(value=110.)}
    result = evaluate(value)
    assert result['metrics']['open_shares'] == {'600900': 0, '000963': 10}
    assert result['metrics']['market_value'] == 1100
    assert result['metrics']['pnl'] == 9387
    value['events'].append(['000963', 'sell', '2026-10-04', 10, 110., 0., 0., None, 0])
    value['prices'] = {}
    assert evaluate(value)['metrics']['market_value'] == 0


def test_degraded_price_exact_warning_and_duplicate_index_last_row():
    value = bundle()
    value['prices']['600900'].update(status='degraded', source_as_of='2026-10-20', freshness_days=1,
                                    fallback_reason='no-close', source='cache')
    value['indices']['H00300.CSI']['rows'].insert(0, dict(date='2026-10-21', close=999., extra='provider column'))
    result = evaluate(value)
    assert result['stdout'] == GOLDEN
    assert result['stderr'] == '价格参考：600900 2026-10-21 使用 2026-10-20 收盘价，相差 1 天（cache；no-close）\n'


@pytest.mark.parametrize('rows,status', [(None,'ok'), ([], 'ok'), ([], 'failed')])
def test_missing_indices_unavailable(rows, status):
    value = bundle()
    value['indices']['H00300.CSI'].update(rows=rows, status=status, error_code='provider_failed', error_type='RuntimeError')
    value['prices'] = {}
    result = evaluate(value)
    assert result['status'] == 'unavailable' and result['metrics'] is None
    assert result['failures'] == [dict(kind='index', code='H00300.CSI', error_code='provider_failed', error_type='RuntimeError')]


def test_missing_stock_no_fabricated_zero():
    value = bundle()
    value['prices']['600900'].update(value=None, status='failed', fallback_reason='SECRET PROVIDER BODY')
    result = evaluate(value)
    assert result['status'] == 'unavailable' and result['metrics'] is None
    assert 'SECRET' not in result['stderr']
    assert result['failures'][0]['kind'] == 'stock'


def test_no_events_and_zero_gross_buys():
    value = bundle()
    value.update(events=[], indices={}, prices={})
    assert evaluate(value) == dict(status='no_events', exit_code=1, stdout='', stderr='错误：账本没有交易事件\n', metrics=None, failures=[])
    value = bundle()
    value['events'] = [value['events'][1]]
    value['prices'] = {}
    assert evaluate(value)['failures'][0]['kind'] == 'calculation'
    with pytest.raises(ValueError):
        benchmark_input.calculate(value['events'], {c:CLOSES for c,_ in benchmark_input.BENCHMARKS}, {})


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(extra=True),
    lambda b: b.update(now='2026-10-22T10:00:00'),
    lambda b: b.update(end='20261022'),
    lambda b: b['events'][0].append(1),
    lambda b: b['events'][0].__setitem__(2, '2026-13-01'),
    lambda b: b['events'][0].__setitem__(2, '2026-10-23'),
    lambda b: b['events'].reverse(),
    lambda b: b['events'][0].__setitem__(3, True),
    lambda b: b['events'][0].__setitem__(4, float('nan')),
    lambda b: b['indices'].pop('H00300.CSI'),
    lambda b: b['indices']['H00300.CSI']['rows'][0].update(close=0),
    lambda b: b['indices']['H00300.CSI']['rows'][0].update(date='2026-10-23'),
    lambda b: b['indices']['H00300.CSI'].update(fetched_at='2026-10-22'),
    lambda b: b['indices']['H00300.CSI'].update(rows=[]),
    lambda b: b['prices'].update({'000963': response(value=1.)}),
    lambda b: b['prices'].clear(),
    lambda b: b['prices']['600900'].update(value=0),
    lambda b: b['prices']['600900'].update(source_as_of='bad'),
])
def test_invalid_boundary_rejected(mutation):
    value = bundle()
    mutation(value)
    with pytest.raises(ValueError):
        evaluate(value)


def test_policy_identity_and_effective_window():
    value = bundle()
    parameters = deepcopy(value['risk_policy'])
    parameters['policy_id'] = 'other'
    with pytest.raises(ValueError, match='does not match'):
        benchmark_evaluation.evaluate(value, parameters)
    value['risk_policy'].update(effective_from='2026-10-23T00:00:00+08:00', confirmed_at='2026-10-20T00:00:00+08:00')
    with pytest.raises(ValueError, match='not effective'):
        evaluate(value)


def test_collector_index_parser_uses_same_boundary():
    value = bundle()
    assert benchmark_evaluation.index_closes(value['indices'], value['end']) == {c:CLOSES for c,_ in benchmark_input.BENCHMARKS}
    value['indices']['H00300.CSI']['rows'] = None
    assert benchmark_evaluation.index_closes(value['indices'], value['end']) is None
    value['indices']['H00922.CSI']['rows'][0]['close'] = float('inf')
    with pytest.raises(ValueError):
        benchmark_evaluation.index_closes(value['indices'], value['end'])


def test_valuation_before_first_event_returns_calculation_failure():
    value = bundle()
    value['events'] = [['600900', 'buy', '2026-10-22', 100, 100., 0., 0., None, 0]]
    value['prices'] = {}
    result = evaluate(value)
    assert result['status'] == 'unavailable'
    assert result['failures'] == [dict(kind='calculation', code=None, error_code=None, error_type=None)]
