"""Boundary checks over captured daily_basic and report_rc responses; no upstream calls."""
import json
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import sources

DATA = Path(__file__).parent / "data"
ROWS = json.loads((DATA / "report_rc_603606_20261031.json").read_text())
MARKET = json.loads((DATA / "600519-financials.json").read_text())["market"]


def test_report_rc_boundary_preserves_verified_np_unit_and_dates():
    rows = sources.report_rc_rows(pd.DataFrame(ROWS), "603606")
    assert len(rows) == 69
    huatai = next(r for r in rows if r["org_name"] == "华泰证券" and r["quarter"] == "2028Q4"
                 and r["report_date"] == "2026-10-30")
    assert huatai["np"] == 217800
    assert huatai["np"] / 10000 == 21.78
    assert "tp" not in huatai and "report_title" not in huatai


def test_report_rc_boundary_rejects_mismatched_company_and_schema():
    with pytest.raises(sources.SourceError, match="其他公司"):
        sources.report_rc_rows(pd.DataFrame(ROWS), "600519")
    with pytest.raises(sources.SourceError, match="缺少"):
        sources.report_rc_rows(pd.DataFrame(ROWS).drop(columns="np"), "603606")


class CapturedReportRC:
    """Replay the saved real response at the existing cache boundary."""
    def __init__(self):
        self.calls = 0

    def query(self, api, **params):
        assert api == "report_rc" and params["ts_code"] == "603606.SH"
        self.calls += 1
        return pd.DataFrame(ROWS)


def test_historical_report_rc_uses_existing_raw_cache(monkeypatch):
    monkeypatch.delenv("TUSHARE_CACHE", raising=False)
    first, second = CapturedReportRC(), CapturedReportRC()
    params = {"ts_code": "603606.SH", "start_date": "20260701", "end_date": "20261031"}
    original = sources.tushare_query(first, "report_rc", **params)
    restored = sources.tushare_query(second, "report_rc", **params)
    pd.testing.assert_frame_equal(original, restored, check_exact=True)
    assert first.calls == 1 and second.calls == 0


def test_pe_reference_requires_distinct_positive_trading_days():
    # This saved market response is only one day; repeating it cannot meet coverage.
    for frame in (pd.DataFrame([MARKET]), pd.DataFrame([MARKET] * 250)):
        with pytest.raises(sources.SourceError, match="仅 1 个交易日，少于 250"):
            sources.pe_quantiles(frame, "600519", MARKET["trade_date"])


@pytest.mark.parametrize("value", [None, 0, -1, float("inf")])
def test_pe_reference_drops_nonpositive_and_missing_observations(value):
    frame = pd.DataFrame([MARKET])
    frame["pe_ttm"] = value  # Mutation of the captured row, not a fabricated history.
    with pytest.raises(sources.SourceError, match="仅 0 个交易日"):
        sources.pe_quantiles(frame, "600519", MARKET["trade_date"])


def test_pe_reference_rejects_wrong_company():
    with pytest.raises(sources.SourceError, match="其他公司"):
        sources.pe_quantiles(pd.DataFrame([MARKET]), "603606", MARKET["trade_date"])


def test_report_rc_limit_message_classification():
    assert sources._report_rc_limited(Exception("抱歉，您每分钟最多访问该接口1次"))
    assert not sources._report_rc_limited(Exception("没有访问权限"))
