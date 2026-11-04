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
    assert len(rows) == 17
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
