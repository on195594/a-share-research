"""Versioned machine-readable investment decision contract."""

from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Any

from a_share_research.core.contracts import CycleStage, CycleStageAssessment


class DecisionContractError(ValueError):
    """A decision payload does not satisfy the supported contract."""


_REQUIRED_FIELDS = {
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
_FRAMEWORKS = {"A通用", "B银行", "C资源", "D公用", "E消费", "F科技"}
_CLASSIFICATIONS = {"A级", "B级", "C级", "D级", "not_formed"}
_L3_STATUSES = {
    "not_applicable",
    "not_triggered",
    "pending",
    "watch",
    "triggered",
    "invalid",
}
_PORTFOLIO_STATUSES = {"clear", "blocked", "incomplete", "not_evaluated"}
_ACTIONS = {
    "回避",
    "等待",
    "买入候选",
    "观望",
    "轻仓试探",
    "小仓观察",
    "买入1/3仓",
    "积极买入2/3仓",
}
_BLOCKED_ACTIONS = {"回避", "等待", "观望"}
_BLOCK_REASONS = {
    "risk_gate_incomplete",
    "valuation_conflict",
    "bond_yield_missing",
    "score_incomplete",
    "portfolio_risk_blocked",
    "l3_triggered",
}


def _reject_nonfinite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise DecisionContractError(f"all numbers must be finite ({path})")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DecisionContractError(f"{field} must be an array of strings")
    return value


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionContractError(f"{field} must be a non-empty string")
    return value


def _supported_string(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise DecisionContractError(f"{field} is unsupported")
    return value


def _validate_cycle_stage(value: Any) -> CycleStageAssessment | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DecisionContractError("cycle_stage must be an object or null")
    try:
        stage = CycleStage(value.get("stage"))
    except ValueError as exc:
        raise DecisionContractError("cycle_stage.stage is unsupported") from exc
    rationale = _nonempty_string(value.get("rationale"), "cycle_stage.rationale")
    return CycleStageAssessment(stage=stage, rationale=rationale)


def validate_decision(decision: Any) -> dict[str, Any]:
    """Validate a v1 decision without discarding additive fields."""
    if not isinstance(decision, dict):
        raise DecisionContractError("decision must be a JSON object")
    _reject_nonfinite(decision)

    missing = sorted(_REQUIRED_FIELDS - decision.keys())
    if missing:
        raise DecisionContractError(f"missing required field: {missing[0]}")

    version = decision["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise DecisionContractError("schema_version must be integer 1")
    code = decision["stock_code"]
    if not isinstance(code, str) or re.fullmatch(r"\d{6}", code) is None:
        raise DecisionContractError("stock_code must be a six-digit string")
    _supported_string(decision["framework"], _FRAMEWORKS, "framework")

    score = decision["framework_score"]
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not 0 <= score <= 80
    ):
        raise DecisionContractError(
            "framework_score must be a finite number from 0 to 80 or null"
        )
    classification = _supported_string(
        decision["framework_classification"],
        _CLASSIFICATIONS,
        "framework_classification",
    )
    _nonempty_string(decision["rule_version"], "rule_version")
    _nonempty_string(decision["rule_hash"], "rule_hash")

    l3_status = _supported_string(decision["l3_status"], _L3_STATUSES, "l3_status")
    portfolio_risk = decision["portfolio_risk"]
    if not isinstance(portfolio_risk, dict):
        raise DecisionContractError("portfolio_risk must be an object")
    portfolio_status = _supported_string(
        portfolio_risk.get("status"), _PORTFOLIO_STATUSES, "portfolio_risk.status"
    )
    portfolio_reasons = _string_list(
        portfolio_risk.get("reason_codes"), "portfolio_risk.reason_codes"
    )

    action = _supported_string(
        decision["suggested_action"], _ACTIONS, "suggested_action"
    )
    blocked = decision["blocked"]
    if not isinstance(blocked, bool):
        raise DecisionContractError("blocked must be a boolean")
    block_reasons = _string_list(decision["block_reason"], "block_reason")
    unsupported_reasons = sorted(set(block_reasons) - _BLOCK_REASONS)
    if unsupported_reasons:
        raise DecisionContractError(
            f"block_reason is unsupported: {unsupported_reasons[0]}"
        )

    source_provenance = decision["source_provenance"]
    if not isinstance(source_provenance, dict):
        raise DecisionContractError("source_provenance must be an object")
    valuation_conflict = source_provenance.get("valuation_conflict")
    if valuation_conflict is not None:
        if not isinstance(valuation_conflict, dict):
            raise DecisionContractError(
                "source_provenance.valuation_conflict must be an object"
            )
        _nonempty_string(
            valuation_conflict.get("pb_conclusion"),
            "source_provenance.valuation_conflict.pb_conclusion",
        )
        _nonempty_string(
            valuation_conflict.get("cross_valuation_conclusion"),
            "source_provenance.valuation_conflict.cross_valuation_conclusion",
        )
        evidence = _string_list(
            valuation_conflict.get("evidence"),
            "source_provenance.valuation_conflict.evidence",
        )
        if not evidence or any(not item.strip() for item in evidence):
            raise DecisionContractError(
                "source_provenance.valuation_conflict.evidence must contain "
                "non-empty strings"
            )
    freshness = decision["freshness"]
    if not isinstance(freshness, dict):
        raise DecisionContractError("freshness must be an object")
    as_of = _nonempty_string(freshness.get("as_of"), "freshness.as_of")
    try:
        datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionContractError("freshness.as_of must be ISO-8601") from exc
    if "narrative" in decision and not isinstance(decision["narrative"], str):
        raise DecisionContractError("narrative must be a string")
    if "cycle_stage" in decision:
        _validate_cycle_stage(decision["cycle_stage"])

    if blocked:
        if not block_reasons:
            raise DecisionContractError("blocked decisions require block_reason")
        if action not in _BLOCKED_ACTIONS:
            raise DecisionContractError(
                "blocked decisions require a safe suggested_action"
            )
    elif block_reasons:
        raise DecisionContractError("unblocked decisions cannot have block_reason")
    has_valuation_reason = "valuation_conflict" in block_reasons
    if (valuation_conflict is not None) != has_valuation_reason:
        raise DecisionContractError(
            "valuation_conflict evidence and block_reason must coincide"
        )
    if (score is None) != (classification == "not_formed"):
        raise DecisionContractError(
            "framework_score null and framework_classification not_formed must coincide"
        )
    if classification == "not_formed" and not blocked:
        raise DecisionContractError("not_formed decisions must be blocked")
    if portfolio_status in {"blocked", "incomplete", "not_evaluated"} and not blocked:
        raise DecisionContractError(
            "non-clear portfolio_risk status requires a blocked decision"
        )
    if portfolio_status == "clear" and portfolio_reasons:
        raise DecisionContractError("clear portfolio_risk cannot have reason_codes")
    if l3_status in {"triggered", "invalid"} and not blocked:
        raise DecisionContractError(
            "triggered or invalid l3_status requires a blocked decision"
        )

    return decision


def loads_decision(text: str) -> dict[str, Any]:
    """Parse and validate one JSON decision."""
    try:
        decision = json.loads(
            text,
            parse_constant=lambda value: (_ for _ in ()).throw(
                DecisionContractError(f"all numbers must be finite ({value})")
            ),
        )
    except DecisionContractError:
        raise
    except (TypeError, json.JSONDecodeError) as exc:
        raise DecisionContractError(f"invalid decision JSON: {exc}") from exc
    return validate_decision(decision)


def dumps_decision(decision: Any) -> str:
    """Validate and emit deterministic JSON while retaining additive fields."""
    return json.dumps(
        validate_decision(decision),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def render_decision_markdown(decision: Any) -> str:
    """Render the validated decision for people; JSON remains authoritative."""
    value = validate_decision(decision)
    score = value["framework_score"]
    lines = [
        f"# 投资决策：{value['stock_code']}",
        f"- 框架：{value['framework']}",
        f"- 框架得分：{'未形成' if score is None else f'{score}/80'}",
        f"- 框架评级：{value['framework_classification']}",
        f"- L3 状态：{value['l3_status']}",
        f"- 组合风险：{value['portfolio_risk']['status']}",
        f"- 建议动作：{value['suggested_action']}",
        f"- 阻断：{'是' if value['blocked'] else '否'}",
        f"- 阻断原因：{', '.join(value['block_reason']) or '无'}",
        f"- 规则：{value['rule_version']} ({value['rule_hash']})",
        f"- 时效：{value['freshness']['as_of']}",
    ]
    narrative = value.get("narrative")
    if narrative:
        lines.extend(["", "## 分析说明", narrative])
    lines.extend(
        [
            "",
            "## 来源证据",
            json.dumps(value["source_provenance"], ensure_ascii=False, sort_keys=True),
        ]
    )
    return "\n".join(lines)
