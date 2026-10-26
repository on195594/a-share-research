"""Latest disclosed cumulative profit from the existing real TuShare captures."""

import json
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import backtest
from a_share_research.backtest_experiment import quarter_signal


def test_quarter_signal_respects_disclosure_date_and_same_period():
    captured = json.loads((Path(__file__).parent / 'data/screen-financials.json').read_text())
    ind = pd.DataFrame(captured['companies']['002648.SZ']['indicator'])
    old = quarter_signal(ind, '20260301')
    assert old['period'] == '20250930' and old['yoy'] == pytest.approx(0.03653463864)
    latest = quarter_signal(ind, '20261023')
    assert latest['period'] == '20260930' and latest['yoy'] == pytest.approx(1.12894754853)
    # A real disclosure with its prior-period rows unavailable must remain unknown, not zero.
    partial = ind[ind['end_date'] == '20260930']
    assert quarter_signal(partial, '20261023')['yoy'] is None
    loss = json.loads((Path(__file__).parent / 'data/600694-quarter-profit.json').read_text())
    # 大商股份: 614,550,537.23 / 1,006,768,293.79 - 1, below the fixed -20% threshold.
    signal = quarter_signal(pd.DataFrame(loss['rows']), loss['as_of'])
    assert signal['yoy'] == pytest.approx(-0.38958095818)


def test_offline_cache_miss_fails_without_a_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, 'CACHE', tmp_path)
    with pytest.raises(backtest.ScreenError, match='离线缓存缺失'):
        backtest.cached(None, 'fina_indicator', ts_code='002648.SZ')
    assert not list(tmp_path.iterdir())
