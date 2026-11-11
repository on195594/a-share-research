"""Test factories for strict cache write contracts."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

from a_share_research.policy.v0 import commands_analysis, domain, store

FRAMEWORK_NAMES = {
    "A": "A通用",
    "B": "B银行",
    "C": "C资源",
    "D": "D公用",
    "E": "E消费",
    "F": "F科技",
}


def valid_fundamentals_payload(
    data: dict[str, Any], period: str = "2025年报"
) -> dict[str, Any]:
    """Add the mandatory period, null reasons and provenance envelope."""
    if {"data_period", "null_reasons", "field_provenance"} <= set(data):
        return data
    payload = dict(data)
    null_reasons = {
        field: "fixture missing value" for field, value in data.items() if value is None
    }
    provenance = {
        field: {
            "source": "test-fixture",
            "as_of": period,
            "status": "missing" if value is None else "ok",
        }
        for field, value in data.items()
    }
    payload.update(
        {
            "data_period": period,
            "null_reasons": null_reasons,
            "field_provenance": provenance,
        }
    )
    return payload


def set_valid_fundamentals(
    code: str,
    name: str,
    industry: str,
    data: dict[str, Any],
    ttl: int | None = None,
) -> str:
    """Call production set_fundamentals with a valid fixture envelope."""
    return store.set_fundamentals(
        code, name, industry, valid_fundamentals_payload(data), ttl
    )


def record_valid_quote(code: str, price: float = 10.0) -> None:
    """Write a recent validated quote snapshot for set-analysis tests."""
    store.record_quote_snapshot(
        code,
        price,
        domain.cst_today(),
        "10:00:00",
        "sina",
        {"sina": {"price": price}},
        False,
    )


def valid_decision_payload(
    code: str,
    framework: str = "A",
    score: int | None = 60,
    *,
    action: str | None = None,
    cycle: bool | dict[str, str] | None = None,
    conflict: bool = False,
    narrative: str = "fixture analysis",
) -> dict[str, Any]:
    """Build the smallest valid decision-v1 fixture."""
    normalized = FRAMEWORK_NAMES.get(framework, framework)
    blocked = conflict or score is None
    reasons = (
        ["valuation_conflict" if conflict else "score_incomplete"] if blocked else []
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "stock_code": code,
        "framework": normalized,
        "framework_score": None if conflict else score,
        "framework_classification": "not_formed" if blocked else "A级",
        "rule_version": "test-v1",
        "rule_hash": "test-rule-hash",
        "l3_status": "not_triggered",
        "portfolio_risk": {"status": "clear", "reason_codes": []},
        "suggested_action": action or ("等待" if blocked else "买入候选"),
        "blocked": blocked,
        "block_reason": reasons,
        "source_provenance": {"fixture": "tests.helpers"},
        "freshness": {"as_of": f"{domain.cst_today()}T10:00:00+08:00"},
        "narrative": narrative,
    }
    if conflict:
        payload["source_provenance"]["valuation_conflict"] = {
            "pb_conclusion": "低估",
            "cross_valuation_conclusion": "偏贵",
            "evidence": ["fixture valuation evidence"],
        }
    if cycle is True:
        payload["cycle_stage"] = {"stage": "上行期", "rationale": "fixture cycle"}
    elif isinstance(cycle, dict):
        payload["cycle_stage"] = cycle
    return payload


def set_valid_analysis(monkeypatch, **kwargs: Any) -> dict[str, Any]:
    """Write a decision-v1 fixture through the production stdin command."""
    payload = valid_decision_payload(**kwargs)
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload, ensure_ascii=False)))
    commands_analysis.cmd_set_analysis([])
    return payload


def mock_price_quotes(monkeypatch, fetch):
    """Supply fixture quotes to both risk views without an industry provider."""
    from a_share_research.policy.v0 import commands_holdings, market_quotes

    monkeypatch.setattr(market_quotes, "fetch_current_price_quotes", fetch)
    monkeypatch.setattr(
        commands_holdings,
        "fetch_monitor_price_quotes",
        lambda codes: (fetch(codes), None),
    )
