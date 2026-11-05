"""Replay live TuShare constituent snapshots; corruptions must fail closed."""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from statistics import median

import pytest

from a_share_research import checking, evidence, industry, sources
from a_share_research.research_tools import ResearchTools, calculate
from support.reports import MATERIAL, report

DATA = json.loads(Path(__file__).with_name('data').joinpath('industry-20261104.json').read_text())


def test_live_aggregates_have_exact_reproducible_inputs():
    rows = industry.industry_rows(DATA)
    industry.validate_rows(rows, rows)
    assert len(rows) == 20
    by_metric = {r['id'].rsplit('.', 1)[-1]: r for r in rows}
    current = max(DATA['daily'])
    pe = [Decimal(str(r['pe_ttm'])) for r in DATA['daily_basic'][current]['rows'] if r['pe_ttm'] and r['pe_ttm'] > 0]
    assert Decimal(next(reversed(by_metric['pe_median']['exact_values'].values()))) == median(pe)
    assert by_metric['pe_percentile']['source']['valid_samples'] == 60
    for window in (20, 60, 120):
        row = by_metric[f'median_{window}']
        start, end = row['source']['start'], row['source']['end']
        prices = {d: {r['ts_code']: Decimal(str(r['close'])) for r in DATA['daily'][d]['rows']} for d in (start, end)}
        factors = {d: {r['ts_code']: Decimal(str(r['adj_factor'])) for r in DATA['adj_factor'][d]['rows']} for d in (start, end)}
        returns = {c: (prices[end][c] * factors[end][c] / (prices[start][c] * factors[start][c])-1)*100 for c in row['source']['included']}
        assert Decimal(next(iter(row['exact_values'].values()))) == median(returns.values())
        weights = {r['ts_code']: Decimal(str(r['circ_mv'])) for r in DATA['daily_basic'][start]['rows'] if r['ts_code'] in row['source']['weighted_members']}
        assert Decimal(next(iter(by_metric[f'weighted_{window}']['exact_values'].values()))) == sum(returns[c]*weights[c] for c in sorted(weights))/sum(weights.values())
        assert Decimal(next(iter(by_metric[f'company_{window}']['exact_values'].values()))) == returns[DATA['code']]
        assert len(row['source']['sessions']) == window+1


def test_industry_citations_calculator_and_period_scope():
    material = deepcopy(MATERIAL)
    rows = industry.industry_rows(DATA)
    ResearchTools(material, None)._condition_rows(rows)
    row = next(r for r in rows if r['id'].endswith('.median_20'))
    benchmark = next(r for r in rows if r['id'].endswith('.benchmark_20'))
    period = next(iter(row['values']))
    calc = calculate(material.table, '行业相对大盘', 'difference', {'ref': row['id'], 'period': period}, {'ref': benchmark['id'], 'period': period})
    material.table.append(calc)
    assert Decimal(calc['exact_values']['result']) == Decimal(row['exact_values'][period])-Decimal(benchmark['exact_values'][period])
    draft = report()
    draft['stance']['text'] = f"近20个交易日行业收益中位数为{row['values'][period]}%。"
    draft['stance']['cites'] = [{'ref': row['id'], 'period': period}]
    assert not [p for p in checking.check(draft, material) if p['where']=='stance']
    draft['stance']['text'] = '近99999个交易日行业收益987654.32%。'
    assert any(p['where']=='stance' for p in checking.check(draft, material))
    assert evidence.citation_errors([{'ref': row['id'], 'period': '2026-01-01'}], {}, material.table)
    with pytest.raises(ValueError, match='可用期间'):
        calculate(material.table, '错配期间', 'difference', {'ref': row['id'], 'period': '2026-01-01'}, {'ref': benchmark['id'], 'period': period})
    # Report 91 (300750, run 111) failed on "近20、60、120个交易日…中证800": window lists and the
    # benchmark name are identifiers of the cited rows, not figures.
    cited = {w: {k: next(r for r in rows if r['id'].endswith(f'.{k}_{w}')) for k in ('median', 'benchmark')}
             for w in (20, 60, 120)}
    draft['stance']['cites'] = [{'ref': cited[w][k]['id'], 'period': period} for w in cited for k in cited[w]]
    figures = '、'.join(cited[w]['median']['values'][period] for w in cited)
    draft['stance']['text'] = f"近20、60、120个交易日，行业收益中位数分别为{figures}%，对照中证800价格指数。"
    assert not [p for p in checking.check(draft, material) if p['where']=='stance']
    # Mutations: a window the cites do not measure, or an unsupported figure, still fails.
    draft['stance']['text'] = f"近20、60、90个交易日，行业收益中位数分别为{figures}%，对照中证800价格指数。"
    assert any('90' in p['problem'] for p in checking.check(draft, material) if p['where']=='stance')
    draft['stance']['text'] = f"近20、60、120个交易日，行业收益中位数分别为{figures}%，中证800下跌800.5%。"
    assert any('800.5' in p['problem'] for p in checking.check(draft, material) if p['where']=='stance')
    draft['stance']['cites'] = [{'ref': row['id'], 'period': period}]
    draft['stance']['text'] = f"近20个交易日行业收益中位数为{row['values'][period]}%，跑赢沪深300。"
    assert any('300' in p['problem'] for p in checking.check(draft, material) if p['where']=='stance')
    # A chained comparison must not conceal corrupted constituent evidence.
    row['values'][period] = '987654.32'
    assert evidence.citation_errors([{'ref': calc['id']}], {}, material.table)


@pytest.mark.parametrize('mutation', ['value', 'period', 'members', 'raw_date', 'raw_member', 'query_period'])
def test_mutated_industry_evidence_is_rejected(mutation):
    rows = industry.industry_rows(deepcopy(DATA))
    row = next(r for r in rows if r['id'].endswith('.median_20'))
    dataset = rows[0]['source']['dataset']
    if mutation == 'value':
        row['exact_values'][next(iter(row['exact_values']))] = '987654.32'
    elif mutation == 'period':
        row['source']['start'] = row['source']['end']
    elif mutation == 'members':
        row['source']['members'] = row['source']['members'][1:]
    elif mutation == 'raw_date':
        dataset['daily'][row['source']['start']]['rows'][0]['trade_date'] = row['source']['end']
    elif mutation == 'raw_member':
        dataset['daily'][row['source']['start']]['rows'][0]['ts_code'] = '000906.SH'
    else:
        dataset['daily'][row['source']['start']]['params']['trade_date'] = row['source']['end']
    with pytest.raises(ValueError, match='行业'):
        industry.validate_rows(rows, rows)
    assert evidence.citation_errors([{'ref': row['id']}], {}, rows)


def test_historical_cache_identity_and_current_membership(monkeypatch):
    monkeypatch.delenv('TUSHARE_CACHE')
    day = max(DATA['daily_basic'])
    params = DATA['daily_basic'][day]['params']
    assert sources._tushare_cache_key('daily_basic', params)
    assert sources._tushare_cache_key('daily_basic', params) != sources._tushare_cache_key('daily_basic', params | {'trade_date': min(DATA['daily_basic'])})
    assert sources._tushare_cache_key('index_member_all', DATA['membership']['params'])


def test_peer_weighted_return_excludes_the_company():
    rows = {r['id'].rsplit('.', 1)[-1]: r for r in industry.industry_rows(DATA)}
    code = DATA['code']
    for window in (20, 60, 120):
        row = rows[f'weighted_ex_{window}']
        start, end = row['source']['start'], row['source']['end']
        close = {d: {r['ts_code']: Decimal(str(r['close'])) for r in DATA['daily'][d]['rows']} for d in (start, end)}
        adj = {d: {r['ts_code']: Decimal(str(r['adj_factor'])) for r in DATA['adj_factor'][d]['rows']} for d in (start, end)}
        weight = {r['ts_code']: Decimal(str(r['circ_mv'])) for r in DATA['daily_basic'][start]['rows']
                  if r['ts_code'] in row['source']['weighted_members'] and r['ts_code'] != code}
        expected = sum((close[end][c] * adj[end][c] / (close[start][c] * adj[start][c]) - 1) * 100 * weight[c] for c in sorted(weight)) / sum(weight[c] for c in sorted(weight))
        assert Decimal(next(iter(row['exact_values'].values()))) == expected


def test_saved_reports_without_peer_rows_still_replay():
    rows = [r for r in industry.industry_rows(DATA) if '.weighted_ex_' not in r['id']]
    assert len(rows) == 17
    industry.validate_rows(rows, rows)


def test_tool_compares_same_basis_returns_only(monkeypatch):
    rows = industry.industry_rows(deepcopy(DATA))
    monkeypatch.setattr(industry, 'industry_trend', lambda code, level: {'rows': rows, 'basis': industry.BASIS, 'metrics': {}})
    material = deepcopy(MATERIAL)
    result = ResearchTools(material, None).industry_trend(material.code[:6])
    pairs = {tuple(i['ref'].rsplit('.', 1)[-1].rsplit('_', 1)[0] for i in c['calculation']['inputs']) for c in result['changes']}
    assert len(result['changes']) == 12
    assert pairs == {('weighted', 'benchmark'), ('weighted_ex', 'benchmark'), ('company', 'median'), ('company', 'weighted_ex')}
    by_id = {r['id'].rsplit('.', 1)[-1]: r for r in rows}
    period = next(iter(by_id['weighted_ex_60']['values']))
    change = next(c for c in result['changes'] if [i['ref'].rsplit('.', 1)[-1] for i in c['calculation']['inputs']] == ['company_60', 'weighted_ex_60'])
    assert Decimal(change['exact_values']['result']) == (Decimal(by_id['company_60']['exact_values'][period])
                                                         - Decimal(by_id['weighted_ex_60']['exact_values'][period]))
