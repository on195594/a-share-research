"""Daily replay over recorded Huadong dividend history and explicit boundary variants.

The surrounding monitor account uses an existing structural fixture; this is not
new market, broker or investment evidence. No upstream calls are made.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from a_share_research.core.market_data import MarketDataResult
from a_share_research.core import providers
from a_share_research.policy import daily_evaluation
from a_share_research.policy.v0 import commands_holdings, commands_monitor, domain, market_quotes, risk_budget
from a_share_research.policy.v0.dividend_input import implemented_rows
from policy_v0.monitor.test_dividend_check import HUADONG_DIVIDENDS, HUADONG_EVENTS
from policy_v0.monitor.test_monitor_snapshot import _seed_holding


NOW = '2026-10-22T15:30:05+08:00'


def provider(rows):
    return dict(rows=rows, source='tushare.dividend', status='ok' if rows is not None else 'failed',
                fetched_at=NOW, source_as_of=None, error_code=None if rows is not None else 'TIMEOUT',
                error_type=None if rows is not None else 'TimeoutError')


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    monkeypatch.setenv('POLICY_V0_DB', str(path))
    monkeypatch.setenv('A_STOCK_CONFIG_FILE', str(tmp_path / 'missing-runtime.env'))
    monkeypatch.delenv('POLICY_V0_ACCOUNT_SHADOW', raising=False)
    monkeypatch.delenv('POLICY_V0_ANALYSIS_SHADOW', raising=False)
    monkeypatch.setattr(domain, 'cst_now', lambda: datetime.fromisoformat(NOW))
    monkeypatch.setattr(domain, 'cst_today', lambda: '2026-10-22')
    monkeypatch.setattr(domain, 'utc_now_iso', lambda: '2026-10-22T07:30:05+00:00')
    _seed_holding(path, '000963', shares=800)
    local = commands_monitor._load_monitor_local_snapshot()
    row = local['holdings'][0]
    normalized = [dict(div_proc='实施', record_date=d['record_date'], ex_date=d['ex_date'],
                       pay_date=d['pay_date'], cash_div_tax=d['cash_div'], stk_div=d['split_ratio'])
                  for d in HUADONG_DIVIDENDS]
    return dict(local=local, quotes={'000963': asdict(market_quotes.PriceQuote(
                    26.21, '2026-10-22', '15:00:00', 'historical-boundary-fixture', previous_close=26.59))},
                portfolio_value=None, industry_result=None, now=NOW, risk_policy=risk_budget.policy(),
                denominator_evidence={}, account_scope_required=False, research_result={'status': 'not_configured'},
                dividend_today='2026-10-22', brief_today='2026-10-22', dividends=[
                    dict(holding_id=row['id'], code=row['code'], name=row['name'], current_shares=row['shares'],
                         events=[list(event) for event in HUADONG_EVENTS], provider=provider(normalized))])


def evaluate(value):
    return daily_evaluation.evaluate(value, value['risk_policy'])


def old_stdout(bundle, monkeypatch, capsys):
    monkeypatch.setattr(commands_holdings, '_parse_portfolio_risk_args', lambda *a, **kw: SimpleNamespace(
        portfolio_value=bundle['portfolio_value'], risk_policy=bundle['risk_policy'],
        denominator_evidence=lambda: bundle['denominator_evidence'], account_scope_required=bundle['account_scope_required']))
    monkeypatch.setattr(commands_monitor, '_load_monitor_local_snapshot', lambda: deepcopy(bundle['local']))
    monkeypatch.setattr(commands_holdings, 'fetch_monitor_price_quotes', lambda codes: (
        {code: None if row is None else market_quotes.PriceQuote(**row) for code, row in bundle['quotes'].items()},
        None if bundle['industry_result'] is None else MarketDataResult(**bundle['industry_result'])))
    by_id = {row['holding_id']: row for row in bundle['dividends']}
    by_code = {row['code']: row for row in bundle['dividends']}
    monkeypatch.setattr(commands_holdings, '_load_lifecycle_events', lambda conn, hid: deepcopy(by_id[hid]['events']))
    class CapturedProvider:
        def __init__(self, **kwargs): pass
        def fetch_dividend_history(self, code):
            value = by_code[code]['provider']
            return SimpleNamespace(value=None if value['rows'] is None else pd.DataFrame(value['rows']), status=value['status'])
    monkeypatch.setattr(providers, 'TushareDividendProvider', CapturedProvider)
    monkeypatch.setattr(commands_holdings.paths, 'tushare_token', lambda: '')
    monkeypatch.setattr(domain, 'cst_now', lambda: datetime.fromisoformat(bundle['now']))
    monkeypatch.setattr(domain, 'cst_today', lambda: bundle['dividend_today'])
    commands_monitor.cmd_daily_brief([])
    return capsys.readouterr()


def test_recorded_entitlement_280_and_exact_old_command_output(bundle, monkeypatch, capsys):
    before = deepcopy(bundle)
    result = evaluate(bundle)
    assert bundle == before
    old = old_stdout(bundle, monkeypatch, capsys)
    assert result['stdout'] == old.out and result['stderr'] == old.err == ''
    assert result['exit_code'] == 0 and result['status'] == 'ready'
    assert [(row['item']['shares'], row['item']['cash']) for row in result['dividends']['findings']] == [(800, 280.)]
    assert '登记日2026-09-17 应收280.00元（运行 dividend-check 核对）' in result['stdout']
    assert bundle == before


@pytest.mark.parametrize('day', ['2026-09-17', '2026-09-18'])
def test_existing_dividend_on_record_or_pay_day_suppresses_missing_claim(bundle, monkeypatch, capsys, day):
    bundle['dividends'][0]['events'].append(['dividend', day, None, None, 0., 0., 280., 0])
    result = evaluate(bundle)
    assert result['dividends']['findings'] == []
    assert result['stdout'] == old_stdout(bundle, monkeypatch, capsys).out


def test_cash_plus_split_partial_entry_retains_old_cash_message(bundle, monkeypatch, capsys):
    bundle['dividends'][0]['provider']['rows'][1]['stk_div'] = .3  # Explicit bonus-share boundary variant.
    bundle['dividends'][0]['events'].append(['dividend', '2026-09-17', None, None, 0., 0., 280., 0])
    result = evaluate(bundle)
    assert result['dividends']['findings'][0]['item']['partially_recorded'] is True
    assert result['stdout'] == old_stdout(bundle, monkeypatch, capsys).out


def test_empty_success_and_provider_failure_remain_distinct(bundle, monkeypatch, capsys):
    bundle['dividends'][0]['provider'] = provider([])
    empty = evaluate(bundle)
    assert empty['dividends'] == dict(checked=1, findings=[], failures=[])
    assert empty['stdout'] == old_stdout(bundle, monkeypatch, capsys).out
    bundle['dividends'][0]['provider'] = provider(None)
    failed = evaluate(bundle)
    assert len(failed['dividends']['failures']) == 1
    assert failed['dividends']['failures'][0]['error_code'] == 'TIMEOUT'
    assert '000963 分红未核对：数据获取失败' in failed['stdout']
    assert failed['stdout'] == old_stdout(bundle, monkeypatch, capsys).out


def test_failure_does_not_hide_other_holdings_findings(bundle, tmp_path, monkeypatch, capsys):
    second_id = _seed_holding(tmp_path / 'app.db', '600036', shares=500)
    bundle['local'] = commands_monitor._load_monitor_local_snapshot()
    second = next(row for row in bundle['local']['holdings'] if row['id'] == second_id)
    bundle['dividends'].append(dict(holding_id=second_id, code=second['code'], name=second['name'],
        current_shares=second['shares'], events=[], provider=provider(None)))
    bundle['quotes']['600036'] = None
    result = evaluate(bundle)
    assert len(result['dividends']['findings']) == len(result['dividends']['failures']) == 1
    assert '280.00元' in result['stdout'] and '600036 分红未核对' in result['stdout']
    assert result['stdout'] == old_stdout(bundle, monkeypatch, capsys).out


def test_no_today_quote_keeps_old_silence_but_retains_dividend_evidence(bundle, monkeypatch, capsys):
    bundle['quotes']['000963']['quote_date'] = '2026-10-21'
    result = evaluate(bundle)
    assert result['status'] == 'silent' and result['stdout'] == ''
    assert result['dividends']['findings'][0]['item']['cash'] == 280.
    assert old_stdout(bundle, monkeypatch, capsys).out == ''


@pytest.mark.parametrize('mutate', [
    lambda b: b.update(research_result={'status': 'ok', 'cards': []}),
    lambda b: b.update(dividend_today='2026-02-30'),
    lambda b: b.update(dividend_today='2026-10-21'),
    lambda b: b.update(brief_today='2026-10-21'),
    lambda b: b.update(extra='unknown'),
    lambda b: b['dividends'].clear(),
    lambda b: b['dividends'].append(deepcopy(b['dividends'][0])),
    lambda b: b['dividends'][0].update(holding_id=99),
    lambda b: b['dividends'][0].update(code='600000'),
    lambda b: b['dividends'][0].update(name='different'),
    lambda b: b['dividends'][0].update(current_shares=900),
    lambda b: b['local']['holdings'][0].update(id=True),
    lambda b: b['dividends'][0]['events'][0].pop(),
    lambda b: b['dividends'][0]['events'][0].__setitem__(2, '800'),
    lambda b: b['dividends'][0]['events'][0].__setitem__(3, float('inf')),
    lambda b: b['dividends'][0]['events'].reverse(),
    lambda b: b['dividends'][0]['provider'].update(fetched_at='2026-10-22'),
    lambda b: b['dividends'][0]['provider'].update(source_as_of='20260230'),
    lambda b: b['dividends'][0]['provider']['rows'][0].update(record_date='2026-02-30'),
    lambda b: b['dividends'][0]['provider'].update(unexpected='field'),
])
def test_invalid_capture_is_rejected_not_rendered_as_complete(bundle, mutate):
    mutate(bundle)
    with pytest.raises(ValueError): evaluate(bundle)


def test_parameter_mismatch_rejected(bundle):
    with pytest.raises(ValueError, match='does not match'):
        daily_evaluation.evaluate(bundle, bundle['risk_policy'] | {'max_position_risk_pct': 3.})


def test_duplicate_local_code_still_keeps_distinct_holding_checks(bundle):
    second = deepcopy(bundle['local']['holdings'][0])
    second['id'] = 2
    bundle['local']['holdings'].append(second)
    check = deepcopy(bundle['dividends'][0])
    check['holding_id'] = 2
    bundle['dividends'].append(check)
    result = evaluate(bundle)
    assert [row['holding_id'] for row in result['dividends']['findings']] == [1, 2]
    bundle['dividends'].reverse()
    with pytest.raises(ValueError, match='code/id order'): evaluate(bundle)


def test_implemented_filter_matches_recorded_rows_and_missing_numeric_variants(bundle):
    rows = bundle['dividends'][0]['provider']['rows']
    assert implemented_rows(rows) == HUADONG_DIVIDENDS
    base = rows[1]
    assert implemented_rows([base | {'div_proc': '预案'}, base | {'record_date': None},
                             base | {'cash_div_tax': None}, base | {'cash_div_tax': '0.35'}]) == []
    assert implemented_rows([base | {'cash_div_tax': None, 'stk_div': .3}]) == [
        HUADONG_DIVIDENDS[1] | {'cash_div': 0., 'split_ratio': .3}]
