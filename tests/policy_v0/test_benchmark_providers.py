"""Benchmark integration uses the installed library, never a live SDK client."""

import pandas as pd
import pytest
from a_share_research.core.providers import TushareMarketDataProvider

from a_share_research.policy.v0 import commands_holdings


class Client:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def index_daily(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame

    daily = index_daily


def install_provider(monkeypatch, frame):
    client = Client(frame)
    provider = TushareMarketDataProvider(token="fake-token", client=client)
    monkeypatch.setattr(commands_holdings.paths, "tushare_token", lambda: "fake-token")
    monkeypatch.setattr(
        commands_holdings, "TushareMarketDataProvider", lambda **_: provider
    )
    return client


@pytest.mark.parametrize("code", ["H00300.CSI", "H00922.CSI"])
def test_index_closes_use_library_contract(monkeypatch, code):
    client = install_provider(
        monkeypatch,
        pd.DataFrame({"trade_date": ["20261030"], "close": [1200.5]}),
    )
    assert commands_holdings._index_closes(code, "2026-10-01", "2026-11-01") == {
        "2026-10-30": 1200.5
    }
    assert client.calls[0]["ts_code"] == code
    assert client.calls[0]["start_date"] == "20261001"
    assert client.calls[0]["end_date"] == "20261101"


@pytest.mark.parametrize(
    ("day", "degraded"), [("2026-10-30", False), ("2026-11-01", True)]
)
def test_stock_close_surfaces_nearest_date(monkeypatch, capsys, day, degraded):
    install_provider(
        monkeypatch,
        pd.DataFrame(
            {
                "trade_date": ["20261030"],
                "open": [10],
                "high": [11],
                "low": [9],
                "close": [10.5],
                "vol": [100],
            }
        ),
    )
    assert commands_holdings._close_on("600900", day) == 10.5
    warning = capsys.readouterr().err
    if degraded:
        assert "2026-10-30" in warning and "相差 2 天" in warning
        assert "NEAREST_AVAILABLE_PRICE" in warning and "tushare.daily" in warning
    else:
        assert warning == ""


@pytest.mark.parametrize("kind", ["stock", "index"])
def test_empty_data_is_not_a_price(monkeypatch, kind):
    install_provider(monkeypatch, pd.DataFrame())
    with pytest.raises(RuntimeError, match="MISSING_COLUMNS"):
        if kind == "stock":
            commands_holdings._close_on("600900", "2026-11-01")
        else:
            commands_holdings._index_closes("H00300.CSI", "2026-10-01", "2026-11-01")
