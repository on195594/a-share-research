from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from a_share_research.core.market_data import (
    CACHE_MISSING,
    CACHE_STALE,
    MarketDataResult,
    now,
)
from a_share_research.core.providers.tushare_common import (
    ClientFactory,
    TushareProviderBase,
    TushareRateLimiter,
    request_fingerprint,
)

TUSHARE_FUNDAMENTALS_SOURCE = "tushare.stock_basic"
_INDUSTRY_PARAMS = {"exchange": "", "list_status": "L", "fields": "ts_code,industry"}
_STOCK_CODE_RE = re.compile(r"[0-9]{6}", re.ASCII)
DEFAULT_CACHE_PATH = (
    Path.home() / ".cache" / "a_stock_lib" / "tushare_industry_map.json"
)
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30天，行业分类极少变化，不需要高频刷新


class TushareFundamentalsProvider(TushareProviderBase):
    """批量获取全市场行业分类，本地缓存（默认30天TTL），非逐股高频查询。"""

    def __init__(
        self,
        token: str | None = None,
        cache_path: Path = DEFAULT_CACHE_PATH,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        client: Any | None = None,
        env_path: Path | None = None,
        client_factory: ClientFactory | None = None,
        rate_limiter: TushareRateLimiter | None = None,
    ) -> None:
        super().__init__(
            token=token,
            client=client,
            client_factory=client_factory,
            env_path=env_path,
            rate_limiter=rate_limiter,
        )
        self.cache_path = Path(cache_path)
        self.ttl_seconds = ttl_seconds

    def fetch_industry_map(
        self, force_refresh: bool = False
    ) -> MarketDataResult[dict[str, str]]:
        if not force_refresh:
            cached = self._read_cache()
            if cached is not None:
                return cached
        result = self._request_frame(
            TUSHARE_FUNDAMENTALS_SOURCE,
            "stock_basic",
            _INDUSTRY_PARAMS,
            {"ts_code", "industry"},
        )
        if result.value is None:
            return result
        df = result.value
        clean_df = df.dropna(subset=["industry"])
        industry_map: dict[str, str] = {}
        for row in clean_df.itertuples(index=False):
            ts_code = getattr(row, "ts_code", "")
            industry = str(getattr(row, "industry", "")).strip()
            if industry and ts_code:
                industry_map[str(ts_code).split(".")[0]] = industry
        try:
            self._write_cache(industry_map)
        except OSError as exc:
            return MarketDataResult(
                industry_map,
                "degraded",
                result.source,
                result.fetched_at,
                fallback_reason="CACHE_WRITE_FAILED",
                error_message=str(exc),
                freshness_days=0,
                request_fingerprint=result.request_fingerprint,
                row_count=len(industry_map),
            )
        return MarketDataResult(
            industry_map,
            result.status,
            result.source,
            result.fetched_at,
            freshness_days=0,
            request_fingerprint=result.request_fingerprint,
            row_count=len(industry_map),
        )

    def _read_cache(self) -> MarketDataResult[dict[str, str]] | None:
        result = self.read_cached_industry_map()
        return result if result.status == "ok" else None

    def read_cached_industry_map(self) -> MarketDataResult[dict[str, str]]:
        """Read and validate the configured cache without network access or writes."""
        fingerprint = request_fingerprint("stock_basic", _INDUSTRY_PARAMS)
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            fetched_at = payload["fetched_at"]
            epoch = datetime.fromisoformat(fetched_at).timestamp()
            industry_map = self._validated_industry_map(payload["industry_map"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return self._cache_failure(fingerprint, str(exc))

        age_seconds = time.time() - epoch
        if age_seconds < 0 or industry_map is None:
            return self._cache_failure(fingerprint)

        freshness_days = int(age_seconds / 86400)
        if age_seconds > self.ttl_seconds:
            return MarketDataResult(
                None,
                "failed",
                TUSHARE_FUNDAMENTALS_SOURCE,
                fetched_at,
                fallback_reason=CACHE_STALE,
                error_code=CACHE_STALE,
                freshness_days=freshness_days,
                request_fingerprint=fingerprint,
                row_count=len(industry_map),
            )
        return MarketDataResult(
            industry_map,
            "ok",
            TUSHARE_FUNDAMENTALS_SOURCE,
            fetched_at,
            freshness_days=freshness_days,
            request_fingerprint=fingerprint,
            row_count=len(industry_map),
        )

    @staticmethod
    def _validated_industry_map(value: object) -> dict[str, str] | None:
        if not isinstance(value, dict) or not value:
            return None
        result: dict[str, str] = {}
        for code, industry in value.items():
            if (
                not isinstance(code, str)
                or _STOCK_CODE_RE.fullmatch(code) is None
                or not isinstance(industry, str)
                or not industry.strip()
            ):
                return None
            result[code] = industry.strip()
        return result

    @staticmethod
    def _cache_failure(
        fingerprint: str, message: str | None = None
    ) -> MarketDataResult[dict[str, str]]:
        return MarketDataResult(
            None,
            "failed",
            TUSHARE_FUNDAMENTALS_SOURCE,
            now(),
            fallback_reason=CACHE_MISSING,
            error_code=CACHE_MISSING,
            error_message=message,
            request_fingerprint=fingerprint,
        )

    def _write_cache(self, industry_map: dict[str, str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "industry_map": industry_map,
                "fetched_at": now(),
            },
            ensure_ascii=False,
        )
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.cache_path.parent,
                prefix=f".{self.cache_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            temp_path.replace(self.cache_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
