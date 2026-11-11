"""Real production valuations, reports 70–78; see fixture provenance.

Only dates/card availability and account scope are perturbed, never valuations.
"""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from a_share_research.policy.v0 import monitor_contract, monitor_research

HOLDINGS = ["600900", "000963", "002050", "603606"]


@pytest.fixture
def cards():
    path = Path(__file__).resolve().parents[1] / "fixtures/monitor/opportunity-cards-20261102.json"
    return json.loads(path.read_text())["response"]


def summarize(cards, day="2026-11-02"):
    monitor_contract.validate_research_cards(cards)
    research, prompts, escalations = monitor_research.summarize(
        {"status": "ok", **cards}, HOLDINGS, date.fromisoformat(day))
    payload = {"research": research, "review_prompts": prompts}
    monitor_contract._validate_research_fields(payload, set(HOLDINGS))
    assert not escalations
    return research, [p for p in prompts if p["kind"] == "opportunity_cost"]


def test_four_holdings_one_candidate(cards):
    research, prompts = summarize(cards)
    assert len(research["opportunity_costs"]) == 4
    assert len(prompts) == 1 and prompts[0]["code"] == "300750"
    assert prompts[0]["evidence"]["comparisons"] == research["opportunity_costs"]
    assert {r["holding"]["code"] for r in research["opportunity_costs"]} == set(HOLDINGS)
    for comparison in research["opportunity_costs"]:
        assert comparison["candidate"]["report_id"] == 75
        assert comparison["net_advantage"] == pytest.approx(
            comparison["candidate_return_without_dividends"] - comparison["holding_neutral_return"]
            - .001 / comparison["candidate_horizon"])
    assert "宁德时代" in prompts[0]["detail"] and "19.82%" in prompts[0]["detail"]
    assert "情景未经记分，仅提示复核" in prompts[0]["detail"]


@pytest.mark.parametrize("day,count", [("2026-11-09", 1), ("2026-11-10", 0), ("2026-11-01", 0)])
def test_snapshot_date_freshness_boundary(cards, day, count):
    original, _ = summarize(cards)
    research, prompts = summarize(cards, day)
    assert len(prompts) == count
    assert research["opportunity_costs"] == original["opportunity_costs"]


@pytest.mark.parametrize("completed,day,count", [
    ("2026-11-02T16:30:00Z", "2026-11-10", 1),  # 11-03 00:30 Shanghai: day 7, UTC date would be day 8
    ("2026-11-02T17:00:00Z", "2026-11-02", 0),  # 11-03 Shanghai is after the snapshot date
])
def test_utc_completed_at_uses_shanghai_date(cards, completed, day, count):
    for card in cards["cards"]:
        card["completed_at"] = completed
    _, prompts = summarize(cards, day)
    assert len(prompts) == count


def test_only_one_holding_fresh_includes_all_comparisons(cards):
    cards["cards"][0]["completed_at"] = "2026-11-10T00:00:00+08:00"
    research, prompts = summarize(cards, "2026-11-10")
    assert len(prompts) == 1
    assert len(prompts[0]["evidence"]["comparisons"]) == len(research["opportunity_costs"]) == 4


def test_two_candidates_each_win(cards):
    # Keep real valuations: move CATL's close date 4 days later, and one holding
    # 1 day later. CATL wins that holding; Allist wins the other three.
    for row in cards["cards"]:
        if row["code"] == "300750":
            row["current"]["last_close_date"] = "2026-11-06"
        elif row["code"] == HOLDINGS[0]:
            row["current"]["last_close_date"] = "2026-11-03"
    research, prompts = summarize(cards)
    assert {p["code"] for p in prompts} == {"300750", "688578"}
    assert sorted(len(p["evidence"]["comparisons"]) for p in prompts) == [1, 3]
    assert len(research["opportunity_costs"]) == 4
    assert "超过3日" in research["reason"]


@pytest.mark.parametrize("completed", [None, "missing", "not-a-date", "2026-99-99T00:00:00"])
def test_missing_or_invalid_completed_at_is_not_fresh(cards, completed):
    for row in cards["cards"]:
        if completed == "missing":
            del row["completed_at"]
        else:
            row["completed_at"] = completed
    research, prompts = summarize(cards)
    assert len(research["opportunity_costs"]) == 4 and not prompts
    assert research["status"] == "ok"


@pytest.mark.parametrize("mutation", ["number", "scope", "prompt_code", "empty", "unlisted"])
def test_digest_contract_rejects_invalid_evidence(cards, mutation):
    research, prompts = summarize(cards)
    payload = deepcopy({"research": research, "review_prompts": prompts})
    if mutation == "number":
        payload["research"]["opportunity_costs"][0]["net_advantage"] = None
    elif mutation == "scope":
        payload["research"]["opportunity_costs"][0]["holding"]["code"] = "999999"
    elif mutation == "prompt_code":
        payload["review_prompts"][0]["code"] = HOLDINGS[0]
    elif mutation == "empty":
        payload["review_prompts"][0]["evidence"]["comparisons"] = []
    else:
        payload["research"]["opportunity_costs"] = []
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract._validate_research_fields(payload, set(HOLDINGS))


def test_only_candidate_fresh(cards):
    next(row for row in cards["cards"] if row["code"] == "300750")["completed_at"] = "2026-11-10T18:10:13+08:00"
    research, prompts = summarize(cards, "2026-11-10")
    assert len(prompts) == 1 and len(research["opportunity_costs"]) == 4


def test_legacy_opportunity_prompt_still_readable(cards):
    research, _ = summarize(cards)
    comparisons = research.pop("opportunity_costs")
    prompts = [{"kind": "opportunity_cost", "code": row["holding"]["code"],
                "detail": "旧版逐持仓提示", "evidence": row} for row in comparisons]
    monitor_contract._validate_research_fields(
        {"research": research, "review_prompts": prompts}, set(HOLDINGS))


# Verbatim caveat emitted by Research valuation.at since c2d212f (2026-11-03).
ASSUMPTION = "按冻结累计分红尚未支付测算，未核对实际剩余现金流；不代表已核实的换仓回报差。"


def test_research_assumption_is_projected_and_shown_once(cards):
    _, plain = summarize(cards)
    assert "Research 注" not in plain[0]["detail"]
    for item in cards["cards"]:
        if (item["current"].get("valuation") or {}).get("annual_returns"):
            item["current"]["valuation"]["assumption"] = ASSUMPTION
    research, prompts = summarize(cards)
    rows = research["holdings"] + research["candidates"]
    assert {row["assumption"] for row in rows if row["neutral_return"] is not None} == {ASSUMPTION}
    assert prompts[0]["detail"] == plain[0]["detail"] + f"；Research 注：{ASSUMPTION}"
    # Mutation: a row that drops or rewrites the caveat no longer matches its card.
    research["holdings"][0]["assumption"] = None
    with pytest.raises(monitor_contract.MonitorContractError, match="assumption"):
        monitor_contract._validate_research_fields(
            {"research": research, "review_prompts": prompts}, set(HOLDINGS))


def test_snapshot_rows_without_assumption_remain_readable(cards):
    research, prompts = summarize(cards)
    for row in research["holdings"] + research["candidates"]:
        del row["assumption"]
    monitor_contract._validate_research_fields(
        {"research": research, "review_prompts": prompts}, set(HOLDINGS))


def test_untradable_board_never_becomes_a_switch_candidate(cards):
    """0.1.36: ChiNext 300750 stays a candidate under a STAR-only restriction; relabelled as a STAR
    code (identity change only, valuations untouched) it is excluded and a forged prompt is rejected."""
    monitor_contract.validate_research_cards(cards)
    day = date(2026, 11, 2)
    research, prompts, _ = monitor_research.summarize({"status": "ok", **cards}, HOLDINGS, day, ["STAR"])
    assert [p["code"] for p in prompts if p["kind"] == "opportunity_cost"] == ["300750"]
    starred = deepcopy(cards)
    for card in starred["cards"]:
        if card["code"] == "300750":
            card["code"] = "688750"
    monitor_contract.validate_research_cards(starred)
    research, prompts, _ = monitor_research.summarize({"status": "ok", **starred}, HOLDINGS, day, ["STAR"])
    monitor_contract._validate_research_fields({"research": research, "review_prompts": prompts}, set(HOLDINGS), ["STAR"])
    assert "688750" in [r["code"] for r in research["candidates"]]
    # The next-best tradable candidate may take its place; the excluded code never appears.
    assert all(c["candidate"]["code"] != "688750" for c in research["opportunity_costs"])
    assert "688750" not in [p["code"] for p in prompts]
    # Mutation: a snapshot built while ignoring the restriction names 688750; the contract rejects it.
    ignored, ignored_prompts, _ = monitor_research.summarize({"status": "ok", **starred}, HOLDINGS, day)
    assert "688750" in [p["code"] for p in ignored_prompts if p["kind"] == "opportunity_cost"]
    monitor_contract._validate_research_fields({"research": ignored, "review_prompts": ignored_prompts}, set(HOLDINGS))
    with pytest.raises(monitor_contract.MonitorContractError, match="cannot trade"):
        monitor_contract._validate_research_fields(
            {"research": ignored, "review_prompts": ignored_prompts}, set(HOLDINGS), ["STAR"])


def test_legacy_holding_prompt_cannot_name_untradable_board(cards):
    """Pre-0.1.30 shape (prompt on the holding, no research.opportunity_costs) obeys the restriction too."""
    starred = deepcopy(cards)
    for card in starred["cards"]:
        if card["code"] == "300750":
            card["code"] = "688750"
    monitor_contract.validate_research_cards(starred)
    research, _, _ = monitor_research.summarize({"status": "ok", **starred}, HOLDINGS, date(2026, 11, 2))
    comparison = next(c for c in research.pop("opportunity_costs") if c["candidate"]["code"] == "688750")
    legacy = {"research": research, "review_prompts": [{
        "code": comparison["holding"]["code"], "kind": "opportunity_cost", "detail": "旧格式换仓提示",
        "evidence": comparison}]}
    monitor_contract._validate_research_fields(legacy, set(HOLDINGS))
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract._validate_research_fields(legacy, set(HOLDINGS), ["STAR"])
