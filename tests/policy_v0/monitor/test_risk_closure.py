"""S1 current-contract fixture acceptance, not live-client qualification."""

import json
import socket
import sqlite3
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from a_share_research.policy.v0 import (
    cache,
    commands_monitor,
    domain,
    market_quotes,
    monitor_contract,
    risk_budget,
)
from policy_v0.helpers import mock_price_quotes
from support.policy_replay import _quotes, _run, _seed_holding

ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide"


def _inputs(database, *, count=1, shares=100):
    for index in range(count):
        _seed_holding(database, f"600{index:03d}", shares=shares)
    local = commands_monitor._load_monitor_local_snapshot()
    return local, _quotes([item["code"] for item in local["holdings"]])


def _snapshot(database, *, count=1, shares=100):
    local, quotes = _inputs(database, count=count, shares=shares)
    return commands_monitor.build_monitor_snapshot(local, quotes, 100000)


@pytest.mark.parametrize(
    "count,shares,status,codes",
    [
        (1, 101, "over_budget", {"600000"}),  # A01
        (5, 100, "over_budget", {None}),  # A02
        (4, 100, "within_budget", set()),  # A03: exactly 2% per name, 8% total
        (1, 99, "within_budget", set()),  # A04
    ],
)
def test_budget_controls_review(count, shares, status, codes, isolated_cache_database):
    payload = _snapshot(isolated_cache_database, count=count, shares=shares)
    assert payload["account"]["risk_budget_status"] == status
    assert {item["code"] for item in payload["escalations"]} == codes
    assert payload["review_status"] == ("review_required" if codes else "cleared")
    assert payload["action_status"] == ("review_candidate" if codes else "no_action")
    assert payload["stop_reason"] == (None if codes else "clean_fast_gate")
    assert payload["requires_user_confirmation"] is False
    assert payload["manifest"]["writes"] is False
    assert payload["account"]["risk_policy"] == risk_budget.policy()
    for item in payload["escalations"]:
        assert item["reason_code"] == "risk_budget_exceeded"
        for field in (
            "amount=",
            "ratio=",
            "limit=",
            "denominator=",
            "denominator_source=cli",
            "scope=",
        ):
            assert field in item["detail"]


def test_budget_does_not_compare_rounded_percentages(isolated_cache_database):
    local, quotes = _inputs(isolated_cache_database)
    local["holdings"][0]["stop_loss_20"] = 80 - 0.000001
    payload = commands_monitor.build_monitor_snapshot(local, quotes, 100000)
    assert f"{payload['holdings'][0]['stop_risk_pct']:.2f}" == "2.00"
    assert payload["account"]["risk_budget_status"] == "over_budget"


@pytest.mark.parametrize("field,bad", [
    ("shares", True), ("stop_loss_20", None), ("price", float("nan")),
    ("shares", 0), ("stop_loss_20", "100"), ("price", -1),
])
def test_unknown_inputs_never_mean_zero_risk(field, bad, isolated_cache_database):
    local, quotes = _inputs(isolated_cache_database)
    if field == "price":
        quotes["600000"] = market_quotes.PriceQuote(
            bad, domain.cst_today(), "10:00:00", "fixture"
        )
    else:
        local["holdings"][0][field] = bad
    payload = commands_monitor.build_monitor_snapshot(local, quotes, 100000)
    assert payload["account"]["total_stop_risk"] is None
    assert payload["account"]["total_stop_risk_pct"] is None
    assert payload["account"]["risk_budget_status"] is None
    assert payload["stop_reason"] is None
    assert payload["action_status"] == "review_candidate"


@pytest.mark.parametrize("bad", [None, 0, True, float("inf")])
def test_invalid_denominator_does_not_clear_even_empty(bad, isolated_cache_database):
    for local in (
        {"holdings": [], "closed_count": 0},
        _inputs(isolated_cache_database)[0],
    ):
        payload = commands_monitor.build_monitor_snapshot(
            local, _quotes(["600000"]), bad
        )
        assert payload["account"]["portfolio_value"] is None
        assert payload["account"]["risk_budget_status"] is None
        assert payload["stop_reason"] is None
        assert payload["review_status"] != "cleared"


def test_valid_empty_account_keeps_original_requirements():
    payload = commands_monitor.build_monitor_snapshot(
        {"holdings": [], "closed_count": 0}, {}, 100000
    )
    assert payload["stop_reason"] == "clean_fast_gate"
    assert payload["account"]["total_stop_risk"] == 0
    assert (
        commands_monitor.unavailable_monitor_snapshot(100000)["review_status"]
        == "blocked"
    )


@pytest.mark.parametrize(
    "state",
    ["missing_stop", "missing_quote", "stale", "conflicted", "invalid_timestamp"],
)
def test_known_breach_survives_other_unknown_risk(state, isolated_cache_database):
    local, quotes = _inputs(isolated_cache_database, count=2, shares=101)
    if state == "missing_stop":
        local["holdings"][1]["stop_loss_20"] = None
    elif state == "missing_quote":
        quotes["600001"] = None
    else:
        quotes["600001"] = market_quotes.PriceQuote(
            100,
            "2000-01-01" if state == "stale" else domain.cst_today(),
            "25:00:00" if state == "invalid_timestamp" else "10:00:00",
            "fixture",
            conflicted=state == "conflicted",
        )
    payload = commands_monitor.build_monitor_snapshot(local, quotes, 100000)
    assert payload["account"]["risk_budget_status"] == "over_budget"
    assert payload["account"]["known_stop_risk_lower_bound"] == 2020
    assert payload["account"]["total_stop_risk"] is None
    assert payload["account"]["total_stop_risk_pct"] is None
    assert {
        item["code"]
        for item in payload["escalations"]
        if item["reason_code"] == "risk_budget_exceeded"
    } == {"600000"}
    assert payload["review_status"] == (
        "blocked" if state in {"stale", "conflicted"} else "review_required"
    )


def test_known_portfolio_lower_bound_can_already_exceed_limit(isolated_cache_database):
    local, quotes = _inputs(isolated_cache_database, count=6)
    quotes["600005"] = None
    payload = commands_monitor.build_monitor_snapshot(local, quotes, 100000)
    assert payload["account"]["total_stop_risk"] is None
    assert payload["account"]["known_stop_risk_lower_bound"] == 10000
    assert payload["account"]["risk_budget_status"] == "over_budget"
    assert [
        item["code"]
        for item in payload["escalations"]
        if item["reason_code"] == "risk_budget_exceeded"
    ] == [None]


@pytest.mark.parametrize("action", ["reduce", "exit"])
def test_budget_does_not_downgrade_legal_trade_candidate(
    action, isolated_cache_database
):
    local, quotes = _inputs(isolated_cache_database, shares=101)
    local["holdings"][0]["l3_conditions"][0].update(status="triggered", action=action)
    payload = commands_monitor.build_monitor_snapshot(local, quotes, 100000)
    assert payload["account"]["risk_budget_status"] == "over_budget"
    assert payload["action_status"] == "trade_candidate"
    assert payload["requires_user_confirmation"] is True
    assert payload["manifest"]["writes"] is False


def test_duplicate_holdings_remain_visible_but_not_known_risk(isolated_cache_database):
    local, quotes = _inputs(isolated_cache_database, shares=101)
    duplicate = deepcopy(local["holdings"][0])
    duplicate["id"] += 1
    local["holdings"].append(duplicate)
    payload = commands_monitor.build_monitor_snapshot(local, quotes, 100000)
    assert payload["data_status"] == "conflicted"
    assert len(payload["holdings"]) == payload["quote_coverage"]["active"] == 2
    assert payload["account"]["risk_budget_status"] is None
    assert payload["account"]["known_stop_risk_lower_bound"] == 0


@pytest.mark.parametrize(
    "max_position,max_portfolio,status",
    [(None, None, "over_budget"), (3, 8, "within_budget"), (3, 1, "over_budget")],
)
def test_both_public_clis_consume_same_input_and_policy(
    max_position, max_portfolio, status, isolated_cache_database, monkeypatch, capsys
):
    _inputs(isolated_cache_database, shares=101)
    mock_price_quotes(monkeypatch, _quotes)
    args = ["--portfolio-value", "100000"]
    if max_position is not None:
        args += [
            "--max-position-risk-pct",
            str(max_position),
            "--max-portfolio-risk-pct",
            str(max_portfolio),
        ]
    result, payload = _run(capsys, args)
    assert result == 0
    assert payload["account"]["risk_budget_status"] == status
    assert payload["account"]["risk_policy"] == risk_budget.policy(
        max_position, max_portfolio
    )
    assert cache.main(["portfolio-risk", *args]) == 0
    text = capsys.readouterr().out
    assert ("已知超预算" in text) == (status == "over_budget")
    assert ("本次有效参数内：预算内" in text) == (status == "within_budget")
    assert payload["account"]["risk_policy"]["source"] in text


@pytest.mark.parametrize(
    "field,bad",
    [
        ("stop_loss_20", None),
        ("shares", 1.5),
    ],
)
def test_text_unknown_risk_never_prints_normal(
    field, bad, isolated_cache_database, monkeypatch, capsys
):
    _inputs(isolated_cache_database)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute(f"UPDATE holdings SET {field}=?", (bad,))
    mock_price_quotes(monkeypatch, _quotes)
    result, payload = _run(capsys, ["--portfolio-value", "100000"])
    assert result == 1
    assert payload["account"]["risk_budget_status"] is None
    assert cache.main(["portfolio-risk", "--portfolio-value", "100000"]) == 0
    text = capsys.readouterr().out
    assert "不可完整判定" in text
    assert "正常" not in text
    assert "本次有效参数内：预算内" not in text


@pytest.mark.parametrize("state,price", [
    ("missing", 100), ("invalid_price", float("nan")),
    ("stale", 100), ("conflicted", 100), ("no_time", 100), ("no_source", 100),
])
def test_text_and_json_share_quote_validation(state, price, isolated_cache_database, monkeypatch, capsys):
    _inputs(isolated_cache_database)
    quote = (
        None
        if state == "missing"
        else market_quotes.PriceQuote(
            price,
            "2000-01-01" if state == "stale" else domain.cst_today(),
            None if state == "no_time" else "10:00:00",
            "" if state == "no_source" else "fixture",
            conflicted=state == "conflicted",
        )
    )
    mock_price_quotes(monkeypatch, lambda codes: dict.fromkeys(codes, quote))
    _, payload = _run(capsys, ["--portfolio-value", "100000"])
    assert payload["account"]["risk_budget_status"] is None
    assert cache.main(["portfolio-risk", "--portfolio-value", "100000"]) == 0
    text = capsys.readouterr().out
    assert "不可完整判定" in text and "正常" not in text


def test_text_distinguishes_breach_and_unknown_and_broken_stop(
    isolated_cache_database, monkeypatch, capsys
):
    _inputs(isolated_cache_database, count=3, shares=101)
    with sqlite3.connect(isolated_cache_database) as conn:
        conn.execute("UPDATE holdings SET stop_loss_20=NULL WHERE code='600001'")
        conn.execute("UPDATE holdings SET stop_loss_20=110 WHERE code='600002'")
    mock_price_quotes(monkeypatch, _quotes)
    _, payload = _run(capsys, ["--portfolio-value", "100000"])
    assert payload["holdings"][2]["stop_risk"] == 0
    assert any(item["reason_code"] == "price_stop_2" for item in payload["escalations"])
    assert cache.main(["portfolio-risk", "--portfolio-value", "100000"]) == 0
    text = capsys.readouterr().out
    assert all(marker in text for marker in ("已知超预算", "不可完整判定", "已破线"))


@pytest.mark.parametrize("empty", [False, True])
def test_portfolio_risk_refuses_unavailable_database(empty, isolated_cache_database, capsys):
    if empty:
        sqlite3.connect(isolated_cache_database).close()
    assert cache.main(["portfolio-risk"]) == 1
    assert "不可用" in capsys.readouterr().err
    assert isolated_cache_database.exists() is empty
    if empty:
        with sqlite3.connect(isolated_cache_database) as conn:
            assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_risk_commands_do_not_write_migrate_or_network(
    isolated_cache_database, monkeypatch, capsys
):
    _inputs(isolated_cache_database, shares=101)
    with sqlite3.connect(isolated_cache_database) as conn:
        before = list(conn.iterdump())
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network"))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("external command/notification")
    )
    mock_price_quotes(monkeypatch, _quotes)
    assert _run(capsys, ["--portfolio-value", "100000"])[0] == 0
    assert cache.main(["portfolio-risk", "--portfolio-value", "100000"]) == 0
    with sqlite3.connect(isolated_cache_database) as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("review_status", "cleared"),
        ("action_status", "no_action"),
        ("stop_reason", "clean_fast_gate"),
    ],
)
def test_contract_rejects_over_budget_clean_combinations(
    field, value, isolated_cache_database
):
    payload = _snapshot(isolated_cache_database, shares=101)
    payload[field] = value
    if field == "stop_reason":
        payload["manifest"][field] = value
    with pytest.raises(monitor_contract.MonitorContractError, match="over_budget"):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("field,patch", [
    ("account", {}),
    ("account", {"risk_policy": None}),
    ("account", {"risk_budget_status": None}),
    ("account", {"total_stop_risk": None}),
    ("account", {"portfolio_value": True}),
    ("quote_coverage", {"active": 2, "priced": 2}),
])
def test_contract_requires_current_safety_evidence(field, patch, isolated_cache_database):
    payload = _snapshot(isolated_cache_database)
    payload[field] = payload[field] | patch if patch else {}
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("duplicate", [False, True])
def test_over_budget_requires_one_escalation(duplicate, isolated_cache_database):
    payload = _snapshot(isolated_cache_database, shares=101)
    payload["escalations"] *= 2 if duplicate else 0
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("field,bad", [
    ("market_value", None), ("stop_risk", True),
    ("stop_risk_pct", "0"), ("portfolio_weight_pct", -1),
])
def test_clean_contract_rejects_invalid_holding_risk(field, bad, isolated_cache_database):
    payload = _snapshot(isolated_cache_database)
    payload["holdings"][0][field] = bad
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("field,bad", [
    ("status", "stale"), ("price", 0), ("source", ""), ("as_of", "invalid"),
])
def test_clean_contract_requires_usable_quote_evidence(field, bad, isolated_cache_database):
    payload = _snapshot(isolated_cache_database)
    payload["holdings"][0]["quote"][field] = bad
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(payload)


def test_omitted_stop_reason_cannot_bypass_clean_evidence(isolated_cache_database):
    payload = _snapshot(isolated_cache_database)
    payload["stop_reason"] = payload["manifest"]["stop_reason"] = None
    payload["holdings"][0]["quote"] = {}
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.loads_monitor_snapshot(json.dumps(payload))


@pytest.mark.parametrize("code,missing", [("600000", "lower_bound"), (None, "holding_risk")])
def test_over_budget_requires_evidence_not_only_detail(code, missing, isolated_cache_database):
    payload = _snapshot(isolated_cache_database, shares=101)
    payload["escalations"][0]["code"] = code
    payload["account"].update(total_stop_risk=None, total_stop_risk_pct=None)
    if missing == "lower_bound":
        payload["account"]["known_stop_risk_lower_bound"] = None
    else:
        payload["holdings"][0].update(stop_risk=None, stop_risk_pct=None)
    with pytest.raises(monitor_contract.MonitorContractError, match="risk"):
        monitor_contract.loads_monitor_snapshot(json.dumps(payload))


def test_portfolio_escalation_routes_only_to_existing_reference(
    isolated_cache_database,
):
    payload = _snapshot(isolated_cache_database, count=5)
    skill = (ROOT / "a-stock-monitor/SKILL.md").read_text()
    rows = [
        line
        for line in skill.splitlines()
        if line.startswith("|") and "risk_budget_exceeded" in line
    ]
    assert len(rows) == 1
    assert "references/portfolio-risk.md" in rows[0]
    reference = ROOT / "a-stock-monitor/references/portfolio-risk.md"
    # Deterministic route replay: one injected snapshot + one reference, no tools/network/model.
    assert [(item["code"], item["reason_code"]) for item in payload["escalations"]] == [
        (None, "risk_budget_exceeded")
    ]
    assert payload["action_status"] == "review_candidate"
    assert reference.exists()
