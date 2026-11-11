"""
fetcher.py 单元测试
覆盖：
  - cmd_batch 只捕获 Exception，不吞 KeyboardInterrupt
  - _extract_fin_fields 从 DataFrame 正确提取各财务字段
"""

import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest
from a_share_research.core.providers import QuoteObservation

from a_share_research.policy.v0 import db, domain, fetcher, store
from policy_v0.helpers import set_valid_fundamentals


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_fetcher_cache.db"
    monkeypatch.setenv("CACHE_DB_PATH", str(db_file))
    # fetcher 内部也引用 list_codes，需要同步 patch
    monkeypatch.setattr(store, "list_codes", lambda: ["600519", "000001"])
    fetcher._fetch_tushare_industry_map.cache_clear()
    yield str(db_file)
    fetcher._fetch_tushare_industry_map.cache_clear()


def _patch_tushare_provider(monkeypatch, provider_factory):
    """为函数内懒加载 import 注入 fake a_stock_lib provider。"""
    root_module = ModuleType("a_share_research.core")
    providers_module = ModuleType("a_share_research.core.providers")
    tushare_module = ModuleType("a_share_research.core.providers.tushare_fundamentals")
    root_module.__path__ = []
    providers_module.__path__ = []
    tushare_module.TushareFundamentalsProvider = lambda **kwargs: provider_factory()
    root_module.providers = providers_module
    providers_module.tushare_fundamentals = tushare_module

    monkeypatch.setitem(sys.modules, "a_share_research.core", root_module)
    monkeypatch.setitem(sys.modules, "a_share_research.core.providers", providers_module)
    monkeypatch.setitem(
        sys.modules,
        "a_share_research.core.providers.tushare_fundamentals",
        tushare_module,
    )


@pytest.mark.parametrize("name", ["info", "financials", "dividends", "price_history"])
@pytest.mark.parametrize("source", ["tushare", "akshare"])
def test_structured_fetchers_honor_data_source(monkeypatch, name, source):
    """每条结构化抓取入口都必须选择对应 provider，不能静默走 AKShare。"""
    selected = object()
    other = object()
    monkeypatch.setattr(fetcher, "DATA_SOURCE", source)
    monkeypatch.setattr(
        fetcher,
        f"_fetch_{name}_tushare",
        lambda code: selected if source == "tushare" else other,
    )
    monkeypatch.setattr(
        fetcher,
        f"_fetch_{name}_akshare",
        lambda code: selected if source == "akshare" else other,
    )

    assert getattr(fetcher, f"_fetch_{name}")("600519") is selected


@pytest.mark.parametrize("credential_source", ["config", "environment", "missing"])
@pytest.mark.parametrize(
    "function_name, provider_name, method_name, args",
    [
        (
            "_fetch_trading_dates_tushare",
            "TushareMarketDataProvider",
            "fetch_trade_calendar",
            (),
        ),
        (
            "_fetch_info_tushare",
            "TushareValuationProvider",
            "fetch_valuation_history",
            ("600519",),
        ),
        (
            "_fetch_tushare_industry_map",
            "TushareFundamentalsProvider",
            "fetch_industry_map",
            (),
        ),
        (
            "_fetch_financials_tushare",
            "TushareFinancialProvider",
            "fetch_indicator_history",
            ("600519",),
        ),
        (
            "_fetch_dividends_tushare",
            "TushareDividendProvider",
            "fetch_dividend_history",
            ("600519",),
        ),
        (
            "_fetch_price_history_tushare",
            "TushareValuationProvider",
            "fetch_valuation_history",
            ("600519",),
        ),
    ],
)
def test_tushare_adapters_resolve_private_config_without_exporting_secrets(
    tmp_path,
    monkeypatch,
    credential_source,
    function_name,
    provider_name,
    method_name,
    args,
):
    import os
    import a_share_research.core.providers as providers

    config = tmp_path / "runtime.env"
    config.write_text(
        "TUSHARE_TOKEN=config-test-token\n" if credential_source != "missing" else ""
    )
    config.chmod(0o600)
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(config))
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    if credential_source == "environment":
        monkeypatch.setenv("TUSHARE_TOKEN", "environment-test-token")
    before = os.environ.get("TUSHARE_TOKEN")
    tokens = []

    def capture_token(self, *args, **kwargs):
        tokens.append(self.token)
        return SimpleNamespace(status="failed", value=None, error_code="TEST_OFFLINE")

    monkeypatch.setattr(getattr(providers, provider_name), method_name, capture_token)
    monkeypatch.setattr(
        fetcher, "timed_call", lambda func, *args, **kwargs: func(*args)
    )
    getattr(fetcher, function_name)(*args)

    expected = {
        "config": "config-test-token",
        "environment": "environment-test-token",
        "missing": "",
    }
    assert tokens == [expected[credential_source]]
    assert os.environ.get("TUSHARE_TOKEN") == before


def test_current_pb_uses_the_same_report_period_bps_as_pb_history(monkeypatch):
    assert not hasattr(fetcher, "_fetch_pb_baidu")
    results = {"bps": 6.9176, "eps": 1.95}
    null_reasons = {}

    fetcher._fetch_pb_pe_data("601899", 35.15, results, null_reasons)

    assert results["pb"] == 5.08
    assert results["pe_static"] == 18.03
    assert results["pe_ttm"] == 18.03
    assert fetcher.FIELDS["pe_ttm"][1] == "compatibility"
    assert fetcher.FIELDS["pb"][1] == "computed"
    assert "pb" not in null_reasons


def _latest_bps_snapshot(*, period: str = "2026半年报", bps: float | None = 26.3208):
    return {
        "report_period": period,
        "announcement_date": "2026-08-29",
        "source": "tushare.fina_indicator+income+balancesheet",
        "fields": {"bps": {"value": bps, "status": "ok", "direction": "missing"}},
    }


def test_valuation_compatibility_treats_newer_bps_as_update_not_global_failure():
    result = fetcher.compute_valuation_compatibility(
        current_price=86.75,
        annual_pb=3.48,
        annual_bps=24.9573,
        annual_period="2025年报",
        latest_snapshot=_latest_bps_snapshot(),
        quote_source="sina",
        quote_as_of="2026-09-02T11:30:00",
    )

    assert result == {
        "pb_cache": 3.48,
        "pb_cache_bps": 24.9573,
        "pb_cache_bps_period": "2025年报",
        "pb_latest_report": 3.3,
        "canonical_pb": 3.3,
        "pb_percentile_eligible": False,
        "latest_report_bps": 26.3208,
        "latest_report_period": "2026半年报",
        "relative_difference_pct": 5.17,
        "status": "latest_report_update",
        "timing_eligible": True,
        "reason_code": "latest_bps_period_update",
        "reason": "latest-report BPS is newer; only PB-percentile frameworks must wait for a compatible series",
        "quote_source": "sina",
        "quote_as_of": "2026-09-02T11:30:00",
        "latest_report_bps_source": "tushare.fina_indicator+income+balancesheet",
        "latest_report_announcement_date": "2026-08-29",
    }


def test_valuation_compatibility_includes_decimal_safe_exact_two_percent():
    result = fetcher.compute_valuation_compatibility(
        current_price=29.4,
        annual_pb=3.0,
        annual_bps=10.0,
        annual_period="2025年报",
        latest_snapshot=_latest_bps_snapshot(period="2025年报", bps=10.0),
        quote_source="sina",
        quote_as_of="2026-09-02T11:30:00",
    )

    assert result["pb_latest_report"] == 2.94
    assert result["relative_difference_pct"] == 2.0
    assert result["status"] == "compatible"
    assert result["timing_eligible"] is True
    assert result["pb_percentile_eligible"] is True
    assert result["reason_code"] == "within_2pct"


def test_valuation_compatibility_uses_frozen_python_round_before_decimal_gate():
    result = fetcher.compute_valuation_compatibility(
        current_price=1.02,
        annual_pb=0.42,
        annual_bps=2.43,
        annual_period="2025年报",
        latest_snapshot=_latest_bps_snapshot(period="2026半年报", bps=2.4),
        quote_source="sina",
        quote_as_of="2026-09-02T11:30:00",
    )

    assert result["pb_latest_report"] == 0.43
    assert result["relative_difference_pct"] == 2.38
    assert result["status"] == "latest_report_update"
    assert result["timing_eligible"] is True
    assert result["pb_percentile_eligible"] is False


def test_valuation_compatibility_fails_same_period_value_mismatch():
    result = fetcher.compute_valuation_compatibility(
        current_price=28.0,
        annual_pb=3.0,
        annual_bps=10.0,
        annual_period="2025年报",
        latest_snapshot=_latest_bps_snapshot(period="2025年报", bps=10.0),
        quote_source="sina",
        quote_as_of="2026-09-02T11:30:00",
    )

    assert result["pb_latest_report"] == 2.8
    assert result["status"] == "incomplete"
    assert result["timing_eligible"] is False
    assert result["pb_percentile_eligible"] is False
    assert result["reason_code"] == "same_period_value_mismatch"


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"quote_source": ""}, "missing_or_invalid_input"),
        ({"quote_source": "   "}, "missing_or_invalid_input"),
        ({"quote_as_of": None}, "missing_or_invalid_input"),
        ({"quote_as_of": "2026-09-02"}, "missing_or_invalid_input"),
        ({"annual_period": "bad-period"}, "missing_or_invalid_input"),
        (
            {"latest_snapshot": _latest_bps_snapshot(period="bad-period")},
            "missing_or_invalid_input",
        ),
        (
            {"latest_snapshot": _latest_bps_snapshot(bps=None)},
            "missing_or_invalid_input",
        ),
        ({"annual_pb": float("nan")}, "missing_or_invalid_input"),
    ],
)
def test_valuation_compatibility_fails_closed_on_missing_or_invalid_inputs(
    overrides, expected_reason
):
    kwargs = {
        "current_price": 30.0,
        "annual_pb": 3.0,
        "annual_bps": 10.0,
        "annual_period": "2025年报",
        "latest_snapshot": _latest_bps_snapshot(),
        "quote_source": "sina",
        "quote_as_of": "2026-09-02T11:30:00",
    }
    kwargs.update(overrides)

    result = fetcher.compute_valuation_compatibility(**kwargs)

    assert result["status"] == "incomplete"
    assert result["timing_eligible"] is False
    assert result["reason_code"] == expected_reason


def test_valuation_compatibility_round_trips_in_existing_json_payload():
    compatibility = {
        "status": "period_mismatch",
        "timing_eligible": False,
        "reason_code": "different_period_value_mismatch",
    }
    set_valid_fundamentals(
        "002594",
        "比亚迪",
        "汽车整车",
        {"pb": 3.48, "valuation_compatibility": compatibility},
    )

    stored = store.get_fundamentals("002594")

    assert stored is not None
    assert stored["valuation_compatibility"] == compatibility


def test_build_cache_payload_persists_valuation_compatibility_provenance():
    compatibility = {
        "status": "compatible",
        "timing_eligible": True,
        "reason_code": "within_2pct",
    }
    results = {
        "valuation_compatibility": compatibility,
        "_quote_as_of": "2026-09-02T11:30:00",
    }

    fetcher._build_cache_payload(
        "002594", "比亚迪", "汽车整车", results, {}, "2025年报"
    )
    stored = store.get_fundamentals("002594")

    assert stored is not None
    assert stored["valuation_compatibility"] == compatibility
    assert stored["field_provenance"]["valuation_compatibility"] == {
        "source": "computed",
        "as_of": "2026-09-02T11:30:00",
        "status": "ok",
    }


def test_legacy_payload_remains_absent_without_read_time_backfill():
    set_valid_fundamentals("600000", "旧缓存", "制造", {"pb": 1.2})

    first_read = store.get_fundamentals("600000")
    second_read = store.get_fundamentals("600000")

    assert first_read is not None and second_read is not None
    assert "valuation_compatibility" not in first_read
    assert "valuation_compatibility" not in second_read


def test_preloaded_price_history_is_trimmed_to_requested_percentile_window():
    """A 5-year request must not silently score the full preloaded 10-year frame."""
    import pandas as pd

    fin_df = _make_fin_df_with_period(2014, eps=1.0, bps=10.0)
    dates = pd.date_range("2016-01-01", "2026-01-01", freq="D")
    price_df = pd.DataFrame(
        {
            "date": dates.astype("datetime64[us]"),
            "收盘": [
                10.0 if day < pd.Timestamp("2021-01-01") else 30.0 for day in dates
            ],
        }
    )

    full = fetcher.compute_pe_percentile(
        "test", 20.0, fin_df, years=10, price_df=price_df
    )
    recent = fetcher.compute_pe_percentile(
        "test", 20.0, fin_df, years=5, price_df=price_df
    )

    assert full is not None and 45.0 <= full <= 55.0
    assert recent == 0.0


def test_fetch_percentiles_persists_five_and_ten_year_pe(monkeypatch):
    import pandas as pd

    price_df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2016-01-04", "2026-01-02"]).astype(
                "datetime64[us]"
            ),
            "收盘": [10.0, 20.0],
        }
    )
    calls = []
    monkeypatch.setattr(fetcher, "_load_price_df", lambda code, years: price_df)
    monkeypatch.setattr(
        fetcher,
        "compute_pe_percentile",
        lambda code, pe, fin_df, years, **kwargs: calls.append(years) or float(years),
    )
    monkeypatch.setattr(fetcher, "compute_pb_percentile", lambda *args, **kwargs: 20.0)
    results = {"pe_static": 18.0, "pe_ttm": 18.0, "pb": 2.0}
    null_reasons = {}

    fetcher._fetch_percentiles("600000", object(), results, null_reasons)

    assert calls == [5, 10]
    assert results["pe_percentile_5y"] == 5.0
    assert results["pe_percentile_10y"] == 10.0
    assert results["_pe_percentile_windows"]["5"]["sample_end"] == "2026-01-02"
    assert results["_pe_percentile_windows"]["10"]["sample_start"] == "2016-01-04"


def test_trading_dates_prefer_tushare(monkeypatch):
    expected = (date(2026, 8, 7),)
    monkeypatch.setattr(fetcher, "DATA_SOURCE", "tushare")
    monkeypatch.setattr(fetcher, "_fetch_trading_dates_tushare", lambda: expected)
    monkeypatch.setattr(
        fetcher,
        "_fetch_trading_dates_akshare",
        lambda: pytest.fail("TuShare calendar should be the primary path"),
    )
    fetcher._fetch_trading_dates.cache_clear()
    try:
        assert fetcher._fetch_trading_dates() == expected
    finally:
        fetcher._fetch_trading_dates.cache_clear()


def test_realtime_quote_uses_sina_only_and_records_snapshot(monkeypatch):
    today = date.today()
    quote = QuoteObservation(
        price=1600.0,
        quote_date=today.isoformat(),
        quote_time="10:00:00",
        source="sina",
        instrument_name="贵州茅台",
    )
    snapshot = {}
    monkeypatch.setattr(fetcher, "timed_call", lambda *args, **kwargs: (today,))
    monkeypatch.setattr(fetcher, "_fetch_sina_quote", lambda code: quote)
    monkeypatch.setattr(fetcher, "_validate_sina_quote", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        store,
        "record_quote_snapshot",
        lambda *args, **kwargs: snapshot.update(args=args, kwargs=kwargs),
    )

    result = fetcher._fetch_realtime_quote("600519")

    assert result is quote
    assert snapshot["args"][0:5] == (
        "600519",
        1600.0,
        today.isoformat(),
        "10:00:00",
        "sina",
    )
    assert snapshot["args"][5]["sina"]["price"] == 1600.0


def test_sina_quote_validation_rejects_stale_and_future_quotes():
    shanghai = timezone(timedelta(hours=8))
    now = datetime(2026, 8, 7, 10, 1, tzinfo=shanghai)
    trading_dates = (date(2026, 8, 7),)

    valid = QuoteObservation(1600.0, "2026-08-07", "10:00:00", "sina")
    stale = QuoteObservation(1600.0, "2026-08-07", "09:57:00", "sina")
    future = QuoteObservation(1600.0, "2026-08-07", "10:02:00", "sina")

    assert fetcher._validate_sina_quote(valid, trading_dates, now) is None
    assert "stale" in fetcher._validate_sina_quote(stale, trading_dates, now)
    assert "stale" in fetcher._validate_sina_quote(future, trading_dates, now)


@pytest.mark.parametrize(
    "debt_fields, expected_ratio",
    [
        ({}, None),
        ({"total_liab": 100.0}, None),
        (
            dict(
                total_liab=100,
                st_borr=10,
                lt_borr=5,
                bond_payable=2,
                non_cur_liab_due_1y=3,
            ),
            20.0,
        ),
        (
            dict(
                total_liab=100,
                st_borr=0,
                lt_borr=0,
                bond_payable=0,
                non_cur_liab_due_1y=0,
            ),
            0.0,
        ),
        (
            dict(
                total_liab=100,
                st_borr=None,
                lt_borr=None,
                bond_payable=None,
                non_cur_liab_due_1y=None,
            ),
            None,
        ),
        (
            dict(
                total_liab=100,
                st_borr=10,
                lt_borr=5,
                bond_payable=None,
                non_cur_liab_due_1y=3,
            ),
            None,
        ),
        (
            dict(total_liab=100, st_borr=10, lt_borr=5, non_cur_liab_due_1y=3),
            None,
        ),
        (
            dict(
                total_liab=0,
                st_borr=0,
                lt_borr=0,
                bond_payable=0,
                non_cur_liab_due_1y=0,
            ),
            None,
        ),
        (
            dict(
                total_liab=100,
                st_borr="invalid",
                lt_borr=5,
                bond_payable=2,
                non_cur_liab_due_1y=3,
            ),
            None,
        ),
    ],
)
def test_tushare_financial_adapter_merges_balance_and_cashflow(
    monkeypatch, debt_fields, expected_ratio
):
    import a_share_research.core.providers as providers

    def result(frame):
        return SimpleNamespace(status="ok", value=frame)

    class FakeProvider:
        def fetch_indicator_history(self, code):
            return result(
                pd.DataFrame(
                    {
                        "end_date": ["2025-12-31"],
                        "roe_waa": [12.0],
                        "netprofit_yoy": [8.0],
                        "debt_to_assets": [40.0],
                        "grossprofit_margin": [30.0],
                        "bps": [10.0],
                    }
                )
            )

        def fetch_income_history(self, code):
            return result(
                pd.DataFrame(
                    {
                        "end_date": ["2025-12-31"],
                        "basic_eps": [1.2],
                        "total_revenue": [1000.0],
                    }
                )
            )

        def fetch_balance_history(self, code):
            return result(
                pd.DataFrame(
                    {
                        "end_date": ["2025-12-31"],
                        "total_cur_assets": [200.0],
                        "total_cur_liab": [100.0],
                        "total_share": [100.0],
                        **{key: [value] for key, value in debt_fields.items()},
                    }
                )
            )

        def fetch_cashflow_history(self, code):
            return result(
                pd.DataFrame(
                    {
                        "end_date": ["2025-12-31"],
                        "n_cashflow_act": [300.0],
                    }
                )
            )

    monkeypatch.setattr(
        providers, "TushareFinancialProvider", lambda **kwargs: FakeProvider()
    )
    frame = fetcher._fetch_financials_tushare("600519")

    assert frame.loc[0, "流动比率"] == pytest.approx(2.0)
    assert frame.loc[0, "每股经营现金流"] == pytest.approx(3.0)
    extracted = fetcher._extract_fin_fields(frame)
    assert extracted["interest_bearing_to_total_debt"] == expected_ratio
    assert extracted["roe_3y_avg"] == 12.0
    assert extracted["eps"] == 1.2


# ── cmd_batch：KeyboardInterrupt 不被吞掉 ────────────────────────────────────


def test_cmd_batch_propagates_keyboard_interrupt(monkeypatch):
    """cmd_batch 只 except Exception，不捕获 KeyboardInterrupt"""

    def raise_kb(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(fetcher, "cmd_fetch", raise_kb)

    with pytest.raises(KeyboardInterrupt):
        fetcher.cmd_batch([])


def test_cmd_batch_catches_runtime_error(capsys, monkeypatch):
    """cmd_batch 捕获普通 Exception，继续处理下一支"""
    call_count = 0

    def failing_fetch(args):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("API 超时")

    monkeypatch.setattr(fetcher, "cmd_fetch", failing_fetch)

    fetcher.cmd_batch([])

    # 两支股票都被处理（即使都失败），不中断
    assert call_count == 2
    out = capsys.readouterr().out
    assert "❌" in out
    assert "API 超时" in out


def test_cmd_batch_empty_watchlist(capsys, monkeypatch):
    """watchlist 为空时 cmd_batch 提前返回，不崩溃"""
    monkeypatch.setattr(store, "list_codes", lambda: [])

    fetcher.cmd_batch([])
    out = capsys.readouterr().out
    assert "watchlist 为空" in out


def test_cmd_batch_deduplicates_codes(capsys, monkeypatch):
    """重复代码只取一次（保序去重）"""
    monkeypatch.setattr(store, "list_codes", lambda: ["600519", "600519", "000001"])

    fetched = []

    def track_fetch(args):
        fetched.append(args[0])

    monkeypatch.setattr(fetcher, "cmd_fetch", track_fetch)

    fetcher.cmd_batch([])
    assert fetched.count("600519") == 1, "重复代码 600519 应只抓取一次"


# ── 行业字段：优先使用 a_stock_lib 的 Tushare 批量行业映射 ───────────────────


def test_fetch_industry_from_lib_returns_value_when_hit(monkeypatch):
    """Tushare 行业映射命中时返回对应行业字符串"""

    class FakeProvider:
        def fetch_industry_map(self):
            return SimpleNamespace(status="ok", value={"600519": "白酒"})

    _patch_tushare_provider(monkeypatch, lambda: FakeProvider())

    assert fetcher._fetch_industry_from_lib("600519") == "白酒"


def test_fetch_industry_from_lib_returns_none_when_provider_fails(monkeypatch):
    """Tushare provider 返回 failed 时静默返回 None"""

    class FakeProvider:
        def fetch_industry_map(self):
            return SimpleNamespace(status="failed", value=None)

    _patch_tushare_provider(monkeypatch, lambda: FakeProvider())

    assert fetcher._fetch_industry_from_lib("600519") is None


def test_fetch_industry_from_lib_returns_none_on_exception(monkeypatch):
    """Tushare provider 抛异常时不影响主流程"""

    class FakeProvider:
        def fetch_industry_map(self):
            raise RuntimeError("tushare unavailable")

    _patch_tushare_provider(monkeypatch, lambda: FakeProvider())

    assert fetcher._fetch_industry_from_lib("600519") is None


def test_fetch_spot_data_prefers_lib_industry_over_akshare(monkeypatch):
    """_fetch_spot_data 优先使用 a_stock_lib 行业，而不是 AKShare 原始行业字段"""
    monkeypatch.setattr(
        fetcher,
        "timed_call",
        lambda *args, **kwargs: {
            "股票简称": "贵州茅台",
            "行业": "AKShare行业",
            "最新": "1600.00",
            "总市值": "200",
            "流通市值": "100",
        },
    )
    monkeypatch.setattr(fetcher, "_fetch_industry_from_lib", lambda code: "Tushare行业")
    monkeypatch.setattr(
        fetcher,
        "_fetch_realtime_quote",
        lambda code: QuoteObservation(
            price=1600.0,
            quote_date="2026-07-14",
            quote_time="10:00:00",
            source="sina",
        ),
    )

    results = {}
    null_reasons = {}
    name, industry, current_price = fetcher._fetch_spot_data(
        "600519", results, null_reasons
    )

    assert name == "贵州茅台"
    assert industry == "Tushare行业"
    assert current_price == 1600.0
    assert results["float_to_total_ratio"] == 50.0


def test_fetch_spot_data_preserves_sina_name_when_akshare_info_fails(monkeypatch):
    """A successful Sina quote still supplies the company name on info failure."""
    monkeypatch.setattr(
        fetcher, "timed_call", lambda *args, **kwargs: (None, "API失败")
    )
    monkeypatch.setattr(fetcher, "_fetch_industry_from_lib", lambda code: None)
    monkeypatch.setattr(fetcher, "_lookup_cached_name", lambda code: None)
    monkeypatch.setattr(fetcher, "_lookup_cached_industry", lambda code: None)
    monkeypatch.setattr(
        fetcher,
        "_fetch_realtime_quote",
        lambda code: QuoteObservation(
            price=1600.0,
            quote_date="2026-07-14",
            quote_time="10:00:00",
            source="sina",
            instrument_name="贵州茅台",
        ),
    )

    name, industry, current_price = fetcher._fetch_spot_data("600519", {}, {})

    assert name == "贵州茅台"
    assert industry == "未知"
    assert current_price == 1600.0


def test_fetch_spot_data_marks_cached_industry_stale(monkeypatch):
    monkeypatch.setattr(
        fetcher, "timed_call", lambda *args, **kwargs: (None, "API失败")
    )
    monkeypatch.setattr(fetcher, "_fetch_industry_from_lib", lambda code: None)
    monkeypatch.setattr(fetcher, "_lookup_cached_name", lambda code: "缓存名称")
    monkeypatch.setattr(fetcher, "_lookup_cached_industry", lambda code: "缓存行业")
    monkeypatch.setattr(store, "update_qualitative_only_security", lambda *args: None)
    monkeypatch.setattr(
        fetcher,
        "_fetch_realtime_quote",
        lambda code: QuoteObservation(
            price=10.0,
            quote_date="2026-07-14",
            quote_time="10:00:00",
            source="sina",
        ),
    )
    results = {}

    _, industry, _ = fetcher._fetch_spot_data("600000", results, {})

    assert industry == "缓存行业"
    assert results["industry_status"] == "stale_cache"
    assert results["_industry_source"] == "historical_cache"


def test_fetch_spot_data_persists_qualitative_only_routing_before_fundamentals(
    monkeypatch,
):
    monkeypatch.setattr(
        fetcher,
        "timed_call",
        lambda *args, **kwargs: {
            "股票简称": "保险公司",
            "行业": "保险",
        },
    )
    monkeypatch.setattr(fetcher, "_fetch_industry_from_lib", lambda code: "保险")
    monkeypatch.setattr(
        fetcher,
        "_fetch_realtime_quote",
        lambda code: QuoteObservation(
            price=50.0,
            quote_date="2026-07-14",
            quote_time="10:00:00",
            source="sina",
        ),
    )

    fetcher._fetch_spot_data("601318", {}, {})

    with db.db_session() as conn:
        status = conn.execute(
            "SELECT name, industry FROM qualitative_only_securities WHERE code=?",
            ("601318",),
        ).fetchone()
    assert status == ("保险公司", "保险")


# ── _extract_fin_fields：财务字段提取纯函数 ──────────────────────────────────


def _make_fin_df(rows: int = 5) -> "pd.DataFrame":
    """构造 stock_financial_abstract_ths 格式的 mock DataFrame"""
    import pandas as pd

    data = {
        "净资产收益率": [f"{10 + i:.2f}" for i in range(rows)],
        "净利润同比增长率": [f"{8 + i:.2f}" for i in range(rows)],
        "资产负债率": [f"{45.00:.2f}" for _ in range(rows)],
        "基本每股收益": [f"{2.5 + i * 0.1:.2f}" for i in range(rows)],
        "每股净资产": [f"{15.0 + i:.2f}" for i in range(rows)],
        "销售毛利率": [f"{85.00:.2f}" for _ in range(rows)],
        "营业总收入同比增长率": [f"{12 + i:.2f}" for i in range(rows)],
        "流动比率": [f"{2.5:.2f}" for _ in range(rows)],
        "每股经营现金流": [f"{3.2 + i * 0.1:.2f}" for i in range(rows)],
        "有息负债率": [f"{20.0:.2f}" for _ in range(rows)],
    }
    return pd.DataFrame(data)


def test_extract_fin_fields_returns_all_keys():
    """_extract_fin_fields 返回 10 个预期字段（含 bps 和 interest_bearing_to_total_debt）"""
    fin_df = _make_fin_df()
    result = fetcher._extract_fin_fields(fin_df)

    expected_keys = {
        "roe_3y_avg",
        "net_profit_growth",
        "debt_ratio",
        "eps",
        "bps",
        "gross_margin",
        "revenue_growth_3y",
        "current_ratio",
        "operating_cf_per_share",
        "interest_bearing_to_total_debt",
    }
    assert set(result.keys()) == expected_keys
    # 所有值在正常 DataFrame 下均不应为 None
    for k, v in result.items():
        assert v is not None, f"字段 {k} 不应为 None"


def test_extract_fin_fields_interest_bearing_to_total_debt():
    fin_df = _make_fin_df()
    result = fetcher._extract_fin_fields(fin_df)
    assert result["interest_bearing_to_total_debt"] == 20.0


def test_extract_fin_fields_gross_margin():
    """gross_margin 从 '销售毛利率' 列正确提取最新年度值"""
    fin_df = _make_fin_df()
    result = fetcher._extract_fin_fields(fin_df)
    # 最后一行 '销售毛利率' 值固定为 '85.00'
    assert result["gross_margin"] == 85.0


def test_extract_fin_fields_revenue_growth_3y():
    """revenue_growth_3y 是 '营业总收入同比增长率' 最后 3 年的均值"""
    fin_df = _make_fin_df(rows=5)
    result = fetcher._extract_fin_fields(fin_df)
    # 最后3行值为 '14.00', '15.00', '16.00'（i=2,3,4 → 12+i）
    expected = round((14.0 + 15.0 + 16.0) / 3, 2)
    assert result["revenue_growth_3y"] == expected


# ── _fetch_bond_yield fallback tests ──────────────────────────────────────────


def test_fetch_bond_yield_fallback(monkeypatch, isolated_db):
    """API failure may reuse a source-complete market snapshot under 24h."""
    store.set_market_indicator_snapshot(
        "bond_yield_10y",
        2.15,
        "2026-07-14",
        "fixture-source",
        fetched_at=(
            domain.utc_now()
            - fetcher.BOND_YIELD_REFRESH_INTERVAL
            - timedelta(seconds=1)
        ).isoformat(),
    )

    # 2. 模拟国债收益率 API 失败（返回 None）
    monkeypatch.setattr(fetcher, "_fetch_bond_yield_api", lambda *args, **kwargs: None)

    null_reasons = {}
    snapshot = fetcher._fetch_bond_yield(null_reasons)

    assert snapshot is not None
    assert snapshot["value"] == 2.15
    assert "bond_yield_10y" not in null_reasons


def test_fetch_bond_yield_without_recent_snapshot_is_missing(monkeypatch, isolated_db):
    """No hardcoded baseline is allowed when the API and snapshot both fail."""
    import sqlite3

    # 初始化 DB 以确保表结构存在
    db.get_db().close()
    # 保证 DB 里没有任何股票的缓存
    with closing(sqlite3.connect(isolated_db)) as conn:
        conn.execute("DELETE FROM stock_fundamentals")
        conn.commit()

    monkeypatch.setattr(fetcher, "_fetch_bond_yield_api", lambda *args, **kwargs: None)

    null_reasons = {}
    snapshot = fetcher._fetch_bond_yield(null_reasons)

    assert snapshot is None
    assert null_reasons["bond_yield_10y"] == "国债收益率API失败，且无24小时内可信快照"


def test_fetch_bond_yield_reuses_one_hour_snapshot_without_api(monkeypatch):
    store.set_market_indicator_snapshot(
        "bond_yield_10y", 2.15, "2026-07-14", "fixture-source"
    )
    monkeypatch.setattr(
        fetcher,
        "_fetch_bond_yield_api",
        lambda *args, **kwargs: pytest.fail(
            "fresh global snapshot must avoid API call"
        ),
    )

    snapshot = fetcher._fetch_bond_yield({})

    assert snapshot is not None
    assert snapshot["value"] == 2.15


# ── _detect_split_ratio tests ──────────────────────────────────────────────────


def _make_div_df(rows: list[dict]) -> "pd.DataFrame":
    """构造 stock_fhps_detail_em 格式的 mock DataFrame"""
    import pandas as pd

    return pd.DataFrame(rows)


def test_detect_split_ratio_no_data():
    """div_df 为空时返回 (0.0, None)"""
    import pandas as pd

    result = fetcher._detect_split_ratio(pd.DataFrame(), 2025)
    assert result == (0.0, None)


def test_detect_split_ratio_normal_split():
    """正常送转事件：10转2 → ratio=0.2, ex_date 正确"""
    div_df = _make_div_df(
        [
            {
                "送转股份-送转总比例": 2.0,
                "除权除息日": "2026-05-26",
                "方案进度": "实施分配",
            }
        ]
    )
    ratio, ex_date = fetcher._detect_split_ratio(div_df, 2025)
    assert abs(ratio - 0.2) < 1e-5
    assert ex_date == "2026-05-26"


def test_detect_split_ratio_future_ex_date_ignored():
    """除权日在未来时不触发（避免提前调整 EPS）"""
    import datetime

    future = (datetime.date.today() + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    div_df = _make_div_df(
        [
            {
                "送转股份-送转总比例": 2.0,
                "除权除息日": future,
                "方案进度": "实施分配",
            }
        ]
    )
    ratio, ex_date = fetcher._detect_split_ratio(div_df, 2025)
    assert ratio == 0.0
    assert ex_date is None


def test_detect_split_ratio_before_report_year_ignored():
    """早于最新年报期末的送转不触发（已反映在年报 EPS 中）"""
    div_df = _make_div_df(
        [
            {
                "送转股份-送转总比例": 2.0,
                "除权除息日": "2024-06-01",  # before 2025-12-31
                "方案进度": "实施分配",
            }
        ]
    )
    ratio, ex_date = fetcher._detect_split_ratio(div_df, 2025)
    assert ratio == 0.0


def test_detect_split_ratio_non_implemented_ignored():
    """方案进度非"实施分配"时不触发"""
    div_df = _make_div_df(
        [
            {
                "送转股份-送转总比例": 2.0,
                "除权除息日": "2026-05-26",
                "方案进度": "预案",
            }
        ]
    )
    ratio, ex_date = fetcher._detect_split_ratio(div_df, 2025)
    assert ratio == 0.0


def test_detect_split_ratio_cumulative():
    """两次送转累乘：10转2 (0.2) 后再 10转1 (0.1) → factor=1.2×1.1=1.32, ratio≈0.32"""
    div_df = _make_div_df(
        [
            {
                "送转股份-送转总比例": 2.0,
                "除权除息日": "2026-03-01",
                "方案进度": "实施分配",
            },
            {
                "送转股份-送转总比例": 1.0,
                "除权除息日": "2026-05-01",
                "方案进度": "实施分配",
            },
        ]
    )
    ratio, ex_date = fetcher._detect_split_ratio(div_df, 2025)
    assert abs(ratio - 0.32) < 1e-5
    assert ex_date == "2026-05-01"


def test_fetch_dividend_api_failure_returns_zero_float(monkeypatch):
    """_fetch_dividend API 超时时返回 (0.0, None)，不崩溃"""
    monkeypatch.setattr(fetcher, "timed_call", lambda *a, **kw: "超时")
    results = {}
    null_reasons = {}
    ret = fetcher._fetch_dividend("603606", 42.0, results, null_reasons, 2025)
    assert ret == (0.0, None)
    assert "dividend_yield" in null_reasons


# ── compute_pe_percentile split adjustment tests ────────────────────────────────


def _make_price_df(start: str, end: str, base_price: float) -> "pd.DataFrame":
    """构造日期序列价格 DataFrame（用于 PE/PB 分位测试）"""
    import pandas as pd

    dates = pd.date_range(start, end, freq="B")  # business days
    return pd.DataFrame(
        {
            "date": dates.astype("datetime64[us]"),
            "收盘": [base_price] * len(dates),
        }
    )


def _make_fin_df_with_period(year: int, eps: float, bps: float) -> "pd.DataFrame":
    """构造含报告期的财务 DataFrame"""
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "报告期": str(year),
                "基本每股收益": str(eps),
                "每股净资产": str(bps),
                "净资产收益率": "15.00",
                "净利润同比增长率": "10.00",
                "资产负债率": "40.00",
                "销售毛利率": "30.00",
                "营业总收入同比增长率": "10.00",
                "流动比率": "2.00",
                "每股经营现金流": "3.00",
            }
        ]
    )


def _make_ps_valuation_df(
    start_year: int = 2021, months: int = 60, start_ps: float = 5.0
) -> "pd.DataFrame":
    """构造 fetcher 内部 date/收盘/ps_ttm 同源月度样本。"""
    import pandas as pd

    dates = pd.date_range(f"{start_year}-01-31", periods=months, freq="ME")
    return pd.DataFrame(
        {
            "date": dates.astype("datetime64[us]"),
            "收盘": [40.0] * months,
            "ps_ttm": [start_ps + i * 0.1 for i in range(months)],
        }
    )


def test_compute_price_change_5d_uses_six_valid_sorted_observations():
    frame = pd.DataFrame(
        {
            "date": [
                "2026-01-08",
                "2026-01-05",
                "2026-01-07",
                "invalid",
                "2026-01-02",
                "2026-01-06",
                "2026-01-01",
                "2026-01-05",
                "2026-01-07",
            ],
            "收盘": [110.0, 102.0, 104.0, 999.0, 101.0, 103.0, 100.0, 0.0, -5.0],
        }
    )

    change, as_of, reason = fetcher.compute_price_change_5d(frame)

    assert change == pytest.approx(10.0)
    assert as_of == "2026-01-08"
    assert reason is None


def test_compute_price_change_5d_requires_six_valid_observations():
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=5, freq="B"),
            "收盘": [100.0, 101.0, 102.0, 103.0, 104.0],
        }
    )

    change, as_of, reason = fetcher.compute_price_change_5d(frame)

    assert change is None
    assert as_of is None
    assert reason is not None and "6" in reason


def test_load_price_df_drops_invalid_provider_dates(monkeypatch):
    raw = pd.DataFrame(
        {
            "日期": ["2026-01-02", "malformed", "2026-01-05"],
            "收盘": [100.0, 999.0, 101.0],
        }
    )
    monkeypatch.setattr(fetcher, "timed_call_with_retry", lambda *args, **kwargs: raw)

    result = fetcher._load_price_df("600000", years=10)

    assert result["date"].dt.date.astype(str).tolist() == ["2026-01-02", "2026-01-05"]


def test_compute_ps_ttm_percentile_requires_60_valid_months():
    valuation_df = _make_ps_valuation_df(months=59)

    ps_ttm, percentile, window = fetcher.compute_ps_ttm_percentile_5y(valuation_df)

    assert ps_ttm is None
    assert percentile is None
    assert window["valid_months"] == 59


def test_compute_ps_ttm_percentile_uses_month_end_strict_less():
    valuation_df = _make_ps_valuation_df()
    # 同月早期的极端值不得进入月末样本。
    duplicate = valuation_df.iloc[[-1]].copy()
    duplicate["date"] = duplicate["date"] - pd.Timedelta(days=10)
    duplicate["ps_ttm"] = 999.0
    valuation_df = pd.concat([valuation_df, duplicate], ignore_index=True)

    ps_ttm, percentile, window = fetcher.compute_ps_ttm_percentile_5y(valuation_df)

    assert ps_ttm == pytest.approx(10.9)
    assert percentile == pytest.approx(98.3)
    assert window == {
        "sample_start": "2021-01-31",
        "sample_end": "2025-12-31",
        "valid_months": 60,
        "basis": "ps_ttm",
        "source": "tushare.daily_basic",
    }


def test_fetch_percentiles_reuses_price_history_for_ps(monkeypatch):
    price_df = _make_ps_valuation_df()
    fin_df = _make_fin_df_with_period(2024, eps=2.0, bps=15.0)
    calls = []
    monkeypatch.setattr(
        fetcher,
        "_load_price_df",
        lambda code, years: calls.append((code, years)) or price_df,
    )
    monkeypatch.setattr(fetcher, "DATA_SOURCE", "tushare")
    monkeypatch.setattr(fetcher, "compute_pe_percentile", lambda *args, **kwargs: 20.0)
    monkeypatch.setattr(fetcher, "compute_pb_percentile", lambda *args, **kwargs: 30.0)

    results = {"pe_static": 20.0, "pb": 2.0}
    null_reasons = {}
    fetcher._fetch_percentiles("600000", fin_df, results, null_reasons)

    assert calls == [("600000", 10)]
    assert results["price_change_5d"] == pytest.approx(0.0)
    assert results["_price_change_5d_as_of"] == "2025-12-31"
    assert results["ps_ttm"] == pytest.approx(10.9)
    assert results["ps_percentile_5y"] == pytest.approx(98.3)
    assert results["_ps_percentile_window"]["valid_months"] == 60
    assert "ps_ttm" not in null_reasons
    assert "ps_percentile_5y" not in null_reasons


def test_fetch_percentiles_can_return_ps_when_financials_are_missing(monkeypatch):
    monkeypatch.setattr(fetcher, "DATA_SOURCE", "tushare")
    monkeypatch.setattr(
        fetcher, "_load_price_df", lambda code, years: _make_ps_valuation_df()
    )
    results = {}
    null_reasons = {}

    fetcher._fetch_percentiles("600000", None, results, null_reasons)

    assert results["ps_ttm"] == pytest.approx(10.9)
    assert results["ps_percentile_5y"] == pytest.approx(98.3)
    assert null_reasons["pe_percentile_5y"]


def test_fetch_percentiles_skips_ps_in_akshare_mode(monkeypatch):
    monkeypatch.setattr(fetcher, "DATA_SOURCE", "akshare")
    fin_df = _make_fin_df_with_period(2024, eps=2.0, bps=15.0)
    price_df = _make_price_df("2018-01-01", "2028-01-01", base_price=40.0)
    monkeypatch.setattr(fetcher, "_load_price_df", lambda code, years: price_df)

    results = {"pe_static": 20.0, "pb": 2.0}
    null_reasons = {}
    fetcher._fetch_percentiles("600000", fin_df, results, null_reasons)

    assert results.get("ps_ttm") is None
    assert results.get("ps_percentile_5y") is None
    assert null_reasons["ps_ttm"] == "AKShare手动降级路径不提供同口径PS_TTM"
    assert null_reasons["ps_percentile_5y"] == "AKShare手动降级路径不提供同口径PS_TTM"


def test_compute_pe_percentile_no_split():
    """无送转时 PE 分位正常计算（基线测试）"""
    fin_df = _make_fin_df_with_period(2024, eps=2.0, bps=15.0)
    # 价格全部用 40元，EPS=2.0 → PE=20x
    price_df = _make_price_df("2025-05-01", "2026-06-01", base_price=40.0)
    pct = fetcher.compute_pe_percentile("test", 20.0, fin_df, price_df=price_df)
    # 所有历史PE均=20x，current_pe=20x → 分位约50%（恰好在中位）
    assert pct is not None
    assert isinstance(pct, float)


def test_compute_pe_percentile_with_split_adjusts_post_split_eps():
    """送转后：除权日后的历史 EPS 被除以 (1+ratio)，PE 序列不再凹陷"""
    import pandas as pd

    fin_df = _make_fin_df_with_period(2024, eps=2.4, bps=15.0)
    # 前一半价格 = 60 (pre-split), 后一半价格 = 50 (post-split, 10转2)
    pre_dates = pd.date_range("2025-05-01", "2025-12-31", freq="B")
    post_dates = pd.date_range("2026-01-02", "2026-06-01", freq="B")
    price_df = pd.DataFrame(
        {
            "date": pd.concat(
                [
                    pd.Series(pre_dates.astype("datetime64[us]")),
                    pd.Series(post_dates.astype("datetime64[us]")),
                ]
            ).reset_index(drop=True),
            "收盘": [60.0] * len(pre_dates) + [50.0] * len(post_dates),
        }
    )

    # Without split adjustment: post-split PE = 50/2.4 = 20.8x, pre-split PE = 60/2.4 = 25x
    # With split adjustment (ratio=0.2): post-split adjusted eps = 2.4/1.2 = 2.0
    #   → post-split PE in series = 50/2.0 = 25x (same as pre-split) — no dip
    pct_no_adj = fetcher.compute_pe_percentile("test", 25.0, fin_df, price_df=price_df)
    pct_adj = fetcher.compute_pe_percentile(
        "test",
        25.0,
        fin_df,
        price_df=price_df,
        split_ratio=0.2,
        split_ex_date="2026-01-02",
    )

    # With adjustment, adjusted EPS = 2.4/1.2 = 2.0 for post-split dates
    # → all historical PEs = 25x = current_pe; strict less-than → percentile = 0.0%
    # Without adjustment, post-split PEs ~20.8x drag series lower → pct_no_adj > 0
    assert pct_adj == 0.0
    assert pct_no_adj is not None and pct_no_adj > 0


def test_compute_pb_percentile_with_split_adjusts_post_split_bps():
    """同理验证 PB 分位：除权后 BPS 也被调整"""
    import pandas as pd

    fin_df = _make_fin_df_with_period(2024, eps=2.0, bps=12.0)
    pre_dates = pd.date_range("2025-05-01", "2025-12-31", freq="B")
    post_dates = pd.date_range("2026-01-02", "2026-06-01", freq="B")
    price_df = pd.DataFrame(
        {
            "date": pd.concat(
                [
                    pd.Series(pre_dates.astype("datetime64[us]")),
                    pd.Series(post_dates.astype("datetime64[us]")),
                ]
            ).reset_index(drop=True),
            "收盘": [24.0] * len(pre_dates) + [20.0] * len(post_dates),
        }
    )

    pct_no_adj = fetcher.compute_pb_percentile("test", 2.0, fin_df, price_df=price_df)
    pct_adj = fetcher.compute_pb_percentile(
        "test",
        2.0,
        fin_df,
        price_df=price_df,
        split_ratio=0.2,
        split_ex_date="2026-01-02",
    )
    # BPS 调整后所有历史 PB = 2.0 = current_pb，严格小于比较 → pct_adj = 0.0%
    assert pct_adj == 0.0
    assert pct_no_adj is not None and pct_no_adj > 0


def test_bank_cash_flow_gate_is_not_applicable_during_fetch() -> None:
    results = {
        "_risk_gate_inputs": {
            "cash_flow_gate": {
                "ttm_cfo": -1,
                "ttm_capex": 0,
                "sources": ["tushare.cashflow (company-filed statement mirror)"],
                "as_of": "2026-09-09",
            }
        }
    }

    fetcher._cache_gate_defaults("600036", results, "2025年报", "银行")

    assert results["cash_flow_gate"]["status"] == "not_applicable"
    assert results["cash_flow_gate"]["action_eligible"] is True

    missing_source: dict = {}
    fetcher._cache_gate_defaults("600036", missing_source, "2025年报", "银行")
    assert missing_source["cash_flow_gate"]["status"] == "incomplete"
    assert (
        missing_source["cash_flow_gate"]["reason_code"]
        == "financial_framework_source_missing"
    )


@pytest.fixture
def quarter_frames():
    periods = ["2025-06-30", "2025-09-30", "2025-12-31", "2026-06-30", "2026-09-30"]
    return {
        "income": pd.DataFrame(
            {
                "end_date": periods,
                "revenue": [60, 100, 140, 90, 150],
                "n_income_attr_p": [6, 10, 14, 9, 15],
            }
        ),
        "indicator": pd.DataFrame(
            {
                "ts_code": ["600000.SH"] * 5,
                "ann_date": periods,
                "end_date": periods,
                "update_flag": ["1"] * 5,
                "endpoint": ["fina_indicator"] * 5,
                **{
                    key: [None] * 5
                    for key in (
                        "roe_waa",
                        "netprofit_yoy",
                        "debt_to_assets",
                        "grossprofit_margin",
                        "bps",
                        "or_yoy",
                        "dt_netprofit_yoy",
                        "f_ann_date",
                        "report_type",
                        "comp_type",
                        "end_type",
                    )
                },
            }
        ),
        "deducted": pd.DataFrame(
            {
                "end_date": [period.replace("-", "") for period in periods]
                + ["20260930"],
                "ann_date": ["20261030"] * 6,
                "update_flag": ["1"] * 5 + ["0"],
                "profit_dedt": [5, 8, 12, 7, 12, 999],
            }
        ),
        "cashflow": pd.DataFrame(
            {"end_date": periods, "n_cashflow_act": [80, 120, 200, 100, 180]}
        ),
        "balance": pd.DataFrame(
            {
                "end_date": periods,
                **{
                    key: [80, 100, 200, 120, 150]
                    for key in (
                        "accounts_receiv",
                        "inventories",
                        "notes_payable",
                        "contract_liab",
                    )
                },
            }
        ),
    }


def test_quarter_uses_single_quarter_ytd_and_same_period_balance(quarter_frames):
    result = fetcher.build_quarter_check("600000", quarter_frames)
    fields = result["fields"]
    assert result["report_period"] == "2026-09-30"
    assert fields["revenue_single_quarter"]["value"] == 60
    assert fields["revenue_single_quarter_yoy"]["value"] == 50
    assert fields["net_profit_attributable_ytd_yoy"]["value"] == 50
    assert fields["deducted_net_profit_single_quarter_yoy"]["value"] == pytest.approx(
        200 / 3
    )
    assert fields["operating_cash_flow_ttm"]["value"] == 260
    assert fields["net_profit_attributable_ttm"]["value"] == 19
    assert fields["operating_cash_flow_to_net_profit_ttm"]["value"] == pytest.approx(
        260 / 19
    )
    assert fields["operating_cash_flow_ttm"]["periods"] == [
        "2025-12-31",
        "2026-09-30",
        "2025-09-30",
    ]
    for key in (
        "accounts_receivable",
        "inventory",
        "notes_payable",
        "contract_liabilities",
    ):
        assert fields[key + "_yoy"]["value"] == 50
        assert fields[key + "_yoy"]["basis"] == "yoy_same_period_end"
        assert fields[key + "_yoy"]["periods"] == ["2026-09-30", "2025-09-30"]
        assert fields[key + "_vs_year_start"]["value"] == -25
        assert fields[key + "_vs_year_start"]["basis"] == "vs_year_start"
        assert "季节性" in fields[key + "_vs_year_start"]["label"]


@pytest.mark.parametrize("profit", [0, -1])
def test_quarter_nonpositive_ttm_profit_has_no_ratio(quarter_frames, profit):
    quarter_frames["income"].loc[4, "n_income_attr_p"] = profit - 4
    metric = fetcher.build_quarter_check("600000", quarter_frames)["fields"][
        "operating_cash_flow_to_net_profit_ttm"
    ]
    assert metric["value"] is None
    assert "非正" in metric["reason"]


def test_quarter_missing_inputs_never_annualise_or_use_old_annual(quarter_frames):
    quarter_frames["cashflow"].loc[2, "end_date"] = "2024-12-31"
    quarter_frames["deducted"] = None
    quarter_frames["balance"]["accounts_receiv"] = quarter_frames["balance"][
        "accounts_receiv"
    ].astype(float)
    quarter_frames["balance"].loc[1, "accounts_receiv"] = float("inf")
    fields = fetcher.build_quarter_check("600000", quarter_frames)["fields"]
    for name in (
        "operating_cash_flow_ttm",
        "deducted_net_profit_ytd_yoy",
        "accounts_receivable_yoy",
    ):
        assert fields[name]["value"] is None
        assert fields[name]["reason"]
    assert fields["accounts_receivable_vs_year_start"]["value"] == -25


@pytest.mark.parametrize("period", ["2026-03-31", "2025-12-31"])
def test_quarter_q1_and_annual_periods(period):
    year = int(period[:4])
    previous = f"{year - 1}{period[4:]}"
    income = pd.DataFrame(
        {
            "end_date": [previous, period],
            "revenue": [100, 120],
            "n_income_attr_p": [10, 12],
        }
    )
    fields = fetcher.build_quarter_check("600000", {"income": income})["fields"]
    assert fields["revenue_ytd_yoy"]["value"] == pytest.approx(20)
    if period.endswith("03-31"):
        assert fields["revenue_single_quarter"]["value"] == 120
        assert fields["revenue_single_quarter_yoy"]["value"] == pytest.approx(20)
        assert fields["net_profit_attributable_ttm"]["value"] is None
    else:
        assert fields["net_profit_attributable_ttm"]["value"] == 12
        assert fields["net_profit_attributable_ttm"]["periods"] == [period]
        assert fields["revenue_single_quarter"]["value"] is None


@pytest.fixture
def multi_year_dividends():
    return pd.DataFrame(
        {
            "报告期": ["2024-12-31", "2025-06-30", "2025-12-31", "2026-06-30"],
            "现金分红-现金分红比例": [18.0, 3.0, 5.6, 2.0],
            "送转股份-送转总比例": [2.0, 0.0, 0.0, 0.0],
            "除权除息日": ["2025-05-26", "2025-09-26", None, "2026-05-26"],
            "方案进度": ["实施分配", "实施分配", "董事会预案", "实施分配"],
        }
    )


@pytest.mark.parametrize("financial_year", [2025, 2026])
@pytest.mark.parametrize("quote_kind", ["valid", "no_as_of", "invalid_price"])
@pytest.mark.parametrize(
    "missing,deducted_failure,split_ratio,dps_override",
    [
        (False, False, 0, None),
        (False, False, 0.2, None),
        (False, True, 0, None),
        (True, True, 0, None),
        (False, False, 0, 0),
        (False, False, 0, -1),
    ],
)
def test_quarter_cli_json_thresholds_and_missing_sources(
    monkeypatch,
    capsys,
    quarter_frames,
    missing,
    deducted_failure,
    split_ratio,
    isolated_db,
    quote_kind,
    dps_override,
    multi_year_dividends,
    financial_year,
):
    import json
    import tushare
    from pathlib import Path
    import a_share_research.core.providers as providers

    assert len(quarter_frames["indicator"].columns) == 16
    assert "profit_dedt" not in quarter_frames["indicator"]

    class Provider:
        def __init__(self):
            print("provider creation diagnostic")

        def fetch_indicator_history(self, code):
            pytest.fail("quarter must not fetch unused indicators")

    for key, method in (
        ("income", "fetch_income_history"),
        ("balance", "fetch_balance_history"),
        ("cashflow", "fetch_cashflow_history"),
    ):
        if missing and key == "cashflow":
            continue
        setattr(
            Provider,
            method,
            lambda self, code, key=key: SimpleNamespace(
                status="failed" if missing else "ok", value=quarter_frames[key]
            ),
        )
    monkeypatch.setattr(
        providers, "TushareFinancialProvider", lambda **kwargs: Provider()
    )

    def direct_indicator(**kwargs):
        print("direct indicator diagnostic")
        assert kwargs["ts_code"] == "600000.SH"
        assert set(kwargs["fields"].split(",")) == {
            "ts_code",
            "end_date",
            "ann_date",
            "update_flag",
            "profit_dedt",
        }
        if deducted_failure:
            raise RuntimeError("deducted source failed")
        return quarter_frames["deducted"]

    monkeypatch.setattr(
        tushare,
        "pro_api",
        lambda token: SimpleNamespace(fina_indicator=direct_indicator),
    )

    def timed_call(method, *args, timeout=None, **kwargs):
        try:
            return method(*args, **kwargs)
        except RuntimeError as exc:
            return ("ERROR", str(exc))

    monkeypatch.setattr(fetcher, "timed_call", timed_call)
    monkeypatch.setattr(
        fetcher,
        "_fetch_realtime_quote",
        lambda code: SimpleNamespace(
            price=-1 if quote_kind == "invalid_price" else 20,
            as_of=None if quote_kind == "no_as_of" else "2026-10-30T15:00:00+08:00",
            source="sina",
        ),
    )
    financials = pd.DataFrame({"报告期": [f"{financial_year}-12-31"]})
    monkeypatch.setattr(fetcher, "_fetch_financials", lambda code: financials)
    multi_year_dividends.loc[3, "送转股份-送转总比例"] = split_ratio * 10
    if dps_override == 0:
        # Positive disclosed amounts can round to a zero annual DPS.
        multi_year_dividends["现金分红-现金分红比例"] = 0.00001
    monkeypatch.setattr(
        fetcher,
        "_fetch_dividends",
        lambda code: None if missing else multi_year_dividends,
    )
    if dps_override == -1:
        monkeypatch.setattr(
            fetcher,
            "_compute_dividend_basis",
            lambda *args: (0, dps_override, None, 2025, ["2025-06-30", "2025-12-31"]),
        )
    monkeypatch.setattr(
        fetcher,
        "_fetch_bond_yield",
        lambda reasons: None
        if missing
        else {"value": 2, "as_of": "2026-10-30", "source": "fixture"},
    )
    fetch_results = {}
    fetch_basis = {}
    compute_dividends = fetcher._compute_dividends

    def capture_basis(*args):
        dividend = compute_dividends(*args)
        fetch_basis.update(dividend)
        return dividend

    monkeypatch.setattr(fetcher, "_compute_dividends", capture_basis)
    monkeypatch.setattr(
        fetcher,
        "_fetch_spot_data",
        lambda *args: (
            "fixture",
            "fixture",
            None if quote_kind == "invalid_price" else 20,
        ),
    )

    def financial_data(code, results, reasons):
        results.update(eps=1.2, bps=6, operating_cf_per_share=2.4)
        return financials, 1.2, 6

    monkeypatch.setattr(fetcher, "_fetch_fin_data", financial_data)
    for name in ("_compute_ttm_valuation", "_fetch_pb_pe_data", "_fetch_percentiles"):
        monkeypatch.setattr(fetcher, name, lambda *args: None)
    monkeypatch.setattr(
        fetcher,
        "_build_cache_payload",
        lambda code, name, industry, results, reasons, period: fetch_results.update(
            results
        ),
    )
    assert fetcher.main(["fetch", "600000"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(fetcher, "_compute_dividends", compute_dividends)
    assert fetcher.main(["quarter", "600000", "--json"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert "provider creation diagnostic" in captured.err
    assert "direct indicator diagnostic" in captured.err
    fields = result["fields"]
    for key in ("year", "periods", "split_ratio", "split_ex_date"):
        assert fields["dps"][key] == fetch_basis[key]
    if dps_override is None:
        assert fields["dps"]["value"] == fetch_results.get("dps")
    elif dps_override == 0 and quote_kind != "invalid_price":
        assert fetch_results["dps"] == 0
    expected_split = split_ratio if financial_year == 2025 and not missing else 0
    assert fetch_results["eps"] == round(1.2 / (1 + expected_split), 4)
    assert fetch_results["bps"] == round(6 / (1 + expected_split), 4)
    assert fetch_results["operating_cf_per_share"] == round(
        2.4 / (1 + expected_split), 4
    )
    if dps_override is None and not missing and quote_kind != "invalid_price":
        assert fetch_results["dividend_yield"] == round(4.3 / (1 + expected_split), 2)
        assert fetch_results["dps_ttm"] == round(0.76 / (1 + expected_split), 4)
    assert not Path(isolated_db).exists()
    deducted_fields = {
        "deducted_net_profit_ytd": 12,
        "deducted_net_profit_ytd_yoy": 50,
        "deducted_net_profit_single_quarter": 5,
        "deducted_net_profit_single_quarter_yoy": 200 / 3,
    }
    for name, expected in deducted_fields.items():
        if deducted_failure:
            assert fields[name]["value"] is None
            assert "deducted source failed" in fields[name]["reason"]
        else:
            assert fields[name]["value"] == pytest.approx(expected)
            assert fields[name]["reason"] is None
    if deducted_failure:
        assert "deducted source failed" in result["source_errors"]["deducted"]
    assert fields["price"]["periods"] == (
        ["2026-10-30T15:00:00+08:00"] if quote_kind == "valid" else []
    )
    if dps_override is not None:
        assert fields["dps"]["value"] is None
        assert fields["dps"]["reason"]
        if quote_kind != "invalid_price":
            assert "非正" in fields["dps"]["reason"]
    if quote_kind == "invalid_price" or dps_override is not None:
        assert fields["dps_at_150bp_spread"]["value"] is None
        assert fields["dps_at_100bp_spread"]["value"] is None
    elif missing:
        assert result["report_period"] is None
        assert result["source_errors"]
        assert fields["dps_at_150bp_spread"]["value"] is None
        assert fields["dps_at_150bp_spread"]["reason"]
    else:
        assert result["report_period"] == "2026-09-30"
        assert fields["revenue_single_quarter"]["value"] == 60
        assert fields["dps_at_150bp_spread"]["value"] == pytest.approx(0.7)
        assert fields["dps_at_100bp_spread"]["value"] == pytest.approx(0.6)
        assert fields["dps"]["value"] == (0.7167 if expected_split else 0.86)
        assert fields["dps"]["split_ratio"] == expected_split
        assert fields["dps"]["split_ex_date"] == (
            "2026-05-26" if expected_split else None
        )
        assert fields["dps"]["year"] == 2025
        assert fields["dps"]["periods"] == ["2025-06-30", "2025-12-31"]
        assert fields["dps_at_150bp_spread"]["periods"]
    assert fetcher.main(["quarter", "600000"]) == 0
    assert "季节性" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args", [[], ["bad"], ["600000", "--write"], ["600000", "--json", "extra"]]
)
def test_quarter_rejects_invalid_arguments(args):
    assert fetcher.main(["quarter", *args]) == 1


@pytest.mark.parametrize(
    "frame,field",
    [
        ("income", "net_profit_attributable_ttm"),
        ("cashflow", "operating_cash_flow_ttm"),
    ],
)
@pytest.mark.parametrize("missing", ["2026-09-30", "2025-09-30", "2025-12-31"])
def test_quarter_ttm_requires_each_input_period(quarter_frames, frame, field, missing):
    quarter_frames[frame] = quarter_frames[frame].loc[
        quarter_frames[frame]["end_date"] != missing
    ]
    fields = fetcher.build_quarter_check("600000", quarter_frames)["fields"]
    assert fields[field]["value"] is None
    assert fields[field]["reason"]
    assert fields["operating_cash_flow_to_net_profit_ttm"]["value"] is None


def test_quarter_ignores_indicator_period_and_prefers_corrected_deducted(
    quarter_frames,
):
    quarter_frames["indicator"].loc[4, "end_date"] = "2027-03-31"
    quarter_frames["deducted"].loc[5, "ann_date"] = "20261031"
    result = fetcher.build_quarter_check("600000", quarter_frames)
    assert result["report_period"] == "2026-09-30"
    assert result["fields"]["deducted_net_profit_ytd"]["value"] == 12


@pytest.mark.parametrize(
    "latest,expected_single,expected_ttm",
    [
        ("2025-12-31", 40, 14),
        ("2026-03-31", 30, 15),
    ],
)
def test_quarter_q4_derivation_and_complete_q1_ttm(
    latest, expected_single, expected_ttm
):
    income = pd.DataFrame(
        {
            "end_date": [
                "2024-09-30",
                "2024-12-31",
                "2025-03-31",
                "2025-09-30",
                "2025-12-31",
                "2026-03-31",
            ],
            "revenue": [70, 100, 20, 100, 140, 30],
            "n_income_attr_p": [7, 10, 2, 10, 14, 3],
        }
    )
    income = income.loc[income["end_date"] <= latest]
    fields = fetcher.build_quarter_check("600000", {"income": income})["fields"]
    assert fields["revenue_single_quarter"]["value"] == expected_single
    assert fields["revenue_single_quarter_yoy"]["value"] == pytest.approx(
        (expected_single / (30 if latest.endswith("12-31") else 20) - 1) * 100
    )
    assert fields["net_profit_attributable_ttm"]["value"] == expected_ttm


def test_quarter_annual_ttm_requires_latest_in_each_frame(quarter_frames):
    quarter_frames["balance"].loc[4, "end_date"] = "2026-12-31"
    fields = fetcher.build_quarter_check("600000", quarter_frames)["fields"]
    for name in ("operating_cash_flow_ttm", "net_profit_attributable_ttm"):
        assert fields[name]["value"] is None
        assert fields[name]["reason"]
