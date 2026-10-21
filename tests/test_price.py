"""Price levels on 61 real sessions of 长江电力 and CSI 300 ending 2026-10-20."""

import json
from pathlib import Path

import pytest
from a_share_research.price import PriceError, compute

DATA = json.loads((Path(__file__).parent / "data" / "600900-61d.json").read_text())


def test_levels_match_the_reference_run():
    # Same values the live reference produced for 2026-10-20.
    levels = compute(DATA["stock"], DATA["benchmark"])
    assert levels == {"close": 29.69, "ma20": 28.79, "ma60": 28.5, "relative": 11.02,
                      "trigger": 29.81, "stop": 28.33, "distance": 4.58, "uptrend": True}


def test_short_history_is_refused():
    with pytest.raises(PriceError):
        compute(DATA["stock"][1:], DATA["benchmark"][1:])
