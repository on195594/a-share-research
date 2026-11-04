"""Captured production table, with scenario inputs sampled from actual report_rc estimates.

These are deterministic arithmetic inputs, not an endorsed valuation or a model research run.
"""
from copy import deepcopy
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / 'data'
RECORD = next(r['report'] for r in json.loads((DATA / 'continuation-records.json').read_text())['records']
              if r['report']['id'] == 37)
BODY = RECORD['body']
ROWS = json.loads((DATA / 'report_rc_603606_20261031.json').read_text())
STORED = json.loads((DATA / 'valuation-stored-20261101.json').read_text())['records']


def assumptions():
    # Use same-company actual estimates/PEs as test-only hypothetical inputs. Production never
    # passes report_rc to the model or uses it as scenario input.
    scenarios = {}
    # Probabilities are a test assumption for the v2 shadow rule, not derived from the estimates.
    for name, org, probability in [('保守', '华泰证券', 25), ('中性', '平安证券', 50), ('乐观', '招商证券', 25)]:
        estimates = [max((r for r in ROWS if r['org_name'] == org and r['quarter'] == f'{y}Q4'),
                         key=lambda r: r['report_date']) for y in (2026, 2027, 2028)]
        scenarios[name] = {'profit': [r['np'] / 10000 for r in estimates], 'exit_pe': estimates[-1]['pe'],
                           'probability': probability}
    # The fixture's real annual dividend yield is used only as a test payout assumption.
    payout = next(iter(next(r for r in BODY['table'] if r['id'] == 'M.dv_ttm')['values'].values()))
    return deepcopy({'basis': 'net_profit', 'years': [2026, 2027, 2028], 'scenarios': scenarios,
                     'payout_pct': float(payout),
                     'rationale': [{'text': '以年报归母净利润为基期观察经营兑现。',
                                    'cites': [{'ref': 'T.net_profit', 'period': '2025-12-31'}]}]})


def with_probabilities(value, weights=(25, 50, 25)):
    """Stored pre-v2 assumptions plus test-only scenario probabilities; v1 outputs must not depend on them."""
    value = deepcopy(value)
    if 'scenarios' in value:
        for name, weight in zip(('保守', '中性', '乐观'), weights):
            value['scenarios'][name]['probability'] = weight
    return value
