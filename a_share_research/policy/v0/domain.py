"""Pure time, classification, and holding-status rules."""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

from a_share_research.policy.v0 import framework_catalog
from a_share_research.policy.v0.market_quotes import PriceQuote

_CST = timezone(timedelta(hours=8))
_UTC = timezone.utc
_UNSUPPORTED_FINANCIAL_KEYWORDS = ("保险", "券商", "证券")
INDUSTRY_TTL_MAP = [
    (
        [
            "银行",
            "保险",
            "券商",
            "国有大行",
            "股份制银行",
            "城商行",
            "农商行",
            "水电",
            "公用事业",
            "电网",
            "水务",
            "燃气",
            "高速",
        ],
        72,
    ),
    (["白酒", "消费", "食品", "零售", "饮料", "乳制品"], 12),
]
DEFAULT_STOP_LOSS_PCT = (0.85, 0.80)
ANALYSIS_PRICE_INVALIDATION_THRESHOLD = 0.03
FRAMEWORK_ALIASES = {
    "A": "A通用",
    "A通用": "A通用",
    "B": "B银行",
    "B银行": "B银行",
    "C": "C资源",
    "C资源": "C资源",
    "D": "D公用",
    "D公用": "D公用",
    "E": "E消费",
    "E消费": "E消费",
    "F": "F科技",
    "F科技": "F科技",
}


def utc_now() -> datetime:
    return datetime.now(_UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def cst_now() -> datetime:
    return datetime.now(_CST)


def cst_today() -> str:
    return utc_now().astimezone(_CST).date().isoformat()


def parse_timestamp_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CST)
    return parsed.astimezone(_UTC)


def format_timestamp_cst(value: str) -> str:
    try:
        return parse_timestamp_utc(value).astimezone(_CST).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)


def is_expired(updated_at: str, ttl_hours: int) -> bool:
    try:
        return utc_now() - parse_timestamp_utc(updated_at) > timedelta(hours=ttl_hours)
    except (TypeError, ValueError):
        return True


def is_unsupported_financial_industry(industry: str | None) -> bool:
    return bool(
        industry
        and any(keyword in industry for keyword in _UNSUPPORTED_FINANCIAL_KEYWORDS)
    )


def get_industry_ttl(industry: str) -> int:
    for keywords, ttl in INDUSTRY_TTL_MAP:
        if any(keyword in industry for keyword in keywords):
            return ttl
    return 24


def infer_framework(industry: str | None) -> tuple[str, bool]:
    if (
        not industry
        or "未知" in industry
        or is_unsupported_financial_industry(industry)
    ):
        return "A通用", False
    for routing in framework_catalog.FRAMEWORK_ROUTING.values():
        if any(keyword in industry for keyword in routing.industry_keywords):
            return routing.portfolio_label, True
    return "A通用", True


def get_stop_loss_pct(framework: str) -> tuple[float, float]:
    for routing in framework_catalog.FRAMEWORK_ROUTING.values():
        if routing.portfolio_label == framework and routing.stop_loss_pct is not None:
            return routing.stop_loss_pct
    return DEFAULT_STOP_LOSS_PCT


def is_a_share_trading_hours(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    current = moment.time()
    return dtime(9, 30) <= current <= dtime(11, 30) or dtime(13, 0) <= current <= dtime(
        15, 0
    )


def _legacy_style_status(
    current: float,
    stop_15: float,
    stop_20: float,
    price_label: str,
) -> tuple[str, bool]:
    if current <= stop_20:
        return (
            f"🔴 已跌破20%止损线（{stop_20:.3f}），{price_label}{current:.2f}，建议立即止损",
            True,
        )
    if current <= stop_15:
        return (
            f"⚠️ 已跌破15%止损线（{stop_15:.3f}），{price_label}{current:.2f}，需提高警惕",
            True,
        )
    return (
        f"✅ 正常，{price_label}{current:.2f}（止损15%:{stop_15:.3f} 20%:{stop_20:.3f}）",
        False,
    )


def _intraday_status(
    current: float,
    stop_15: float,
    stop_20: float,
) -> tuple[str, bool]:
    if current <= stop_20:
        return (
            f"🚨 盘中已跌破20%止损线（{stop_20:.3f}），现价{current:.2f}，建议立即止损",
            True,
        )
    if current <= stop_15:
        return (
            f"🚨 盘中已跌破15%止损线（{stop_15:.3f}），现价{current:.2f}，需提高警惕",
            True,
        )
    return (
        f"✅ 正常，现价{current:.2f}（止损15%:{stop_15:.3f} 20%:{stop_20:.3f}）",
        False,
    )


def evaluate_holding_status(
    code: str,
    name: str | None,
    stop_15: float,
    stop_20: float,
    quote: PriceQuote | None,
    today: str,
    now: datetime,
) -> tuple[str, str, bool]:
    label = f"{name}({code})" if name else code
    current = quote.price if quote else None
    if current is None:
        status, alert = "─ 无实时价格", False
    elif quote.quote_date is not None and quote.quote_date != today:
        status = f"📋 上一交易日收盘价观察提醒（{quote.quote_date}收盘{current:.2f}，非当前价，请开盘后复核）"
        alert = False
    elif quote.quote_date == today and is_a_share_trading_hours(now):
        status, alert = _intraday_status(current, stop_15, stop_20)
    elif quote.quote_date == today:
        status, alert = _legacy_style_status(current, stop_15, stop_20, "收盘价")
    else:
        status, alert = "─ 行情时间戳不可验证，不触发止损预警", False
    return label, status, alert
