from __future__ import annotations

import json
import math

import pytest

from a_share_research.policy.v0 import commands_monitor, monitor_contract, risk_budget


def test_escalation_sort_order_matches_contract_vocabulary() -> None:
    assert set(commands_monitor._ESCALATION_REASON_ORDER) == set(
        monitor_contract.ESCALATION_REASON_CODES
    )


def _snapshot(*, clean: bool = True) -> dict:
    return {
        "schema_version": 1,
        "as_of": "2026-09-13T12:00:00+08:00",
        "runtime_version": "0.1.13",
        "data_status": "complete" if clean else "unavailable",
        "valuation_status": "exact" if clean else "unavailable",
        "review_status": "cleared" if clean else "blocked",
        "action_status": "no_action" if clean else "review_candidate",
        "account": {
            "portfolio_value": 100000,
            "denominator_status": "explicit",
            "stock_market_value": 0 if clean else None,
            "stock_weight_pct": 0 if clean else None,
            "total_stop_risk": 0 if clean else None,
            "total_stop_risk_pct": 0 if clean else None,
            "known_stop_risk_lower_bound": 0 if clean else None,
            "risk_budget_status": "within_budget" if clean else None,
            "risk_policy": risk_budget.policy(),
        },
        "quote_coverage": {"priced": 0, "active": 0, "complete": clean},
        "industry_context": {
            "status": "not_applicable",
            "error_code": None,
            "freshness_days": None,
            "source": None,
            "fetched_at": None,
        },
        "holdings": [],
        "escalations": [],
        "data_gaps": []
        if clean
        else [
            {
                "code": None,
                "field": "database",
                "minimum_action": "restore state",
            }
        ],
        "stop_reason": "clean_fast_gate" if clean else None,
        "requires_user_confirmation": False,
        "manifest": {
            "writes": False,
            "stop_reason": "clean_fast_gate" if clean else None,
        },
    }


def _holding() -> dict:
    return {
        "id": 1,
        "code": "600000",
        "name": "浦发银行",
        "framework": "B银行",
        "framework_confident": True,
        "quote": {},
    }


@pytest.mark.parametrize("clean", [True, False])
def test_valid_clean_and_unavailable(clean: bool) -> None:
    payload = _snapshot(clean=clean)
    assert monitor_contract.validate_monitor_snapshot(payload) is payload


def test_unknown_additive_field_is_accepted() -> None:
    payload = _snapshot()
    payload["future"] = {"field": True}
    assert monitor_contract.validate_monitor_snapshot(payload) is payload


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("schema_version", True, "schema_version"),
        ("schema_version", 2, "schema_version"),
        ("as_of", "2026-09-13T12:00:00", "timezone-aware"),
        ("data_status", "exact", "data_status"),
    ],
)
def test_invalid_top_level_value_is_rejected(
    field: str, value: object, match: str
) -> None:
    payload = _snapshot()
    payload[field] = value
    with pytest.raises(monitor_contract.MonitorContractError, match=match):
        monitor_contract.validate_monitor_snapshot(payload)


def test_missing_required_field_is_rejected() -> None:
    payload = _snapshot()
    del payload["account"]
    with pytest.raises(monitor_contract.MonitorContractError, match="account"):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize(
    ("coverage", "match"),
    [
        ({"priced": True, "active": 1, "complete": True}, "priced"),
        ({"priced": 2, "active": 1, "complete": False}, "cannot exceed"),
        ({"priced": 0, "active": 1, "complete": True}, "priced == active"),
        ({"priced": 0, "active": 0, "complete": 1}, "boolean"),
    ],
)
def test_invalid_quote_coverage_is_rejected(coverage: dict, match: str) -> None:
    payload = _snapshot()
    payload["quote_coverage"] = coverage
    with pytest.raises(monitor_contract.MonitorContractError, match=match):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "broken"),
        ("freshness_days", True),
        ("freshness_days", -1),
        ("fetched_at", "not-a-date"),
    ],
)
def test_invalid_industry_context_is_rejected(field: str, value: object) -> None:
    payload = _snapshot()
    payload["industry_context"][field] = value
    with pytest.raises(monitor_contract.MonitorContractError, match="industry_context"):
        monitor_contract.validate_monitor_snapshot(payload)


def test_complete_industry_context_rejects_error_code() -> None:
    payload = _snapshot()
    payload["industry_context"].update(
        {"status": "complete", "error_code": "CACHE_STALE"}
    )
    with pytest.raises(monitor_contract.MonitorContractError, match="error_code"):
        monitor_contract.validate_monitor_snapshot(payload)


def test_noncomplete_data_cannot_produce_no_action() -> None:
    payload = _snapshot(clean=False)
    payload["action_status"] = "no_action"
    with pytest.raises(monitor_contract.MonitorContractError, match="no_action"):
        monitor_contract.validate_monitor_snapshot(payload)


def test_trade_candidate_requires_trade_escalation_and_confirmation() -> None:
    payload = _snapshot()
    payload.update(
        {
            "action_status": "trade_candidate",
            "review_status": "review_required",
            "stop_reason": None,
            "requires_user_confirmation": True,
        }
    )
    payload["manifest"]["stop_reason"] = None
    with pytest.raises(monitor_contract.MonitorContractError, match="trade escalation"):
        monitor_contract.validate_monitor_snapshot(payload)
    payload["escalations"] = [
        {
            "code": "600000",
            "reason_code": "price_stop_2",
            "candidate": "trade",
            "detail": "second stop reached",
        }
    ]
    assert monitor_contract.validate_monitor_snapshot(payload) is payload
    payload["requires_user_confirmation"] = False
    with pytest.raises(monitor_contract.MonitorContractError, match="confirmation"):
        monitor_contract.validate_monitor_snapshot(payload)


def test_degraded_industry_with_holdings_requires_gap() -> None:
    payload = _snapshot(clean=False)
    payload["holdings"] = [_holding()]
    payload["quote_coverage"] = {"priced": 1, "active": 1, "complete": True}
    payload["industry_context"]["status"] = "unavailable"
    with pytest.raises(monitor_contract.MonitorContractError, match="industry_context"):
        monitor_contract.validate_monitor_snapshot(payload)
    payload["data_gaps"].append(
        {
            "code": None,
            "field": "industry_context",
            "minimum_action": "restore industry cache",
        }
    )
    assert monitor_contract.validate_monitor_snapshot(payload) is payload


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("writes", True, "writes"),
        ("writes", 0, "writes"),
        ("stop_reason", None, "stop_reason"),
    ],
)
def test_manifest_invariants(field: str, value: object, match: str) -> None:
    payload = _snapshot()
    payload["manifest"][field] = value
    with pytest.raises(monitor_contract.MonitorContractError, match=match):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("field", ["data_gaps", "quote_coverage"])
def test_clean_fast_gate_requires_no_gaps_and_complete_quotes(field: str) -> None:
    payload = _snapshot()
    if field == "data_gaps":
        payload[field] = [{"code": None, "field": "x", "minimum_action": "review"}]
    else:
        payload[field] = {"priced": 0, "active": 0, "complete": False}
    with pytest.raises(monitor_contract.MonitorContractError, match="clean_fast_gate"):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("constant", [math.nan, math.inf, -math.inf])
def test_nonfinite_numbers_are_rejected(constant: float) -> None:
    payload = _snapshot()
    payload["account"]["value"] = constant
    with pytest.raises(monitor_contract.MonitorContractError, match="finite"):
        monitor_contract.validate_monitor_snapshot(payload)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_loads_rejects_nonfinite_json_constants(constant: str) -> None:
    text = json.dumps(_snapshot()).replace('"portfolio_value": 100000', f'"portfolio_value": {constant}', 1)
    with pytest.raises(monitor_contract.MonitorContractError, match="finite"):
        monitor_contract.loads_monitor_snapshot(text)


@pytest.mark.parametrize("reason", [[], {}, True, False, 0, 1, 1.5, "unknown"])
@pytest.mark.parametrize("json_input", [False, True])
def test_invalid_stop_reason_raises_contract_error(reason, json_input) -> None:
    payload = _snapshot()
    payload["stop_reason"] = reason
    payload["manifest"]["stop_reason"] = reason
    with pytest.raises(monitor_contract.MonitorContractError, match="stop_reason"):
        if json_input:
            monitor_contract.loads_monitor_snapshot(json.dumps(payload))
        else:
            monitor_contract.validate_monitor_snapshot(payload)


def test_loads_and_dumps_round_trip() -> None:
    payload = _snapshot()
    text = monitor_contract.dumps_monitor_snapshot(payload)
    assert " " not in text
    assert monitor_contract.loads_monitor_snapshot(text) == payload


def test_unapplied_policy_golden_and_inconsistent_limits():
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).parent / "golden" / "monitor-policy-not-applied.json").read_text()
    )
    assert monitor_contract.validate_monitor_snapshot(payload) is payload
    payload["account"]["risk_policy"]["max_position_risk_pct"] = 2
    with pytest.raises(monitor_contract.MonitorContractError, match="must be null"):
        monitor_contract.validate_monitor_snapshot(payload)


def test_ordinary_policy_still_rejects_null_limits():
    payload = _snapshot()
    payload["account"]["risk_policy"]["max_position_risk_pct"] = None
    with pytest.raises(monitor_contract.MonitorContractError, match="must be positive"):
        monitor_contract.validate_monitor_snapshot(payload)
