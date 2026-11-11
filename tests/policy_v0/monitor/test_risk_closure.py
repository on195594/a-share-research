"""S1 current-contract fixture acceptance, not live-client qualification."""

import json
import socket
import sqlite3
import subprocess
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

from a_share_research.policy.v0 import (
    cache,
    commands_monitor,
    db,
    domain,
    market_quotes,
    monitor_contract,
    risk_budget,
    schema,
)
from policy_v0.helpers import mock_price_quotes
from policy_v0.monitor.test_monitor_snapshot import _quotes, _run, _seed_holding

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


@pytest.mark.parametrize("field", ["shares", "stop_loss_20", "price"])
@pytest.mark.parametrize(
    "bad", [None, True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "100"]
)
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


@pytest.mark.parametrize(
    "bad", [None, True, False, 0, -1, float("nan"), float("inf"), "100000"]
)
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
        ("stop_loss_20", "bad"),
        ("stop_loss_20", 0),
        ("stop_loss_20", float("inf")),
        ("shares", None),
        ("shares", -1),
        ("shares", 1.5),
        ("shares", "bad"),
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


@pytest.mark.parametrize(
    "state",
    [
        "missing",
        "nan",
        "inf",
        "bool",
        "zero",
        "negative",
        "stale",
        "conflicted",
        "no_time",
        "no_source",
    ],
)
def test_text_and_json_share_quote_validation(
    state, isolated_cache_database, monkeypatch, capsys
):
    _inputs(isolated_cache_database)
    price = {
        "nan": float("nan"),
        "inf": float("inf"),
        "bool": True,
        "zero": 0,
        "negative": -1,
    }.get(state, 100)
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


@pytest.mark.parametrize("missing_schema", [False, True])
def test_text_missing_database_does_not_bootstrap(
    missing_schema, isolated_cache_database, monkeypatch, capsys
):
    if missing_schema:
        with sqlite3.connect(isolated_cache_database) as conn:
            conn.execute("CREATE TABLE sentinel (value)")
    monkeypatch.setattr(
        schema, "bootstrap_database_schema", lambda *a: pytest.fail("migration")
    )
    mock_price_quotes(monkeypatch, lambda *a: pytest.fail("network"))
    assert cache.main(["portfolio-risk"]) == 1
    assert "不可用" in capsys.readouterr().err
    if not missing_schema:
        assert not isolated_cache_database.exists()
    else:
        with sqlite3.connect(isolated_cache_database) as conn:
            assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [
                ("sentinel",)
            ]


def test_risk_commands_do_not_write_migrate_or_network(
    isolated_cache_database, monkeypatch, capsys
):
    _inputs(isolated_cache_database, shares=101)
    with sqlite3.connect(isolated_cache_database) as conn:
        before = list(conn.iterdump())
    original = db.read_only_db_session
    sql = []

    def authorize(action, *_):
        return (
            sqlite3.SQLITE_OK
            if action
            in {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_TRANSACTION,
            }
            else sqlite3.SQLITE_DENY
        )

    @contextmanager
    def checked():
        with original() as conn:
            conn.set_authorizer(authorize)
            conn.set_trace_callback(sql.append)
            yield conn

    monkeypatch.setattr(db, "read_only_db_session", checked)
    monkeypatch.setattr(
        schema, "bootstrap_database_schema", lambda *a: pytest.fail("migration")
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network"))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("external command/notification")
    )
    mock_price_quotes(monkeypatch, _quotes)
    assert _run(capsys, ["--portfolio-value", "100000"])[0] == 0
    assert cache.main(["portfolio-risk", "--portfolio-value", "100000"]) == 0
    assert sql and all(
        statement.lstrip().startswith(("SELECT", "BEGIN", "COMMIT"))
        for statement in sql
    )
    with sqlite3.connect(isolated_cache_database) as conn:
        assert list(conn.iterdump()) == before


def test_confirmed_executed_buy_is_not_blocked_by_budget(
    isolated_cache_database, monkeypatch, capsys
):
    _inputs(isolated_cache_database, shares=101)
    mock_price_quotes(monkeypatch, _quotes)
    assert (
        _run(capsys, ["--portfolio-value", "100000"])[1]["account"][
            "risk_budget_status"
        ]
        == "over_budget"
    )
    args = ["buy-holding", "600000", "100", "100", "--date", domain.cst_today()]
    assert cache.main(args) == 3
    assert cache.main(["--confirm-write", *args]) == 0
    with sqlite3.connect(isolated_cache_database) as conn:
        assert conn.execute("SELECT shares FROM holdings").fetchone() == (201,)
        assert conn.execute(
            "SELECT shares FROM holding_events WHERE event_type='buy'"
        ).fetchone() == (100,)
    capsys.readouterr()
    assert (
        _run(capsys, ["--portfolio-value", "100000"])[1]["account"][
            "risk_budget_status"
        ]
        == "over_budget"
    )


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


@pytest.mark.parametrize(
    "mutation",
    [
        "account_missing",
        "policy_missing",
        "unknown_budget",
        "risk_missing",
        "active_count",
        "bool_value",
        "zero_value",
        "negative_value",
        "duplicate_budget",
        "missing_budget_escalation",
    ],
)
def test_contract_requires_current_safety_evidence(mutation, isolated_cache_database):
    payload = _snapshot(
        isolated_cache_database,
        shares=101
        if "budget_escalation" in mutation or mutation == "duplicate_budget"
        else 100,
    )
    if mutation == "account_missing":
        payload["account"] = {}
    elif mutation == "policy_missing":
        del payload["account"]["risk_policy"]
    elif mutation == "unknown_budget":
        payload["account"]["risk_budget_status"] = None
    elif mutation == "risk_missing":
        payload["account"]["total_stop_risk"] = None
    elif mutation == "active_count":
        payload["quote_coverage"].update(active=2, priced=2)
    elif mutation in {"bool_value", "zero_value", "negative_value"}:
        payload["account"]["portfolio_value"] = {
            "bool_value": True,
            "zero_value": 0,
            "negative_value": -1,
        }[mutation]
    elif mutation == "duplicate_budget":
        payload["escalations"].append(deepcopy(payload["escalations"][0]))
    elif mutation == "missing_budget_escalation":
        payload["escalations"] = []
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize(
    "field", ["market_value", "stop_risk", "stop_risk_pct", "portfolio_weight_pct"]
)
@pytest.mark.parametrize("bad", [None, True, "0", -1])
def test_clean_contract_rejects_invalid_holding_risk(
    field, bad, isolated_cache_database
):
    payload = _snapshot(isolated_cache_database)
    payload["holdings"][0][field] = bad
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("status", "stale"),
        ("price", 0),
        ("price", True),
        ("source", ""),
        ("as_of", "invalid"),
    ],
)
def test_clean_contract_requires_usable_quote_evidence(
    field, bad, isolated_cache_database
):
    payload = _snapshot(isolated_cache_database)
    payload["holdings"][0]["quote"][field] = bad
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("field", ["quote", "shares", "stop_loss_20", "market_value"])
def test_omitted_stop_reason_cannot_bypass_clean_evidence(
    field, isolated_cache_database
):
    payload = _snapshot(isolated_cache_database)
    payload["stop_reason"] = payload["manifest"]["stop_reason"] = None
    payload["holdings"][0].pop(field)
    if field == "quote":
        payload["holdings"][0]["quote"] = {}
    with pytest.raises(monitor_contract.MonitorContractError):
        monitor_contract.loads_monitor_snapshot(json.dumps(payload))


@pytest.mark.parametrize("code", ["600000", None])
@pytest.mark.parametrize("missing", ["lower_bound", "holding_risk", "both"])
def test_over_budget_requires_evidence_not_only_detail(
    code, missing, isolated_cache_database
):
    payload = _snapshot(isolated_cache_database, shares=101)
    payload["escalations"][0]["code"] = code
    payload["account"]["total_stop_risk"] = None
    payload["account"]["total_stop_risk_pct"] = None
    if missing in {"lower_bound", "both"}:
        payload["account"]["known_stop_risk_lower_bound"] = None
    if missing in {"holding_risk", "both"}:
        payload["holdings"][0]["stop_risk"] = None
        payload["holdings"][0]["stop_risk_pct"] = None
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
    for marker in ("code=null", "不重复取持仓或报价", "不调用 W1", "不展开全组合研究"):
        assert marker in reference.read_text()
    assert "不心算补全风险" in skill
    assert "不阻断用户确认的已成交事实记账" in skill
