"""Offline cards derive from the real eval report bodies; see fixture provenance.

Account positions and boundary/event/scope perturbations below are synthetic test
inputs, not observed holdings, market quotes, Research verdicts or forecasts.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from a_share_research.policy.v0 import (
    cache,
    commands_monitor,
    monitor_contract,
    monitor_research,
    research_client,
)
from a_share_research.policy.v0.trading_costs import COMMISSION_RATE, STAMP_TAX_RATE
from policy_v0.helpers import mock_price_quotes
from policy_v0.monitor.test_monitor_snapshot import _quotes, _seed_holding

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/monitor/research-cards.json"
UNCHANGED = (
    "data_status",
    "valuation_status",
    "action_status",
    "data_gaps",
    "stop_reason",
    "requires_user_confirmation",
    "account",
    "holdings",
)


@pytest.fixture
def cards():
    response = json.loads(FIXTURE.read_text())["response"]
    # Synthetic zero-dividend sensitivity endpoints for contract/threshold tests;
    # the captured API fixture predates this field. These are not observed estimates.
    for item in response["cards"]:
        if value := item["current"]["valuation"]:
            value["neutral_return_without_dividends"] = value["annual_returns"]["中性"]
    return response


def card(cards, code):
    return next(item for item in cards["cards"] if item["code"] == code)


@pytest.fixture
def http(monkeypatch, cards):
    """Stand-in for the research read: the payloads are the captured service responses."""
    calls = []
    state = {"status": 200, "body": cards}

    def read(path, params):
        calls.append(("GET", path, {"params": params}))
        if "exception" in state:
            raise state["exception"]
        if state["status"] != 200:
            raise ValueError(f"Research {state['status']}")
        return json.loads(json.dumps(state["body"], ensure_ascii=False))

    monkeypatch.setattr(research_client, "_read", read)
    return state, calls


@pytest.fixture
def run_snapshot(isolated_cache_database, monkeypatch, capsys, cards, http):
    monkeypatch.setattr(
        commands_monitor.domain,
        "cst_now",
        lambda: datetime.fromisoformat("2026-11-02T19:00:00+08:00"),
    )
    # Quote fixtures date themselves with cst_today; pin it to the same session.
    monkeypatch.setattr(commands_monitor.domain, "cst_today", lambda: "2026-11-02")
    _seed_holding(isolated_cache_database, code="600900")
    close = card(cards, "600900")["current"]["last_close"]
    # A hypothetical clean account at the captured close; no invented market price.
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute(
            "UPDATE holdings SET cost_price=?, reference_cost=?, stop_loss_15=?, stop_loss_20=?",
            (close, close, close * 0.85, close * 0.8),
        )
    mock_price_quotes(monkeypatch, lambda codes: _quotes(codes, price=close))

    def run(*args):
        status = cache.main(
            ["monitor-snapshot", "--portfolio-value", "100000", "--json", *args]
        )
        captured = capsys.readouterr()
        return status, json.loads(captured.out)

    return run


def prompts(payload, kind):
    return [item for item in payload["review_prompts"] if item["kind"] == kind]


def assert_gate_unchanged(baseline, actual, *, thesis_review=False):
    for field in UNCHANGED:
        if thesis_review and field in {"action_status", "stop_reason"}:
            continue
        assert actual[field] == baseline[field], field
    if thesis_review:
        # A thesis review ends the clean fast path exactly like other escalations.
        assert (
            actual["stop_reason"] is None and actual["manifest"]["stop_reason"] is None
        )
        assert actual["action_status"] in {"review_candidate", "trade_candidate"}
        return
    assert actual["manifest"]["stop_reason"] == baseline["manifest"]["stop_reason"]
    if not thesis_review:
        assert actual["review_status"] == baseline["review_status"]
        assert actual["escalations"] == baseline["escalations"]


def test_real_cards_one_call_projection_and_prompts_do_not_enter_escalations(
    run_snapshot, cards, http
):
    assert STAMP_TAX_RATE == 0.0005 and COMMISSION_RATE == 0.00025
    _, baseline = run_snapshot("--no-research")
    result, actual = run_snapshot()
    assert result == 0 and actual["schema_version"] == 1
    assert actual["stop_reason"] == "clean_fast_gate"
    assert_gate_unchanged(baseline, actual)
    assert actual["escalations"] == []
    stretched = prompts(actual, "valuation_stretched")
    assert len(stretched) == 1 and "29.74" in stretched[0]["detail"]
    row = actual["research"]["holdings"][0]
    assert row["card"] == card(cards, "600900")
    assert (
        row["neutral_return"]
        == row["card"]["current"]["valuation"]["annual_returns"]["中性"]
    )
    assert row["conservative_return"] < 0  # Negative returns are valid evidence.
    opportunity = prompts(actual, "opportunity_cost")
    assert len(opportunity) == 1
    evidence = opportunity[0]["evidence"]["comparisons"][0]
    assert evidence["candidate"]["code"] == "600309"  # Better than the other valid L5.
    assert (
        evidence["holding"]["report_id"] == 3
        and evidence["candidate"]["report_id"] == 10
    )
    assert (
        evidence["holding"]["close_date"]
        == evidence["candidate"]["close_date"]
        == "2026-10-30"
    )
    horizon = card(cards, "600309")["current"]["valuation"]["horizon"]
    assert evidence["annual_switch_cost"] == pytest.approx(0.001 / horizon)
    assert http[1] == [("GET", "/api/cards", {"params": {}})]


def test_pending_card_escalates_only_review_and_keeps_returns(run_snapshot, cards):
    _, baseline = run_snapshot("--no-research")
    holding = card(cards, "600900")
    holding.update(
        status="待复核",
        pending_events=[{"title": "测试待复核公告", "kind": "major_risk"}],
    )
    holding["current"]["note"] = "待复核，区间暂停解读"
    _, actual = run_snapshot()
    assert_gate_unchanged(baseline, actual, thesis_review=True)
    assert actual["review_status"] == "review_required"
    assert actual["review_prompts"] == []
    assert len(actual["escalations"]) == 1
    escalation = actual["escalations"][0]
    assert (
        escalation["reason_code"] == "research_thesis_review"
        and escalation["candidate"] == "review"
    )
    assert "测试待复核公告（major_risk）" in escalation["detail"]
    row = actual["research"]["holdings"][0]
    assert row["zone"] == "测算透支区" and row["neutral_return"] is not None
    assert row["note"] == holding["current"]["note"]
    order = commands_monitor._ESCALATION_REASON_ORDER
    assert (
        order.index("l3_watch")
        < order.index("research_thesis_review")
        < order.index("governance_gate")
    )


@pytest.mark.parametrize(
    "candidate_return,expected",
    [
        (0.0505, True),  # Synthetic exact threshold: .0505 - .02 - .001/2 = .03.
        (0.0495, False),  # 0.1pp below the net threshold.
        (0.05025, False),  # Gross passes, net fails: detects omitted transaction costs.
        (0.045, False),  # Detects a mutated 2pp threshold.
    ],
)
def test_net_opportunity_threshold(run_snapshot, cards, candidate_return, expected):
    # Only these boundary return/horizon inputs are synthetic, not market data.
    held, candidate = card(cards, "600900"), card(cards, "600309")
    cards["cards"][:] = [held, candidate]
    held["current"]["valuation"]["annual_returns"]["中性"] = 0.02
    candidate["current"]["valuation"]["annual_returns"]["中性"] = candidate_return
    held["current"]["valuation"]["neutral_return_without_dividends"] = 0.02
    candidate["current"]["valuation"]["neutral_return_without_dividends"] = (
        candidate_return
    )
    candidate["current"]["valuation"]["horizon"] = 2
    _, payload = run_snapshot()
    assert bool(prompts(payload, "opportunity_cost")) is expected


@pytest.mark.parametrize(
    "change",
    [
        "verdict",
        "pending",
        "external",
        "null_return",
        "null_valuation",
        "no_horizon",
        "zero_horizon",
        "negative_horizon",
        "missing_date",
    ],
)
def test_ineligible_candidate_never_prompts(run_snapshot, cards, change):
    held, candidate = card(cards, "600900"), card(cards, "600309")
    cards["cards"][:] = [held, candidate]
    valuation = candidate["current"]["valuation"]
    if change == "verdict":
        candidate["verdict"] = "需要谨慎"
    elif change == "pending":
        candidate["status"] = "待复核"
    elif change == "external":
        candidate["origin"] = "external"
    elif change == "null_return":
        valuation["annual_returns"]["中性"] = None
    elif change == "null_valuation":
        candidate["current"]["valuation"] = None
    elif change == "missing_date":
        candidate["current"]["last_close_date"] = None
    else:
        valuation["horizon"] = {
            "no_horizon": None,
            "zero_horizon": 0,
            "negative_horizon": -1,
        }[change]
    _, payload = run_snapshot()
    assert payload["research"]["status"] == "ok"
    assert not prompts(payload, "opportunity_cost") and payload["escalations"] == []


@pytest.mark.parametrize("days,expected", [(3, True), (4, False), (-4, False)])
def test_close_date_comparison(run_snapshot, cards, days, expected):
    held, candidate = card(cards, "600900"), card(cards, "600309")
    cards["cards"][:] = [held, candidate]
    candidate["current"]["last_close_date"] = (
        date.fromisoformat(held["current"]["last_close_date"]) + timedelta(days=days)
    ).isoformat()
    _, payload = run_snapshot()
    assert bool(prompts(payload, "opportunity_cost")) is expected
    if not expected:
        assert "超过3日" in payload["research"]["reason"]


@pytest.mark.parametrize(
    "case,status",
    [
        ("unconfigured", "not_configured"),
        ("skip", "skipped"),
        ("timeout", "unavailable"),
        ("version", "unavailable"),
        ("http", "unavailable"),
        ("shape", "unavailable"),
        ("nan", "unavailable"),
    ],
)
def test_optional_failures_preserve_all_gates(
    run_snapshot, http, monkeypatch, case, status
):
    _, baseline = run_snapshot("--no-research")
    state, calls = http
    if case == "unconfigured":
        state["exception"] = research_client.ResearchNotConfigured("研究库不存在")
    elif case == "timeout":
        state["exception"] = sqlite3.OperationalError("database is locked")
    elif case == "version":
        state["body"] = {"version": "research-v2"}
    elif case == "http":
        state.update(
            status=503,
            body={"version": "research-v1", "error": "secret-test-token\n不可用"},
        )
    elif case == "shape":
        state["body"] = {"version": "research-v1", "cards": []}
    elif case == "nan":
        state["body"]["cards"][0]["current"]["last_close"] = float("nan")
    result, payload = run_snapshot(*(["--no-research"] if case == "skip" else []))
    assert result == 0 and payload["research"]["status"] == status
    assert_gate_unchanged(baseline, payload)
    assert len(calls) == (0 if case == "skip" else 1)
    assert "secret-test-token" not in json.dumps(payload, ensure_ascii=False)
    assert bool(prompts(payload, "research_unavailable")) is (status == "unavailable")


@pytest.mark.parametrize(
    "stance", ["现在分批", ["现在分批"], 1, True, "", [], 0, False]
)
@pytest.mark.parametrize("with_frozen", [False, True])
def test_nonobject_stance_is_a_contract_error(cards, stance, with_frozen):
    held = card(cards, "600900")
    held["stance"] = stance
    if with_frozen:
        held["stance_calc"] = {"action": "现在分批"}
    else:
        held.pop("stance_calc", None)
    with pytest.raises(
        monitor_contract.MonitorContractError,
        match="research.card.stance must be an object",
    ):
        monitor_contract.validate_research_cards(cards)


@pytest.mark.parametrize("stance", [None, {}, {"action": "现在分批"}])
def test_optional_object_stance_remains_compatible_and_unchanged(cards, stance):
    held = card(cards, "600900")
    held["stance_calc"] = {"action": "现在分批"}
    held["stance"] = stance
    before = deepcopy(cards)
    monitor_contract.validate_research_cards(cards)
    assert cards == before
    del held["stance"]
    monitor_contract.validate_research_cards(cards)


@pytest.mark.parametrize(
    "stance", ["现在分批", ["现在分批"], 1, True, "", [], 0, False]
)
def test_malformed_stance_degrades_research_without_changing_monitor_gates(
    run_snapshot, cards, http, stance
):
    _, baseline = run_snapshot("--no-research")
    held = card(cards, "600900")
    held["stance_calc"] = {"action": "现在分批"}
    held["stance"] = stance
    result, payload = run_snapshot()
    assert result == 0 and payload["research"]["status"] == "unavailable"
    assert payload["research"]["reason"] == "research.card.stance must be an object"
    assert_gate_unchanged(baseline, payload)
    assert len(http[1]) == 1 and len(prompts(payload, "research_unavailable")) == 1


def test_malformed_stance_does_not_hide_a_price_stop(run_snapshot, cards, monkeypatch):
    close = card(cards, "600900")["current"]["last_close"]
    mock_price_quotes(monkeypatch, lambda codes: _quotes(codes, price=close * 0.7))
    baseline_status, baseline = run_snapshot("--no-research")
    assert any(
        item["reason_code"] == "price_stop_2" for item in baseline["escalations"]
    )
    held = card(cards, "600900")
    held["stance_calc"] = {"action": "现在分批"}
    held["stance"] = "现在分批"
    result, payload = run_snapshot()
    assert result == baseline_status and payload["research"]["status"] == "unavailable"
    assert_gate_unchanged(baseline, payload)


def test_missing_cards_and_l5_scope(run_snapshot, cards):
    held = card(cards, "600900")
    held["origin"] = "l5"  # In-scope holding must not also become a candidate.
    _, payload = run_snapshot()
    assert "600900" not in {r["code"] for r in payload["research"]["candidates"]}
    cards["cards"][:] = [
        c for c in cards["cards"] if c["code"] not in {"600900", "600309"}
    ]
    cards["missing"] = [
        {"code": "600900", "origin": "external"},
        {"code": "600309", "origin": "l5"},
    ]
    _, payload = run_snapshot()
    assert payload["research"]["holdings"][0]["card"] is None
    assert payload["research"]["missing"] == ["600900", "600309"]
    assert (
        next(r for r in payload["research"]["candidates"] if r["code"] == "600309")[
            "card"
        ]
        is None
    )
    assert not payload["review_prompts"]


@pytest.mark.parametrize(
    "mutation",
    [
        "status",
        "kind",
        "return",
        "card_status",
        "scope",
        "missing_evidence",
        "thesis_trade",
    ],
)
def test_additive_contract_rejects_invalid_research(run_snapshot, cards, mutation):
    if mutation == "thesis_trade":
        card(cards, "600900")["status"] = "待复核"
    _, payload = run_snapshot()
    value = deepcopy(payload)
    if mutation == "status":
        value["research"]["status"] = "bad"
    elif mutation == "kind":
        value["review_prompts"][0]["kind"] = "sell"
    elif mutation == "return":
        value["research"]["holdings"][0]["neutral_return"] = True
    elif mutation == "card_status":
        value["research"]["holdings"][0]["card_status"] = "invalid"
    elif mutation == "scope":
        value["research"]["holdings"] = []
    elif mutation == "missing_evidence":
        del value["review_prompts"][0]["evidence"]
    else:
        value["escalations"][0]["candidate"] = "trade"
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(value)


def test_pending_research_does_not_unblock_existing_monitor_failure(
    run_snapshot, cards, monkeypatch
):
    mock_price_quotes(monkeypatch, lambda codes: {})
    _, baseline = run_snapshot("--no-research")
    card(cards, "600900")["status"] = "待复核"
    result, actual = run_snapshot()
    assert result == 1 and actual["review_status"] == "blocked"
    assert_gate_unchanged(baseline, actual, thesis_review=True)
    assert any(
        e["reason_code"] == "research_thesis_review" for e in actual["escalations"]
    )


def test_unavailable_database_still_reports_optional_research(http, capsys):
    assert cache.main(["monitor-snapshot", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["data_status"] == "unavailable"
    assert payload["research"]["status"] == "ok" and len(http[1]) == 1
    assert payload["research"]["holdings"] == []


def test_opportunity_freshness_uses_snapshot_as_of(run_snapshot, monkeypatch):
    # Advance the snapshot clock only; original report timestamps stay unchanged.
    monkeypatch.setattr(
        commands_monitor.domain,
        "cst_now",
        lambda: datetime.fromisoformat("2026-11-10T19:00:00+08:00"),
    )
    _, payload = run_snapshot()
    assert payload["as_of"].startswith("2026-11-10")
    assert len(payload["research"]["opportunity_costs"]) == 1
    assert not prompts(payload, "opportunity_cost")


def test_cash_uncertainty_can_remove_advantage_without_removing_card_returns(cards):
    held, candidate = card(cards, "600900"), card(cards, "600309")
    result = {"status": "ok", "cards": [held, candidate], "missing": []}
    # Boundary mutation: nominal candidate beats holding, but its ex-dividend endpoint does not.
    candidate["current"]["valuation"]["neutral_return_without_dividends"] = held[
        "current"
    ]["valuation"]["annual_returns"]["中性"]
    research, notices, escalations = monitor_research.summarize(
        result, ["600900"], date(2026, 11, 2)
    )
    assert not research["opportunity_costs"] and "现金流未核对" in research["reason"]
    assert (
        not [n for n in notices if n["kind"] == "opportunity_cost"] and not escalations
    )
    assert (
        research["candidates"][0]["neutral_return"]
        == candidate["current"]["valuation"]["annual_returns"]["中性"]
    )
    del candidate["current"]["valuation"]["neutral_return_without_dividends"]
    research, _, _ = monitor_research.summarize(result, ["600900"], date(2026, 11, 2))
    assert not research["opportunity_costs"] and "缺少适用" in research["reason"]


def test_captured_maotai_changjiang_sensitivity_reverses_nominal_advantage(cards):
    held, candidate = card(cards, "600900"), card(cards, "600519")
    # Hypothetical scope/verdict only, not an actual account recommendation.
    candidate.update(origin="l5", verdict="值得继续研究")
    # Recomputed by Research valuation.at from valuation-stored-20261101.json,
    # matching these captured reports: exit_cap/current_cap ** (1/horizon) - 1.
    held["current"]["valuation"][
        "neutral_return_without_dividends"
    ] = -0.004238281527728428
    candidate["current"]["valuation"]["neutral_return_without_dividends"] = (
        0.0335926372960775
    )
    nominal = (
        candidate["current"]["valuation"]["annual_returns"]["中性"]
        - held["current"]["valuation"]["annual_returns"]["中性"]
        - 0.001 / candidate["current"]["valuation"]["horizon"]
    )
    assert nominal > 0.03
    result = {"status": "ok", "cards": [held, candidate], "missing": []}
    research, notices, _ = monitor_research.summarize(
        result, ["600900"], date(2026, 11, 2)
    )
    assert not research["opportunity_costs"]
    assert "现金流未核对" in research["reason"]
    robust = (
        0.0335926372960775
        - held["current"]["valuation"]["annual_returns"]["中性"]
        - 0.001 / candidate["current"]["valuation"]["horizon"]
    )
    # Near-miss reason carries both bases (about 3.92pp nominal vs 1.20pp robust).
    assert (
        f"计分红 {nominal * 100:.2f} 个百分点，候选不计累计分红 {robust * 100:.2f} 个百分点"
        in research["reason"]
    )
    assert not [n for n in notices if n["kind"] == "opportunity_cost"]
