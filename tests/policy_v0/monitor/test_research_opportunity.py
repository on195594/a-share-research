"""Research opportunity prompts from the V4 stance and v2 expected return (0.1.35).

The card is a real Research company_card from the 2026-11-04 艾力斯 688578 smoke research
(see fixture provenance). Stance/price/date perturbations below are synthetic test inputs.
"""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from a_share_research.policy.v0 import monitor_contract, monitor_research

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/monitor/research-card-stance.json"
DAY = date(2026, 11, 4)


@pytest.fixture
def card():
    return json.loads(FIXTURE.read_text())["card"]


def run(card, day=DAY, holdings=()):
    payload = {"cards": [card], "missing": []}
    monitor_contract.validate_research_cards(payload)
    research, prompts, escalations = monitor_research.summarize({"status": "ok", **payload}, list(holdings), day)
    monitor_contract._validate_research_fields({"research": research, "review_prompts": prompts}, set(holdings))
    return research, [p for p in prompts if p["kind"] == "research_opportunity"], escalations


def test_real_card_projects_stance_and_prompts_on_v2_support(card):
    research, prompts, escalations = run(card)
    row = research["candidates"][0]
    # v1 calls the 83.08 close neutral (conservative case -17.6%); v2 weights it to 18.4% expected.
    assert row["zone"] == "中性区" and row["v2_zone"] == "测算支持区"
    assert row["expected_return"] == pytest.approx(0.1840264176679583)
    assert row["stance_action"] == "现在分批" and row["stance_price"] is None
    assert len(prompts) == 1 and not escalations
    assert "18.40%" in prompts[0]["detail"] and "position-size" in prompts[0]["detail"]
    assert prompts[0]["evidence"]["report_id"] == card["report_id"]


def test_stance_alone_needs_a_fresh_report_and_price_wait_needs_the_price(card):
    card["current"]["valuation"]["v2"]["zone"] = "中性区"
    assert len(run(card)[1]) == 1  # 现在分批 within 7 days
    assert run(card, day=date(2026, 11, 20))[1] == []
    card["stance_calc"] = {"action": "等价格", "price": 83.08, "valid_until": "2027-05-04"}
    assert "等待价 83.08" in run(card, day=date(2026, 11, 20))[1][0]["detail"]
    card["stance_calc"]["price"] = 83.07
    assert run(card)[1] == []
    card["stance_calc"] |= {"price": 90.0, "valid_until": "2026-11-02"}  # expired before the close date
    assert run(card)[1] == []
    card["stance_calc"]["valid_until"] = "2026-11-03"  # valid on the close date, expired by the snapshot date
    assert run(card, day=date(2026, 11, 3))[1] and run(card)[1] == []


@pytest.mark.parametrize("change", [
    {"status": "待复核"}, {"verdict": "暂不值得继续研究"}, {"stance_calc": {"action": "回避"}},
])
def test_ineligible_cards_never_prompt_even_in_v2_support(card, change):
    assert card["current"]["valuation"]["v2"]["zone"] == "测算支持区"
    if change.get("status") == "待复核":
        card["pending_events"] = [{"title": "临时公告", "kind": "announcement"}]
    assert run(card | change)[1] == []


def test_holdings_are_not_opportunities_and_old_cards_still_read(card):
    research, prompts, _ = run(card, holdings=["688578"])
    assert prompts == [] and research["holdings"][0]["stance_action"] == "现在分批"
    old = deepcopy(card)
    del old["stance_calc"], old["current"]["valuation"]["v2"]
    research, prompts, _ = run(old)
    assert prompts == [] and research["candidates"][0]["expected_return"] is None


def _ineligible(r, p, **change):
    # Make the row ineligible while keeping its card projection consistent.
    row = r["candidates"][0]
    row.update(change)
    row["card"] = row["card"] | {"status": row["card_status"], "verdict": row["verdict"]}
    if row["card_status"] == "待复核":
        row["card"]["pending_events"] = [{"title": "临时公告", "kind": "announcement"}]


@pytest.mark.parametrize("mutate", [
    lambda r, p: p[0]["evidence"].update(expected_return=0.5),
    lambda r, p: p[0]["evidence"].update(report_id=99),
    lambda r, p: p[0]["evidence"].update(close=1.0),
    lambda r, p: p[0]["evidence"].update(close_date="2026-11-04"),
    lambda r, p: p[0]["evidence"].pop("stance_price"),
    lambda r, p: r["candidates"][0].update(stance_action="等价格"),
    lambda r, p: r["candidates"][0].pop("v2_zone"),
    lambda r, p: p[0].update(code="600900"),
    lambda r, p: _ineligible(r, p, card_status="待复核"),
    lambda r, p: _ineligible(r, p, verdict="需要谨慎"),
])
def test_contract_rejects_forged_opportunity(card, mutate):
    research, prompts, _ = monitor_research.summarize({"status": "ok", "cards": [card], "missing": []}, [], DAY)
    prompts = [p for p in prompts if p["kind"] == "research_opportunity"]
    mutate(research, prompts)
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract._validate_research_fields({"research": research, "review_prompts": prompts}, set())


def test_boundary_rejects_malformed_stance_calc(card):
    card["stance_calc"] = {"action": "持有"}
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_research_cards({"cards": [card], "missing": []})


def test_untradable_board_keeps_research_but_drops_advice(card):
    """0.1.36: the owner's account cannot trade STAR; 688578 stays researched, never advised."""
    payload = {"status": "ok", "cards": [card], "missing": []}
    research, prompts, _ = monitor_research.summarize(payload, [], DAY, ["STAR"])
    monitor_contract._validate_research_fields({"research": research, "review_prompts": prompts}, set(), ["STAR"])
    row = research["candidates"][0]
    assert row["code"] == "688578" and row["v2_zone"] == "测算支持区" and row["stance_action"] == "现在分批"
    assert not [p for p in prompts if p["kind"] == "research_opportunity"]
    assert "688578" in research["reason"] and "科创板" in research["reason"]
    # BSE-only restriction leaves the STAR candidate advisable.
    assert len(run(card)[1]) == 1
    assert [p["kind"] for p in monitor_research.summarize(payload, [], DAY, ["BSE"])[1]] == ["research_opportunity"]


def test_contract_rejects_opportunity_on_untradable_board(card):
    research, prompts, _ = monitor_research.summarize({"status": "ok", "cards": [card], "missing": []}, [], DAY)
    snapshot = {"research": research, "review_prompts": prompts}
    monitor_contract._validate_research_fields(snapshot, set(), ["BSE"])
    with pytest.raises(monitor_contract.MonitorContractError, match="cannot trade"):
        monitor_contract._validate_research_fields(snapshot, set(), ["STAR"])


def test_snapshot_assembly_threads_policy_restriction(card):
    """The monitor command passes the policy's untradable_boards into the research summary."""
    from a_share_research.policy.v0 import commands_monitor
    base = json.loads((FIXTURE.parents[1] / "monitor/agent-behavior-snapshots.json").read_text())["no_action"]
    base["as_of"] = "2026-11-04T15:00:00+08:00"
    base.pop("research", None), base.pop("review_prompts", None)
    result = {"status": "ok", "cards": [card], "missing": []}
    open_ = commands_monitor._with_research(deepcopy(base), deepcopy(result))
    assert [p["kind"] for p in open_["review_prompts"]] == ["research_opportunity"]
    base["account"]["risk_policy"] |= {"untradable_boards": ["STAR"]}
    base["account"]["risk_policy"]["field_sources"] |= {"untradable_boards": "policy_file"}
    closed = commands_monitor._with_research(base, result)
    monitor_contract.validate_monitor_snapshot(closed)
    assert closed["review_prompts"] == [] and closed["research"]["candidates"][0]["code"] == "688578"


REFERENCE = FIXTURE.with_name("reference-evidence-cards.json")


def reference_card(code):
    return next(c for c in json.loads(REFERENCE.read_text())["cards"] if c["code"] == code)


def new_checkpoint_card():
    # Explicit contract perturbation: real captured card plus Research's support-fixture stance.
    card = reference_card("002648")
    card["stance"] = json.loads(FIXTURE.with_name("research-add-trigger.json").read_text())["stance"]
    card["stance_calc"] = {"action": card["stance"]["action"],
                           "add_trigger": deepcopy(card["stance"]["add_trigger"])}
    return card


@pytest.mark.parametrize("code,value", [("300750", 1000), ("002946", 15.1)])
def test_legacy_evidence_wait_is_first_tranche_with_reference_checkpoint(code, value):
    card = reference_card(code)
    research, prompts, _ = run(card)
    row = research["candidates"][0]
    assert row["stance_action"] == "等证据"
    assert row["stance_add_trigger"] == card["stance_calc"]["trigger"]
    assert row["stance_add_trigger"]["value"] == value
    detail = prompts[0]["detail"]
    assert f"2026-12-31 经营现金流 ≥ {value:g} 亿元" in detail
    assert "参考，不阻止首批" in detail and "tranches[0]" in detail
    assert "更新的有效研究确认" in detail and "未达到则复核是否减仓" in detail
    # Remove the independent support-zone condition to exercise the relaxed freshness rule alone.
    card["current"]["valuation"]["v2"]["zone"] = "中性区"
    completed = date.fromisoformat(card["completed_at"][:10])
    from datetime import timedelta
    assert run(card, completed + timedelta(days=7))[1]
    assert not run(card, completed + timedelta(days=8))[1]
    assert not run(card, completed - timedelta(days=1))[1]


def test_new_checkpoint_projects_and_absence_does_not_invent_one():
    card = new_checkpoint_card()
    research, prompts, _ = run(card)
    assert research["candidates"][0]["stance_add_trigger"] == card["stance_calc"]["add_trigger"]
    assert "2026-03-31 经营现金流 ≥ 200 亿元" in prompts[0]["detail"]
    plain = reference_card("002648")
    research, prompts, _ = run(plain)
    assert research["candidates"][0]["stance_add_trigger"] is None
    assert "加仓检查点" not in prompts[0]["detail"]


def test_checkpoint_threshold_is_shown_unrounded():
    # Review P2: ':g' rounded 200.0001 to 200, so the shown threshold differed from the frozen one.
    card = new_checkpoint_card()
    for trigger in (card["stance"]["add_trigger"], card["stance_calc"]["add_trigger"]):
        trigger["value"] = 200.0001
    research, prompts, _ = run(card)
    assert research["candidates"][0]["stance_add_trigger"]["value"] == 200.0001
    assert "经营现金流 ≥ 200.0001 亿元" in prompts[0]["detail"]


@pytest.mark.parametrize("patch", [
    {"metric": "M.close"}, {"period": None}, {"period": "2026-13-31"},
    {"op": ">"}, {"value": True}, {"value": None}, {"value": float("inf")}, {"extra": 1},
])
def test_checkpoint_shape_rejects_mutations(patch):
    card = new_checkpoint_card()
    card["stance_calc"]["add_trigger"].update(patch)
    card["stance"]["add_trigger"] = deepcopy(card["stance_calc"]["add_trigger"])
    with pytest.raises(monitor_contract.MonitorContractError):
        run(card)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("location", ["frozen", "row", "evidence", "missing_evidence"])
def test_checkpoint_mismatch_rejected(legacy, location):
    card = reference_card("300750") if legacy else new_checkpoint_card()
    if location == "frozen":
        card["stance_calc"]["trigger" if legacy else "add_trigger"]["value"] += 1
        with pytest.raises(monitor_contract.MonitorContractError):
            run(card)
        return
    research, prompts, _ = run(card)
    if location == "row":
        research["candidates"][0]["stance_add_trigger"] = None
    elif location == "evidence":
        prompts[0]["evidence"]["stance_add_trigger"] = None
    else:
        del prompts[0]["evidence"]["stance_add_trigger"]
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract._validate_research_fields({"research": research, "review_prompts": prompts}, set())


def test_old_snapshot_without_additive_projection_remains_readable():
    research, prompts, _ = run(reference_card("002648"))
    del research["candidates"][0]["stance_add_trigger"], prompts[0]["evidence"]["stance_add_trigger"]
    monitor_contract._validate_research_fields({"research": research, "review_prompts": prompts}, set())


@pytest.mark.parametrize("legacy", [False, True])
def test_checkpoint_does_not_bypass_eligibility_or_board_restriction(legacy):
    card = reference_card("300750") if legacy else new_checkpoint_card()
    for field, value in (("status", "待复核"), ("verdict", "需要谨慎")):
        assert not run(card | {field: value})[1]
    assert not run(card, holdings=[card["code"]])[1]
    bad = deepcopy(card)
    bad["stance_calc"]["action"] = "回避"
    with pytest.raises(monitor_contract.MonitorContractError):
        run(bad)
    bad = deepcopy(card)
    source, dest = ("trigger", "add_trigger") if legacy else ("add_trigger", "trigger")
    bad["stance_calc"][dest] = bad["stance_calc"].pop(source)
    with pytest.raises(monitor_contract.MonitorContractError):
        run(bad)
    card["code"] = "688578"  # Board-only mutation; financial inputs remain captured values.
    payload = {"status": "ok", "cards": [card], "missing": []}
    assert not monitor_research.summarize(payload, [], DAY, ["STAR"])[1]
    assert monitor_research.summarize(payload, [], DAY, ["BSE"])[1]
