"""Independent size pools on captured TuShare rows; no fabricated company or market rows.

The numeric boundary vectors below are unit conversions of the documented rules,
not captured quotes: 30e8 / 1e4 = 300000 万元, 100e8 / 1e4 = 1000000
万元, and 3000e4 / 1e3 = 30000 千元. Adjacent floats exercise the exact boundary.
"""

import copy
import json
import math
from collections import Counter
from pathlib import Path

import pandas as pd

from a_share_research.batches import NEW_RULE, RULE, small_rule
from a_share_research.screening_factors import (
    liquidity_mask, market_cap_mask, market_filters, pick, rank_candidates, score,
)

DATA = Path(__file__).parent / "data"
CAPTURED = json.loads((DATA / "screen-small-rows.json").read_text())
SMALL = small_rule(NEW_RULE)


def codes(rows):
    return [r["code"] for r in rows]


def test_small_units_include_30_yi_floor_and_exclude_100_yi_ceiling():
    # 亿 -> 万元; the 100 亿 boundary belongs only to the large pool.
    low, high = 30e8 / 1e4, 100e8 / 1e4
    values = pd.Series([math.nextafter(low, -math.inf), low,
                        math.nextafter(high, -math.inf), high])
    assert market_cap_mask(values, SMALL).tolist() == [False, True, True, False]
    assert market_cap_mask(values, RULE).tolist() == [False, False, False, True]


def test_small_liquidity_3000_wan_is_inclusive_in_tushare_thousands():
    boundary = 3000e4 / 1e3
    values = pd.Series([math.nextafter(boundary, -math.inf), boundary,
                        math.nextafter(boundary, math.inf), 1e8 / 1e3])
    assert liquidity_mask(values, SMALL).tolist() == [False, True, True, True]
    assert liquidity_mask(values, RULE).tolist() == [False, False, False, True]


def test_real_market_rows_stay_in_separate_pools():
    frame = pd.DataFrame(CAPTURED["large_market_rows"] + CAPTURED["small_market_rows"])
    args = (frame, set(CAPTURED["suspended"]), CAPTURED["as_of"])
    large, large_funnel, _ = market_filters(*args, RULE)
    small, small_funnel, _ = market_filters(*args, SMALL)
    assert set(large.ts_code) == {r["ts_code"] for r in CAPTURED["large_market_rows"]}
    assert set(small.ts_code) == {r["ts_code"] for r in CAPTURED["small_market_rows"]}
    assert set(large.ts_code).isdisjoint(small.ts_code)
    assert large_funnel["listed"] == small_funnel["listed"] == len(frame)
    assert large_funnel["after_industry_mapping"] == len(large)
    assert small_funnel["after_industry_mapping"] == len(small)


def test_frozen_v11_rankings_and_candidates_are_unchanged():
    rows = copy.deepcopy(CAPTURED["large_companies"])
    before = copy.deepcopy(rows)
    ranked, q20, q5 = rank_candidates(rows, RULE)
    assert [{"code": r["code"], "score": r["score"]} for r in ranked] == CAPTURED["frozen_large_ranked"]
    assert q20 == pick(ranked, 20, 3)
    assert q5 == codes(pick(q20, 5, 2))
    assert rows == before
    # Also retain the pre-existing 2026-10-23 live-screen scores verbatim.
    live = json.loads((DATA / "screen-rows.json").read_text())["rows"]
    ranked, _, _ = rank_candidates([r | {"status": "ok"} for r in live], RULE)
    assert codes(ranked) == codes(live)
    assert [r["score"] for r in ranked] == [r["score"] for r in live]


def test_small_ranks_are_local_and_do_not_change_large_ranks():
    small_rows = copy.deepcopy(CAPTURED["small_companies"])
    large_rows = copy.deepcopy(CAPTURED["large_companies"])
    small, _, _ = rank_candidates(small_rows, SMALL)
    large, _, _ = rank_candidates(large_rows, NEW_RULE)
    assert [{"code": r["code"], "score": r["score"]} for r in small] == CAPTURED["frozen_small_ranked"]
    assert [{"code": r["code"], "score": r["score"]} for r in large] == CAPTURED["frozen_large_ranked"]
    counts = Counter(r["industry"] for r in small_rows)
    assert all(r["group_size"] == counts[r["industry"]] for r in small)
    assert all(r["small_group"] == (counts[r["industry"]] < 10) for r in small)
    # Combining the captured same-day pools would change industry percentiles.
    combined = {r["code"]: r for r in score(small_rows + large_rows, RULE)}
    assert any(r["score"] != combined[r["code"]]["score"] for r in small)


def test_s10_and_sq3_have_their_own_seats_and_industry_limits():
    _, s10, sq3 = rank_candidates(CAPTURED["small_companies"], SMALL)
    assert len(s10) == 10 and max(Counter(r["industry"] for r in s10).values()) == 2
    assert len(sq3) == 3 and sq3 == codes(pick(s10, 3, 2))
    industry = {r["code"]: r["industry"] for r in s10}
    assert max(Counter(industry[c] for c in sq3).values()) <= 2
    assert SMALL["picks_per_industry"] == 1  # SL3's comparison limit is separate.


def test_sq3_can_fill_a_second_seat_from_one_industry():
    # The same captured rows restricted to two industries can fill three SQ3
    # seats, while the comparison's one-per-industry limit could fill only two.
    rows = [r for r in CAPTURED["small_companies"] if r["industry"] in {"食品饮料", "家用电器"}]
    _, s10, sq3 = rank_candidates(rows, SMALL)
    assert len(sq3) == 3
    assert len(pick(s10, 3, SMALL["picks_per_industry"])) == 2

