"""Phase 4 versioned structured decision contract."""

from __future__ import annotations

import copy
import json
import sqlite3
from io import StringIO
from pathlib import Path

import pytest

from a_share_research.policy.v0 import commands_analysis, decision_contract, domain, store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REQUIRED_FIELDS = {
    "schema_version",
    "stock_code",
    "framework",
    "framework_score",
    "framework_classification",
    "rule_version",
    "rule_hash",
    "l3_status",
    "portfolio_risk",
    "suggested_action",
    "blocked",
    "block_reason",
    "source_provenance",
    "freshness",
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    "name",
    [
        "decision_v1_complete.json",
        "decision_v1_blocked.json",
        "decision_v1_additive_unknown.json",
    ],
)
def test_v1_golden_fixtures_are_valid_and_complete(name: str) -> None:
    payload = fixture(name)

    validated = decision_contract.validate_decision(payload)

    assert REQUIRED_FIELDS <= validated.keys()
    assert validated == payload


def test_additive_unknown_fields_survive_canonical_serialization() -> None:
    payload = fixture("decision_v1_additive_unknown.json")

    encoded = decision_contract.dumps_decision(
        decision_contract.loads_decision(json.dumps(payload, ensure_ascii=False))
    )

    assert json.loads(encoded)["future_contract_field"] == {"kept": True}
    assert json.loads(encoded)["portfolio_risk"]["future_limit"] == {"value": 7}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda p: p.pop("freshness"), "freshness"),
        (lambda p: p.__setitem__("schema_version", 2), "schema_version"),
        (lambda p: p.__setitem__("framework_score", True), "framework_score"),
        (lambda p: p.__setitem__("framework_score", float("nan")), "finite"),
        (lambda p: p.__setitem__("blocked", "false"), "blocked"),
        (lambda p: p.__setitem__("block_reason", "none"), "block_reason"),
        (lambda p: p.__setitem__("source_provenance", []), "source_provenance"),
        (lambda p: p.__setitem__("freshness", {"as_of": "yesterday"}), "as_of"),
    ],
)
def test_v1_rejects_missing_wrong_nonfinite_or_unsupported_values(
    mutation, message: str
) -> None:
    payload = copy.deepcopy(fixture("decision_v1_complete.json"))
    mutation(payload)

    with pytest.raises(decision_contract.DecisionContractError, match=message):
        decision_contract.validate_decision(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"blocked": True, "block_reason": []},
        {"blocked": False, "block_reason": ["score_incomplete"]},
        {
            "blocked": True,
            "block_reason": ["score_incomplete"],
            "suggested_action": "买入候选",
        },
        {"framework_score": None},
        {"framework_classification": "not_formed"},
        {"portfolio_risk": {"status": "incomplete", "reason_codes": []}},
        {"l3_status": "triggered"},
    ],
)
def test_v1_rejects_inconsistent_safety_combinations(changes: dict) -> None:
    payload = copy.deepcopy(fixture("decision_v1_complete.json"))
    payload.update(changes)

    with pytest.raises(decision_contract.DecisionContractError):
        decision_contract.validate_decision(payload)


def test_structured_valuation_conflict_renders_without_regex_contract() -> None:
    markdown = decision_contract.render_decision_markdown(
        fixture("decision_v1_blocked.json")
    )

    assert "valuation_conflict" in markdown
    assert "正式披露与可复核假设" in markdown
    assert "估值冲突[状态=" not in markdown


def test_set_analysis_persists_json_as_authority_and_renders_markdown(
    monkeypatch, isolated_cache_database
) -> None:
    payload = fixture("decision_v1_complete.json")
    now = domain.utc_now_iso()
    store.record_quote_snapshot(
        payload["stock_code"],
        100.0,
        domain.cst_today(),
        "10:00:00",
        "test",
        {"test": {"price": 100.0}},
        False,
        fetched_at=now,
    )
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload, ensure_ascii=False)))

    commands_analysis.cmd_set_analysis([])

    with sqlite3.connect(isolated_cache_database) as conn:
        decision_json, result, score, framework, scoring_status = conn.execute(
            """SELECT decision_json, result, score, framework, scoring_status
               FROM analysis_results WHERE code=? AND date=?""",
            (payload["stock_code"], domain.cst_today()),
        ).fetchone()
    assert json.loads(decision_json) == payload
    assert "品牌与渠道仍有优势。" in result
    assert "建议动作：买入候选" in result
    assert score == 68
    assert framework == "E消费"
    assert scoring_status == "complete"


def test_invalid_decision_fails_before_write(
    monkeypatch, isolated_cache_database
) -> None:
    payload = fixture("decision_v1_complete.json")
    payload["blocked"] = True
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload, ensure_ascii=False)))

    with pytest.raises(SystemExit):
        commands_analysis.cmd_set_analysis([])

    assert not isolated_cache_database.exists()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["source_provenance"].clear(),
            "valuation_conflict evidence and block_reason must coincide",
        ),
        (
            lambda payload: payload["source_provenance"]["valuation_conflict"].pop(
                "evidence"
            ),
            "source_provenance.valuation_conflict.evidence",
        ),
        (
            lambda payload: payload["block_reason"].remove("valuation_conflict"),
            "valuation_conflict evidence and block_reason must coincide",
        ),
    ],
)
def test_valuation_conflict_evidence_and_block_reason_are_bidirectional(
    mutate, message
) -> None:
    payload = fixture("decision_v1_blocked.json")
    mutate(payload)

    with pytest.raises(decision_contract.DecisionContractError, match=message):
        decision_contract.validate_decision(payload)
