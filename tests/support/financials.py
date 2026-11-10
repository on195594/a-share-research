"""Captured financial inputs shared by series, chart and calculation regressions."""

import copy
import functools
import json
from pathlib import Path

import pandas as pd

from a_share_research.material import financial_series

DATA = Path(__file__).resolve().parents[1] / "data"
CAPTURES = json.loads((DATA / "financial-series-captures.json").read_text())["companies"]
FINANCIAL_DATA = json.loads((DATA / "600519-financials.json").read_text())


def frames(captured):
    return {key: pd.DataFrame(rows) for key, rows in captured["financials"].items()}


@functools.lru_cache(maxsize=None)
def _build_cached(code="000858"):
    captured = CAPTURES[code]
    return financial_series(frames(captured), captured["code"], captured["retrieved_at"])


def build(code="000858"):
    """Return an isolated copy; callers must not mutate the shared cached series."""
    return copy.deepcopy(_build_cached(code))
