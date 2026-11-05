"""Boundary stances: same-version reruns on 2026-11-04 (stance-stability-20261105) plus production report 109."""
import json
from pathlib import Path

import pytest

from a_share_research import reading, stance, validation

RUNS = json.loads((Path(__file__).parent / "data/stance-boundary-20261105.json").read_text())


def test_flipping_reruns_against_the_five_percent_band():
    # 新乳业 at 16.90 flipped 现在分批/等价格/等价格 against hurdles 17.74/16.17/15.89 (-4.7%/+4.5%/+6.4%):
    # the band catches both sides of the flip but not the third run, so it narrows rather than removes the noise.
    xinru = [r for r in RUNS if r["code"] == "002946.SZ"]
    assert [r["action"] for r in xinru] == ["现在分批", "等价格", "等价格"]
    assert [stance.on_boundary(r["valuation_calc"]) for r in xinru] == [True, True, False]
    # 紫金 at 29.25 held 现在分批 3/3 against hurdles 29.79–30.67 (-1.8% to -4.6%), all inside the band.
    assert all(stance.on_boundary(r["valuation_calc"]) for r in RUNS if r["code"] == "601899.SH")


def test_a_wide_margin_is_not_a_boundary():
    # 中国神华 report 109: close 50.98 against a 35.58 hurdle (+43%).
    shenhua = next(r for r in RUNS if r["code"] == "601088.SH")
    assert stance.hurdle_margin(shenhua["valuation_calc"]) == pytest.approx(0.433, abs=0.001)
    assert stance.on_boundary(shenhua["valuation_calc"]) is False


@pytest.mark.parametrize("calc", [None, {}, {"close": 16.9}, {"close": 16.9, "v2": {"support_below": None}},
                                  {"close": 16.9, "v2": {"support_below": -3}}, {"close": 0, "v2": {"support_below": 17}},
                                  {"close": True, "v2": {"support_below": 1}}])
def test_unpriced_or_invalid_hurdle_is_unknown_not_boundary(calc):
    assert stance.on_boundary(calc) is None


def test_edges_of_the_band():
    assert stance.on_boundary({"close": 105, "v2": {"support_below": 100}}) is True
    assert stance.on_boundary({"close": 105.01, "v2": {"support_below": 100}}) is False
    assert stance.on_boundary({"close": 94.99, "v2": {"support_below": 100}}) is False


def test_page_view_flags_boundary_only_inside_the_band():
    for run in RUNS:
        view = reading.stance_decision({"action": run["action"]}, {}, run["valuation_calc"])
        assert view["boundary"] is (stance.on_boundary(run["valuation_calc"]) is True)


def test_boundary_stances_are_summarized_apart():
    rows = [{"stance": "现在分批", "boundary": True, "returns": {"latest": {"excess": 0.10}}},
            {"stance": "现在分批", "boundary": False, "returns": {"latest": {"excess": -0.02}}},
            {"stance": "现在分批", "boundary": None, "returns": {"latest": {"excess": 0.04}}}]
    summary = validation.summarize_stances(rows)
    assert summary["现在分批·边界"] == {"n": 1, "scored": 1, "mean_excess": pytest.approx(0.10)}
    assert summary["现在分批"] == {"n": 2, "scored": 2, "mean_excess": pytest.approx(0.01)}
