"""Stable provider surface consumed by a-stock applications.

Names listed in ``__all__`` are the supported public API. Other module-level
helpers in ``a_share_research.core.providers.*`` are implementation details.
"""

from .validated_realtime_quotes import QuoteObservation, validate_quote_observation
from .tushare_common import TushareProviderBase, TushareRateLimiter
from .tushare_financials import TushareDividendProvider, TushareFinancialProvider
from .tushare_fundamentals import TushareFundamentalsProvider
from .tushare_quotes import TushareMarketDataProvider, to_tushare_index_code, to_tushare_stock_code
from .tushare_valuation import TushareValuationProvider

__all__ = [
    "QuoteObservation",
    "TushareDividendProvider",
    "TushareFinancialProvider",
    "TushareFundamentalsProvider",
    "TushareMarketDataProvider",
    "TushareProviderBase",
    "TushareRateLimiter",
    "TushareValuationProvider",
    "to_tushare_index_code",
    "to_tushare_stock_code",
    "validate_quote_observation",
]
