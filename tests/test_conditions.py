"""Exact live TuShare captures, with explicit corruption mutations for boundary checks."""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import market, checking, conditions, sources
from a_share_research.research_tools import ResearchTools, price_changes
from support.reports import MATERIAL, report

DATA = json.loads(Path(__file__).with_name('data').joinpath('external-conditions-20261104.json').read_text())


def rows(api='fut_daily'):
    sample = DATA['apis'][api]
    fields = {'close': ('铝主力连续收盘价（换月未复权）', '人民币元/吨')} if api == 'fut_daily' else next(v[1] for v in conditions.MACRO.values() if v[0] == api)
    key = 'trade_date' if api == 'fut_daily' else ('date' if api.startswith('shibor') else 'month')
    return conditions.series_rows(pd.DataFrame(sample['rows']), api=api, params=sample['params'], key=key,
                                  fields=fields, prefix=f'X.{api}', retrieved_at=DATA['captured_at'])


@pytest.mark.parametrize('api', DATA['apis'])
def test_exact_series_values_and_provenance(api):
    parsed = rows(api)
    sample = DATA['apis'][api]
    for row in parsed:
        assert row['source']['api'] == api and row['source']['params'] == sample['params']
        assert row['values'] == row['exact_values']
        field = row['source']['field']
        assert sorted(Decimal(v) for v in row['values'].values()) == sorted(Decimal(str(r[field])) for r in sample['rows'] if r[field] is not None)


def test_changes_register_and_check_without_granting_unseen_numbers():
    mat = deepcopy(MATERIAL)
    tools = ResearchTools(mat, None)
    row = rows()[0]
    tools._condition_rows([row])
    results = price_changes(mat.table, row)
    assert not results['gaps']
    mat.table.extend(results['changes'].values())
    days = sorted(row['values'])
    for window, calc in results['changes'].items():
        expected = (Decimal(row['values'][days[-1]]) / Decimal(row['values'][days[-int(window)-1]]) - 1) * 100
        assert Decimal(calc['exact_values']['result']) == expected
        assert calc['calculation']['inputs'][0]['ref'] == row['id']
        assert checking._checked_figures(f"{window}个交易日，{window}%", mat, [{'ref': calc['id']}]) == [(float(window), 0)]
        draft = report()
        draft['stance']['text'] = f"铝主力连续收盘价近{window}个交易日变化{calc['values']['result']}%，换月影响仍需考虑。"
        draft['stance']['cites'] = [{'ref': calc['id']}]
        assert not [p for p in checking.check(draft, mat) if p['where'] == 'stance']
        draft['stance']['text'] = f"近99999个交易日变化{calc['values']['result']}%。"
        assert any(p['where'] == 'stance' for p in checking.check(draft, mat))
        draft['stance']['text'] = '行业涨幅987654.32%。'
        assert any(p['where'] == 'stance' and '987654' in p['problem'] for p in checking.check(draft, mat))
    draft['stance']['text'] = '铝主力连续收盘价变化。'
    draft['stance']['cites'] = [{'ref': 'X.absent.close'}]
    assert any(p['where'] == 'stance' for p in checking.check(draft, mat))
    corrupted = deepcopy(row)
    corrupted['values'][days[-1]] = '987654.32'
    with pytest.raises(ValueError, match='不同版本'):
        tools._condition_rows([corrupted])


def test_boundary_rejects_duplicate_bad_number_and_outside_period():
    sample = DATA['apis']['fut_daily']
    for corrupted in ([*sample['rows'], sample['rows'][0]],
                      [sample['rows'][0] | {'close': 'not-a-number'}],
                      [sample['rows'][0] | {'trade_date': '20270101'}]):
        with pytest.raises(sources.SourceError):
            conditions.series_rows(pd.DataFrame(corrupted), api='fut_daily', params=sample['params'], key='trade_date',
                                  fields={'close': ('铝', '人民币元/吨')}, prefix='X.AL', retrieved_at=DATA['captured_at'])


def test_macro_cache_keys_include_series_and_month_range(monkeypatch):
    monkeypatch.delenv("TUSHARE_CACHE")
    a = DATA['apis']['cn_m']['params']
    assert market._tushare_cache_key('cn_m', a)
    assert market._tushare_cache_key('cn_m', a) != market._tushare_cache_key('cn_pmi', a)
    assert market._tushare_cache_key('cn_m', a) != market._tushare_cache_key('cn_m', a | {'end_m': '202609'})


def test_lpr_cooldown_is_shared_and_prevents_an_upstream_call(monkeypatch):
    from contextlib import closing
    from a_share_research import db
    monkeypatch.delenv('TUSHARE_CACHE')
    with closing(market.report_rc_connect()) as conn:
        conn.execute("INSERT OR REPLACE INTO tushare_cache VALUES ('shibor_lpr:last-call', '{}', ?)", (db.now(),))
    # No provider is needed: the reservation must reject before any upstream access.
    with pytest.raises(sources.SourceError, match='每小时一次'):
        market.query(None, 'shibor_lpr', **DATA['apis']['shibor_lpr']['params'])


def test_missing_cites_still_reports_a_problem_instead_of_crashing():
    draft = report()
    draft['stance']['cites'] = None
    assert any(p['where'] == 'stance' and '没有出处' in p['problem'] for p in checking.check(draft, MATERIAL))


def test_pmi_fields_mean_what_their_labels_say():
    # NBS 2026-10 release: production 49.8, new orders 49.4; 50.2 and 48.4 are large and medium firms.
    by_label = {r['label']: r['values']['2026-10'] for r in rows('cn_pmi')}
    assert by_label == {'制造业PMI': '49.3', '制造业生产指数': '49.8', '制造业新订单指数': '49.4'}
