from __future__ import annotations

import os
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import requests

import pytest

from datetime import date

from a_share_research import db, market
from a_share_research.core.market_data import (
    PERMISSION_DENIED,
    RATE_LIMITED,
    REMOTE_DISCONNECTED,
    TIMEOUT,
)
from a_share_research.core.providers.tushare_common import (
    TushareRateLimiter,
    call_with_network_retry,
    classify_tushare_exception,
    compact_date,
    read_tushare_token,
)
from a_share_research.core.providers import (
    TushareDividendProvider,
    TushareFinancialProvider,
    TushareFundamentalsProvider,
    TushareMarketDataProvider,
    TushareProviderBase,
    TushareValuationProvider,
)


@pytest.mark.parametrize(
    "provider_type",
    [
        TushareProviderBase,
        TushareMarketDataProvider,
        TushareFundamentalsProvider,
        TushareFinancialProvider,
        TushareDividendProvider,
        TushareValuationProvider,
    ],
)
def test_provider_does_not_discover_another_projects_credentials(monkeypatch, provider_type):
    monkeypatch.setattr(
        "a_share_research.core.providers.tushare_common.read_tushare_token",
        lambda path: pytest.fail("credential files require an explicit env_path"),
    )
    assert provider_type().token is None
    monkeypatch.setenv("TUSHARE_TOKEN", "environment-token")
    assert provider_type().token == "environment-token"
    assert provider_type(token="explicit-token").token == "explicit-token"
    assert provider_type(token="").token == ""


class _FakeClock:
    def __init__(self) -> None:
        self.current = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def test_default_suite_starts_without_external_credentials() -> None:
    for name in ("TUSHARE_TOKEN", "TG_TOKEN", "TG_CHAT_ID", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        assert name not in os.environ


def test_read_tushare_token_handles_spaces_comments_and_directories(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text('TUSHARE_TOKEN = "file-token-789" # comment\n')

    assert read_tushare_token(env_path) == "file-token-789"
    assert read_tushare_token(tmp_path) is None


def test_rate_limiter_spaces_calls_below_configured_ceiling() -> None:
    fake = _FakeClock()
    limiter = TushareRateLimiter(calls_per_minute=120, clock=fake.monotonic, sleep=fake.sleep)

    limiter.wait()
    limiter.wait()
    limiter.wait()

    assert fake.sleeps == pytest.approx([0.5, 0.5])


def test_rate_limiter_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="calls_per_minute"):
        TushareRateLimiter(calls_per_minute=0)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RuntimeError("抱歉，您没有权限访问该接口，权限由积分决定"), PERMISSION_DENIED),
        (RuntimeError("每分钟最多访问该接口180次，访问频次超限"), RATE_LIMITED),
        (RuntimeError("request rate limit exceeded"), RATE_LIMITED),
        (RuntimeError("time limit exceeded"), TIMEOUT),
        (TimeoutError("read timed out"), TIMEOUT),
        (ConnectionError("remote disconnected"), REMOTE_DISCONNECTED),
    ],
)
def test_classify_tushare_exception_uses_stable_error_categories(
    exc: Exception,
    expected: str,
) -> None:
    code, _ = classify_tushare_exception(exc)
    assert code == expected


def test_classify_tushare_exception_redacts_token() -> None:
    code, message = classify_tushare_exception(
        RuntimeError("request failed for secret-token-123"),
        token="secret-token-123",
    )

    assert code is not None
    assert "secret-token-123" not in message
    assert "[REDACTED]" in message


def test_network_timeout_is_retried_once() -> None:
    calls = 0
    fake = _FakeClock()
    limiter = TushareRateLimiter(calls_per_minute=180, clock=fake.monotonic, sleep=fake.sleep)

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("temporary")
        return "ok"

    assert call_with_network_retry(flaky, limiter=limiter) == "ok"
    assert calls == 2


def test_generic_api_error_with_timeout_text_is_not_retried() -> None:
    calls = 0
    limiter = TushareRateLimiter(calls_per_minute=180, clock=lambda: 0.0, sleep=lambda _: None)

    def fail() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("API timeout quota message")

    with pytest.raises(RuntimeError, match="quota"):
        call_with_network_retry(fail, limiter=limiter)

    assert calls == 1


@pytest.mark.parametrize(
    ("raw_input", "expected"),
    [
        ("2026-09-03", "20260903"),
        (" 2026-09-03 ", "20260903"),
        ("20260903", "20260903"),
        (" 20260903 ", "20260903"),
        ("2026-09-03T15:30:00", "20260903"),
        (date(2026, 9, 3), "20260903"),
    ],
)
def test_compact_date_normalizes_various_inputs(raw_input: str | date, expected: str) -> None:
    assert compact_date(raw_input) == expected


def captured_market_frame():
    path = Path(__file__).resolve().parents[1] / "data" / "600519-financials.json"
    return pd.DataFrame([json.loads(path.read_text())["market"]])


def test_checked_client_pools_one_session_per_thread(monkeypatch):
    from a_share_research.core.providers.tushare_common import checked_client

    frame = captured_market_frame()
    reply = SimpleNamespace(ok=True, text=json.dumps({"code": 0, "data": {
        "fields": list(frame.columns), "items": frame.values.tolist()}}))
    calls = []

    def post(session, url, **kwargs):
        calls.append((session, kwargs))
        return reply

    monkeypatch.setattr(requests.Session, "post", post)
    client = checked_client("captured-response-only", timeout=17)
    for _ in range(2):
        pd.testing.assert_frame_equal(client.daily_basic(ts_code="600519.SH"), frame)
    with ThreadPoolExecutor(1) as pool:
        pool.submit(client.daily_basic, ts_code="600519.SH").result()
    sessions = [session for session, _ in calls]
    assert sessions[0] is sessions[1] and sessions[2] is not sessions[0]
    assert {kwargs["timeout"] for _, kwargs in calls} == {17}


@pytest.mark.parametrize("injection", [None, "client", "client_factory"])
def test_provider_cache_skips_limiter_and_respects_injected_clients(monkeypatch, injection):
    monkeypatch.setenv("TUSHARE_CACHE", "1")
    series = json.loads((Path(__file__).parents[1] / "data/account-nav-closes-202611.json").read_text())["index"]
    day = "2026-11-12"
    frame = pd.DataFrame([{"trade_date": "20261112", "close": series[day]}])
    remote_calls, waits = [], []

    def index_daily(**kwargs):
        remote_calls.append(kwargs)
        return frame.copy()

    client = SimpleNamespace(index_daily=index_daily)
    monkeypatch.setattr(TushareMarketDataProvider, "_build_client", lambda self: client)
    kwargs = {injection: client if injection == "client" else lambda token: client} if injection else {}
    provider = TushareMarketDataProvider(token="captured-response-only", query_cache=market.cached_query,
                                         rate_limiter=SimpleNamespace(wait=lambda: waits.append(1)), **kwargs)
    for _ in range(2):
        result = provider.fetch_index_bars("H00906.CSI", day, day)
        assert result.status == "ok" and result.value["close"].tolist() == [series[day]]
    assert len(remote_calls) == len(waits) == (2 if injection else 1)
    assert (db.DATA / "tushare_cache.db").exists() is (injection is None)
