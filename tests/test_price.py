"""Price levels on 61 real sessions of 长江电力 and CSI 300 ending 2026-10-20."""

import json
from pathlib import Path

import pytest
from a_share_research.price import PriceError, SUPPORTED_CODE, compute

DATA = json.loads((Path(__file__).parent / "data" / "600900-61d.json").read_text())


def test_levels_match_the_reference_run():
    # Same values the live reference produced for 2026-10-20.
    levels = compute(DATA["stock"], DATA["benchmark"])
    assert levels == {"close": 29.69, "ma20": 28.79, "ma60": 28.5, "relative": 11.02,
                      "trigger": 29.81, "stop": 28.33, "distance": 4.58, "uptrend": True}


def test_short_history_is_refused():
    with pytest.raises(PriceError):
        compute(DATA["stock"][1:], DATA["benchmark"][1:])


def test_supported_board_codes_and_exchange_boundaries():
    # Positive codes come from real research/price runs; the variants below are invalid inputs.
    for code in ("600900.SH", "002648.SZ", "300750.SZ", "301269.SZ", "688981.SH", "688208.SH"):
        assert SUPPORTED_CODE.fullmatch(code)
    for code in ("600900.SZ", "300750.SH", "688981.SZ", "688981.BJ", "689009.SH", "200002.SZ"):
        assert not SUPPORTED_CODE.fullmatch(code)
