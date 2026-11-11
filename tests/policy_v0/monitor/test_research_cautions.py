"""Cross-service warning contracts from actual Research projections, without live research.

Combining a captured open question with another report, or changing a flag/date/status,
is an explicit contract perturbation, never claimed as an observed production report.
"""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from a_share_research.policy.v0 import commands_monitor, monitor_contract, monitor_research

DATA = Path(__file__).resolve().parents[1] / "fixtures/monitor"
CAPTURED = json.loads((DATA / "research-cautions-20261105.json").read_text())
BASE = json.loads((DATA / "agent-behavior-snapshots.json").read_text())["no_action"]


def assemble(card):
    payload = {"cards": [card], "missing": []}
    monitor_contract.validate_research_cards(payload)
    base = deepcopy(BASE)
    base["as_of"] = "2026-11-05T19:00:00+08:00"
    return commands_monitor._with_research(base, {"status": "ok", **payload})


def opportunity(snapshot):
    return next(
        p for p in snapshot["review_prompts"] if p["kind"] == "research_opportunity"
    )


def positive_card():
    return deepcopy(CAPTURED["valuation_cases"][1]["card"])


def with_questions(index=1):
    card = positive_card()
    card["open_questions"] = deepcopy(
        CAPTURED["question_cases"][index]["card"]["open_questions"]
    )
    return card


@pytest.mark.parametrize(
    "case", CAPTURED["valuation_cases"], ids=lambda c: str(c["source_id"])
)
def test_captured_boundary_survives_full_snapshot_and_opportunity(case):
    card = deepcopy(case["card"])
    snapshot = assemble(card)
    row = snapshot["research"]["candidates"][0]
    assert row["card"] == card
    expected = case["source_id"] in {1, 2, 3, 5}
    prompts = [
        p for p in snapshot["review_prompts"] if p["kind"] == "research_opportunity"
    ]
    assert bool(prompts) is expected
    if expected:
        prompt = prompts[0]
        assert prompt["evidence"]["report_boundary"] is True
        assert prompt["evidence"]["current_boundary"] is True
        assert prompt["evidence"]["open_questions"] == []
        assert "报告形成时为边界立场" in prompt["detail"]
        assert "当前价格处于边界" in prompt["detail"]
        assert "同样材料重跑可能得出相反立场" in prompt["detail"]
    if case["source_id"] == 2:
        assert "12.17%" in prompts[0]["detail"] and "首批" in prompts[0]["detail"]


@pytest.mark.parametrize("index", range(3))
def test_real_open_questions_and_agenda_location_are_not_lost_in_opportunity(index):
    card = with_questions(index)
    original = deepcopy(card)
    snapshot = assemble(card)
    prompt = opportunity(snapshot)
    assert card == original
    assert (
        snapshot["research"]["candidates"][0]["card"]["open_questions"]
        == card["open_questions"]
    )
    assert prompt["evidence"]["open_questions"] == card["open_questions"]
    assert "已通过复核，但以下问题尚未答完" in prompt["detail"]
    for q in card["open_questions"]:
        assert (
            q["id"] in prompt["detail"]
            and q["where"] in prompt["detail"]
            and q["problem"] in prompt["detail"]
        )
    assert "不构成已形成的交易动作" in prompt["detail"]


@pytest.mark.parametrize(
    "field", ["open_questions", "report_boundary", "current_boundary"]
)
@pytest.mark.parametrize("mutation", ["remove", "change", "type"])
def test_new_card_requires_exact_cautions_in_opportunity_evidence(field, mutation):
    snapshot = assemble(with_questions())
    evidence = opportunity(snapshot)["evidence"]
    if mutation == "remove":
        del evidence[field]
    elif field == "open_questions":
        if mutation == "type":
            evidence[field] = "not a list"
        else:
            evidence[field][0]["problem"] = "changed"
    else:
        evidence[field] = 1 if mutation == "type" else False
    with pytest.raises(monitor_contract.MonitorContractError, match="research"):
        monitor_contract.validate_monitor_snapshot(snapshot)


@pytest.mark.parametrize("field", ["open_questions", "boundary"])
def test_dropping_warning_from_snapshot_card_cannot_silently_keep_the_old_evidence(
    field,
):
    snapshot = assemble(with_questions())
    card = snapshot["research"]["candidates"][0]["card"]
    if field == "boundary":
        del card["valuation_calc"][field]
    else:
        del card[field]
    with pytest.raises(monitor_contract.MonitorContractError, match="caution"):
        monitor_contract.validate_monitor_snapshot(snapshot)


@pytest.mark.parametrize(
    "report,current", [(True, False), (False, True), (False, False), (None, None)]
)
def test_report_and_current_flags_stay_distinct_and_unknown_is_not_false(
    report, current
):
    card = positive_card()
    # Flags only are perturbed to verify faithful consumption; Skills does not recompute 5%.
    card["valuation_calc"]["boundary"] = report
    card["current"]["valuation"]["boundary"] = current
    prompt = opportunity(assemble(card))
    assert prompt["evidence"]["report_boundary"] is report
    assert prompt["evidence"]["current_boundary"] is current
    assert ("报告形成时为边界立场" in prompt["detail"]) is (report is True)
    assert ("当前价格处于边界" in prompt["detail"]) is (current is True)
    if report is None:
        assert "不可判定" in prompt["detail"]
    if report is False and current is False:
        assert "Research 风险提示" not in prompt["detail"]
    assert "以下问题尚未答完" not in prompt["detail"]


def test_legacy_card_and_snapshot_remain_readable_without_inventing_negative_flags():
    card = positive_card()
    del (
        card["open_questions"],
        card["valuation_calc"]["boundary"],
        card["current"]["valuation"]["boundary"],
    )
    snapshot = assemble(card)
    evidence = opportunity(snapshot)["evidence"]
    assert (
        not {"open_questions", "report_boundary", "current_boundary"} & evidence.keys()
    )
    assert (
        "未提供或不可判定，不代表判断稳定或问题已完整回答"
        in opportunity(snapshot)["detail"]
    )
    monitor_contract.validate_monitor_snapshot(snapshot)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "questions",
        {},
        [{"id": "x", "where": "valuation"}],
        [{"id": True, "where": "valuation", "problem": "gap"}],
    ],
)
def test_malformed_question_array_is_rejected_at_http_boundary(value):
    card = positive_card()
    card["open_questions"] = value
    with pytest.raises(monitor_contract.MonitorContractError):
        assemble(card)


@pytest.mark.parametrize("where", ["report", "current"])
@pytest.mark.parametrize("value", [0, 1, "true", [], {}])
def test_malformed_boundary_flag_is_rejected_at_http_boundary(where, value):
    card = positive_card()
    view = card["valuation_calc"] if where == "report" else card["current"]["valuation"]
    view["boundary"] = value
    with pytest.raises(monitor_contract.MonitorContractError):
        assemble(card)


def test_new_warnings_change_neither_opportunity_eligibility_nor_account_gates():
    new = with_questions()
    old = deepcopy(new)
    del (
        old["open_questions"],
        old["valuation_calc"]["boundary"],
        old["current"]["valuation"]["boundary"],
    )
    before, after = assemble(old), assemble(new)
    for key in set(before) - {"research", "review_prompts"}:
        assert before[key] == after[key], key
    assert [p["kind"] for p in before["review_prompts"]] == [
        p["kind"] for p in after["review_prompts"]
    ]
    assert "tranches[0]" in opportunity(after)["detail"]
    for change in ({"verdict": "需要谨慎"}, {"status": "待复核"}):
        result = assemble(new | change)
        assert not any(
            p["kind"] == "research_opportunity" for p in result["review_prompts"]
        )
    _, prompts, _ = monitor_research.summarize(
        {"status": "ok", "cards": [new], "missing": []},
        [new["code"]],
        date(2026, 11, 5),
    )
    assert not any(p["kind"] == "research_opportunity" for p in prompts)


def switch_result():
    """Real older holdings plus a captured new candidate; only scope/question combinations are controlled."""
    holding_codes = {"600900", "000963", "002050", "603606"}
    payload = json.loads((DATA / "opportunity-cards-20261102.json").read_text())[
        "response"
    ]
    payload["cards"] = [c for c in payload["cards"] if c["code"] in holding_codes]
    candidate = deepcopy(CAPTURED["valuation_cases"][0]["card"])
    candidate["open_questions"] = deepcopy(
        CAPTURED["question_cases"][0]["card"]["open_questions"]
    )
    payload["cards"][0]["open_questions"] = deepcopy(
        CAPTURED["question_cases"][1]["card"]["open_questions"]
    )
    payload["cards"].append(candidate)
    monitor_contract.validate_research_cards(payload)
    research, prompts, _ = monitor_research.summarize(
        {"status": "ok", **payload}, sorted(holding_codes), date(2026, 11, 5)
    )
    result = {"research": research, "review_prompts": prompts}
    monitor_contract._validate_research_fields(result, holding_codes)
    return result, holding_codes


def test_switch_and_stretched_prompts_preserve_both_sides_cautions_without_new_kinds():
    result, _ = switch_result()
    switch = next(
        p for p in result["review_prompts"] if p["kind"] == "opportunity_cost"
    )
    assert len(switch["evidence"]["comparisons"]) == 2
    assert (
        "601899 报告 #1" in switch["detail"]
        and "报告形成时为边界立场" in switch["detail"]
    )
    questions = CAPTURED["question_cases"][0]["card"]["open_questions"]
    for comparison in switch["evidence"]["comparisons"]:
        assert comparison["candidate"]["report_boundary"] is True
        assert comparison["candidate"]["open_questions"] == questions
    holding_questions = CAPTURED["question_cases"][1]["card"]["open_questions"]
    for question in questions + holding_questions:
        assert question["problem"] in switch["detail"]
    stretched = next(
        p
        for p in result["review_prompts"]
        if p["kind"] == "valuation_stretched" and p["code"] == "600900"
    )
    assert stretched["evidence"]["open_questions"] == holding_questions
    assert all(q["problem"] in stretched["detail"] for q in holding_questions)


@pytest.mark.parametrize(
    "side,field",
    [
        ("holding", "open_questions"),
        ("candidate", "open_questions"),
        ("candidate", "report_boundary"),
        ("candidate", "current_boundary"),
    ],
)
def test_switch_comparison_cannot_drop_source_warnings(side, field):
    result, holdings = switch_result()
    comparison = next(
        c
        for c in result["research"]["opportunity_costs"]
        if c["holding"]["code"] == "600900"
    )
    del comparison[side][field]
    with pytest.raises(monitor_contract.MonitorContractError, match="caution"):
        monitor_contract._validate_research_fields(result, holdings)
