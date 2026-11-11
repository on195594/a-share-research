from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from a_share_research.core.market_data import MISSING_COLUMNS
from a_share_research.core.providers.tushare_fundamentals import (
    TushareFundamentalsProvider,
)


class _FakeProClient:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def stock_basic(self, exchange, list_status, fields):
        return self._df


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600036.SH", "002594.SZ"],
            "industry": ["银行", "汽车整车"],
        }
    )


def test_fetch_industry_map_returns_ok_with_mapping(tmp_path):
    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=tmp_path / "cache.json",
        client=_FakeProClient(_sample_df()),
    )
    result = provider.fetch_industry_map()
    assert result.status == "ok"
    assert result.value == {"600036": "银行", "002594": "汽车整车"}
    assert result.request_fingerprint is not None
    assert result.freshness_days == 0
    assert result.source_as_of is None
    assert result.row_count == 2


def test_fetch_industry_map_uses_cache_within_ttl(tmp_path):
    cache_path = tmp_path / "cache.json"
    fetched_at_epoch = time.time()
    cache_path.write_text(
        json.dumps(
            {
                "industry_map": {"600036": "银行"},
                "fetched_at": datetime.fromtimestamp(fetched_at_epoch).isoformat(),
                "fetched_at_epoch": fetched_at_epoch,
            }
        )
    )
    provider = TushareFundamentalsProvider(
        token="fake-token", cache_path=cache_path, ttl_seconds=3600
    )
    result = provider.fetch_industry_map()
    assert result.status == "ok"
    assert result.value == {"600036": "银行"}
    assert result.source_as_of is None
    assert result.request_fingerprint
    assert result.row_count == 1


def test_fetch_industry_map_treats_corrupt_cache_as_miss(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{")
    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=cache_path,
        ttl_seconds=3600,
        client=_FakeProClient(_sample_df()),
    )

    result = provider.fetch_industry_map()

    assert result.status == "ok"
    assert result.value == {"600036": "银行", "002594": "汽车整车"}


def test_fetch_industry_map_ignores_stale_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    fetched_at_epoch = time.time() - 999999
    cache_path.write_text(
        json.dumps(
            {
                "industry_map": {"600036": "银行"},
                "fetched_at": datetime.fromtimestamp(fetched_at_epoch).isoformat(),
                "fetched_at_epoch": fetched_at_epoch,
            }
        )
    )
    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=cache_path,
        ttl_seconds=3600,
        client=_FakeProClient(_sample_df()),
    )
    result = provider.fetch_industry_map()
    assert result.value == {"600036": "银行", "002594": "汽车整车"}


def test_write_cache_preserves_existing_cache_when_replace_fails(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    fetched_at_epoch = time.time()
    existing_payload = {
        "industry_map": {"600036": "银行"},
        "fetched_at": datetime.fromtimestamp(fetched_at_epoch).isoformat(),
        "fetched_at_epoch": fetched_at_epoch,
    }
    cache_path.write_text(json.dumps(existing_payload))
    provider = TushareFundamentalsProvider(token="fake-token", cache_path=cache_path)

    def fail_replace(self, target):
        if self.name.startswith(".cache.json."):
            raise OSError("simulated replace failure")
        return original_replace(self, target)

    original_replace = Path.replace
    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        provider._write_cache({"002594": "汽车整车"})

    assert json.loads(cache_path.read_text()) == existing_payload
    assert list(tmp_path.glob(".cache.json.*.tmp")) == []


def test_fetch_industry_map_degrades_when_cache_write_fails(tmp_path, monkeypatch):
    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=tmp_path / "cache.json",
        client=_FakeProClient(_sample_df()),
    )

    def fail_write(_value: dict[str, str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(provider, "_write_cache", fail_write)

    result = provider.fetch_industry_map()

    assert result.status == "degraded"
    assert result.value == {"600036": "银行", "002594": "汽车整车"}
    assert result.fallback_reason == "CACHE_WRITE_FAILED"


def test_fetch_industry_map_empty_response_fails(tmp_path):
    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=tmp_path / "cache.json",
        client=_FakeProClient(pd.DataFrame(columns=["ts_code", "industry"])),
    )
    result = provider.fetch_industry_map()
    assert result.status == "failed"
    assert result.error_code == "EMPTY_RESPONSE"


def test_fetch_industry_map_schema_changed_when_required_columns_missing(tmp_path):
    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=tmp_path / "cache.json",
        client=_FakeProClient(
            pd.DataFrame({"code": ["600036.SH"], "industry": ["银行"]})
        ),
    )

    result = provider.fetch_industry_map()

    assert result.status == "failed"
    assert result.error_code == MISSING_COLUMNS
    assert result.value is None


def test_fetch_industry_map_drops_nan_industries_and_normalizes_values(tmp_path):
    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=tmp_path / "cache.json",
        client=_FakeProClient(
            pd.DataFrame(
                {
                    "ts_code": ["600036.SH", "002594.SZ", "000001.SZ", "300750.SZ"],
                    "industry": [" 银行 ", float("nan"), None, 123],
                }
            )
        ),
    )

    result = provider.fetch_industry_map()

    assert result.status == "ok"
    assert result.value == {"600036": "银行", "300750": "123"}


def _write_industry_cache(
    path: Path,
    industry_map: object,
    fetched_at_epoch: int | float,
) -> None:
    fetched_at = datetime.fromtimestamp(fetched_at_epoch).isoformat()
    path.write_text(
        json.dumps(
            {
                "industry_map": industry_map,
                "fetched_at": fetched_at,
            }
        )
    )


def test_read_cached_industry_map_is_cache_only_and_returns_fresh_mapping(
    tmp_path, monkeypatch
):
    cache_path = tmp_path / "cache.json"
    _write_industry_cache(cache_path, {"600036": " 银行 "}, time.time())
    provider = TushareFundamentalsProvider(cache_path=cache_path, ttl_seconds=3600)
    monkeypatch.setattr(provider, "_request_frame", lambda *args, **kwargs: pytest.fail("network"))
    monkeypatch.setattr(provider, "_write_cache", lambda *args, **kwargs: pytest.fail("write"))
    before = cache_path.read_bytes()

    result = provider.read_cached_industry_map()

    assert result.status == "ok"
    assert result.value == {"600036": "银行"}
    assert result.error_code is None
    assert result.fallback_reason is None
    assert cache_path.read_bytes() == before


@pytest.mark.parametrize("age, error_code", [(7200, "CACHE_STALE"), (-60, "CACHE_MISSING")])
def test_read_cached_industry_map_rejects_stale_or_future_timestamp(
    tmp_path, age, error_code
):
    cache_path = tmp_path / "cache.json"
    _write_industry_cache(cache_path, {"600036": "银行"}, time.time() - age)
    provider = TushareFundamentalsProvider(cache_path=cache_path, ttl_seconds=3600)

    result = provider.read_cached_industry_map()

    assert result.status == "failed"
    assert result.error_code == error_code
    assert result.fallback_reason == error_code
    assert result.value is None


@pytest.mark.parametrize("content", [None, "{", '{"fetched_at": "not-a-date"}'])
def test_read_cached_industry_map_treats_unusable_cache_as_missing(tmp_path, content):
    cache_path = tmp_path / "cache.json"
    if content is not None:
        cache_path.write_text(content)

    result = TushareFundamentalsProvider(cache_path=cache_path).read_cached_industry_map()

    assert result.status == "failed"
    assert result.value is None
    assert result.error_code == "CACHE_MISSING"
    assert result.fallback_reason == "CACHE_MISSING"


@pytest.mark.parametrize("industry_map", [{}, {"600036": "   "}, {"bad": "银行"}])
def test_read_cached_industry_map_rejects_invalid_mapping(tmp_path, industry_map):
    cache_path = tmp_path / "cache.json"
    _write_industry_cache(cache_path, industry_map, time.time())

    result = TushareFundamentalsProvider(cache_path=cache_path).read_cached_industry_map()

    assert result.status == "failed"
    assert result.value is None
    assert result.error_code == "CACHE_MISSING"


def test_fundamentals_provider_accepts_client_factory_and_rate_limiter(tmp_path):
    from a_share_research.core.providers import TushareRateLimiter

    waits: list[str] = []

    class _Limiter(TushareRateLimiter):
        def wait(self) -> None:
            waits.append("wait")

    tokens: list[str] = []

    def factory(token: str):
        tokens.append(token)
        return _FakeProClient(_sample_df())

    provider = TushareFundamentalsProvider(
        token="fake-token",
        cache_path=tmp_path / "cache.json",
        client_factory=factory,
        rate_limiter=_Limiter(),
    )
    result = provider.fetch_industry_map()
    assert result.status == "ok"
    assert tokens == ["fake-token"]
    assert waits == ["wait"]
