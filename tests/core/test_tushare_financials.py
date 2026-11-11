from __future__ import annotations

import pandas as pd
import pytest

from a_share_research.core.market_data import MISSING_COLUMNS
from a_share_research.core.providers.tushare_financials import (
    BALANCE_SHEET_SOURCE,
    CASHFLOW_SOURCE,
    DIVIDEND_SOURCE,
    FINA_INDICATOR_SOURCE,
    INCOME_SOURCE,
    TushareDividendProvider,
    TushareFinancialProvider,
)


class _FakeFinancialClient:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _response(self, endpoint: str, kwargs: dict[str, object]) -> pd.DataFrame:
        self.calls.append((endpoint, kwargs))
        return self.frames[endpoint].copy()

    def fina_indicator(self, **kwargs: object) -> pd.DataFrame:
        return self._response("fina_indicator", kwargs)

    def income(self, **kwargs: object) -> pd.DataFrame:
        return self._response("income", kwargs)

    def balancesheet(self, **kwargs: object) -> pd.DataFrame:
        return self._response("balancesheet", kwargs)

    def cashflow(self, **kwargs: object) -> pd.DataFrame:
        return self._response("cashflow", kwargs)

    def dividend(self, **kwargs: object) -> pd.DataFrame:
        return self._response("dividend", kwargs)


def _statement_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600036.SH"],
            "ann_date": ["20260425"],
            "f_ann_date": ["20260426"],
            "end_date": ["20251231"],
            "report_type": ["1"],
            "comp_type": ["2"],
            "end_type": ["4"],
            "update_flag": ["1"],
            "n_income_attr_p": ["148000000000"],
        }
    )


def _frames() -> dict[str, pd.DataFrame]:
    statement = _statement_frame()
    return {
        "fina_indicator": pd.DataFrame(
            {
                "ts_code": ["600036.SH"],
                "ann_date": ["20260425"],
                "end_date": ["20251231"],
                "update_flag": ["1"],
                "roe_waa": ["15.2"],
                "bps": ["42.1"],
            }
        ),
        "income": statement,
        "balancesheet": statement.rename(columns={"n_income_attr_p": "total_assets"}),
        "cashflow": statement.rename(columns={"n_income_attr_p": "n_cashflow_act"}),
        "dividend": pd.DataFrame(
            {
                "ts_code": ["600036.SH"],
                "end_date": ["20251231"],
                "ann_date": ["20260320"],
                "div_proc": ["实施"],
                "cash_div": ["1.80"],
                "cash_div_tax": ["2.00"],
                "record_date": ["20260710"],
                "ex_date": ["20260713"],
                "imp_ann_date": ["20260701"],
            }
        ),
    }


def test_indicator_history_keeps_unavailable_common_keys_null() -> None:
    client = _FakeFinancialClient(_frames())
    provider = TushareFinancialProvider(token="fake-token", client=client)

    result = provider.fetch_indicator_history("600036", "2025-01-01", "2026-07-20")

    assert result.status == "ok"
    assert result.source == FINA_INDICATOR_SOURCE
    assert result.row_count == 1
    assert result.source_as_of == "2026-04-25"
    assert result.value is not None
    row = result.value.iloc[0]
    assert row["endpoint"] == "fina_indicator"
    for column in ("f_ann_date", "report_type", "comp_type", "end_type"):
        assert pd.isna(row[column])
    assert client.calls[0] == (
        "fina_indicator",
        {
            "ts_code": "600036.SH",
            "start_date": "20250101",
            "end_date": "20260720",
            "fields": (
                "ts_code,ann_date,end_date,update_flag,roe_waa,netprofit_yoy,"
                "debt_to_assets,grossprofit_margin,bps,or_yoy,dt_netprofit_yoy"
            ),
        },
    )


@pytest.mark.parametrize(
    ("method_name", "endpoint", "source"),
    [
        ("fetch_income_history", "income", INCOME_SOURCE),
        ("fetch_balance_history", "balancesheet", BALANCE_SHEET_SOURCE),
        ("fetch_cashflow_history", "cashflow", CASHFLOW_SOURCE),
    ],
)
def test_statement_history_preserves_point_in_time_keys(
    method_name: str,
    endpoint: str,
    source: str,
) -> None:
    client = _FakeFinancialClient(_frames())
    provider = TushareFinancialProvider(token="fake-token", client=client)

    result = getattr(provider, method_name)("600036", "2025-01-01", "2026-07-20")

    assert result.status == "ok"
    assert result.source == source
    assert result.source_as_of == "2026-04-26"
    assert result.value is not None
    row = result.value.iloc[0]
    assert row["endpoint"] == endpoint
    assert row["ann_date"] == "2026-04-25"
    assert row["f_ann_date"] == "2026-04-26"
    assert row["end_date"] == "2025-12-31"
    assert row["report_type"] == "1"
    assert row["comp_type"] == "2"
    assert row["update_flag"] == "1"
    assert result.row_count == 1
    assert result.request_fingerprint
    assert result.fetched_at
    numeric = {"income": "n_income_attr_p", "balancesheet": "total_assets", "cashflow": "n_cashflow_act"}[endpoint]
    assert row[numeric] == "148000000000"  # statement payloads retain source units/types
    assert client.calls[0][0] == endpoint
    assert client.calls[0][1]["start_date"] == "20250101"
    assert client.calls[0][1]["end_date"] == "20260720"


@pytest.mark.parametrize(("method", "endpoint"), [
    ("fetch_income_history", "income"),
    ("fetch_balance_history", "balancesheet"),
    ("fetch_cashflow_history", "cashflow"),
])
def test_statement_history_fails_when_report_type_is_missing(method, endpoint) -> None:
    frames = _frames()
    frames[endpoint] = frames[endpoint].drop(columns="report_type")
    provider = TushareFinancialProvider(
        token="fake-token", client=_FakeFinancialClient(frames)
    )

    result = getattr(provider, method)("600036")

    assert result.status == "failed"
    assert result.error_code == MISSING_COLUMNS


def test_indicator_history_fails_when_update_flag_is_missing() -> None:
    frames = _frames()
    frames["fina_indicator"] = frames["fina_indicator"].drop(columns="update_flag")
    provider = TushareFinancialProvider(
        token="fake-token", client=_FakeFinancialClient(frames)
    )

    result = provider.fetch_indicator_history("600036")

    assert result.status == "failed"
    assert result.error_code == MISSING_COLUMNS


def test_dividend_history_preserves_tax_semantics_and_dates() -> None:
    client = _FakeFinancialClient(_frames())
    provider = TushareDividendProvider(token="fake-token", client=client)

    result = provider.fetch_dividend_history("600036")

    assert result.status == "ok"
    assert result.source == DIVIDEND_SOURCE
    assert result.source_as_of == "2026-07-01"
    assert result.value is not None
    row = result.value.iloc[0]
    assert row["cash_div"] == pytest.approx(1.8)  # official field: after tax
    assert row["cash_div_tax"] == pytest.approx(2.0)  # official field: before tax
    assert row["record_date"] == "2026-07-10"
    assert row["ex_date"] == "2026-07-13"
    assert client.calls == [("dividend", {"ts_code": "600036.SH"})]


def test_empty_dividend_history_is_valid_no_event_result() -> None:
    frames = _frames()
    frames["dividend"] = frames["dividend"].iloc[0:0]
    provider = TushareDividendProvider(
        token="fake-token", client=_FakeFinancialClient(frames)
    )

    result = provider.fetch_dividend_history("600036")

    assert result.status == "ok"
    assert result.row_count == 0
    assert result.value is not None
    assert result.value.empty
