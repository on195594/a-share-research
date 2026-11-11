"""Versioned machine-readable monitoring snapshot contract."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import json
import math
import re
from typing import Any

from a_share_research.policy.v0 import boards


class MonitorContractError(ValueError):
    """A monitoring snapshot does not satisfy monitor-v1."""


DATA_STATUSES = frozenset({"complete", "partial", "stale", "unavailable", "conflicted"})
VALUATION_STATUSES = frozenset({"exact", "priced_positions_lower_bound", "unavailable"})
REVIEW_STATUSES = frozenset({"cleared", "review_required", "blocked"})
ACTION_STATUSES = frozenset({"no_action", "review_candidate", "trade_candidate"})
INDUSTRY_CONTEXT_STATUSES = frozenset(
    {"complete", "stale", "invalid", "unavailable", "not_applicable"}
)
ESCALATION_REASON_CODES = frozenset(
    {
        "price_stop_1",
        "price_stop_2",
        "daily_drop",
        "relative_underperformance",
        "tier_review_due",
        "alert_review_due",
        "l3_review_due",
        "l3_candidate",
        "l3_watch",
        "research_thesis_review",
        "governance_gate",
        "risk_budget_exceeded",
        "quote_gap",
        "denominator_missing",
        "data_conflict",
    }
)
_REQUIRED_FIELDS = {
    "schema_version",
    "as_of",
    "runtime_version",
    "data_status",
    "valuation_status",
    "review_status",
    "action_status",
    "account",
    "quote_coverage",
    "industry_context",
    "holdings",
    "escalations",
    "data_gaps",
    "stop_reason",
    "requires_user_confirmation",
    "manifest",
}


def _reject_nonfinite(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise MonitorContractError(f"all numbers must be finite ({path})")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MonitorContractError(f"{field} must be an object")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise MonitorContractError(f"{field} must be an array")
    return value


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MonitorContractError(f"{field} must be a non-empty string")
    return value


def _supported(value: Any, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MonitorContractError(f"{field} is unsupported")
    return value


def _iso8601(value: Any, field: str, *, aware: bool = False) -> str:
    text = _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MonitorContractError(f"{field} must be ISO-8601") from exc
    if aware and (parsed.tzinfo is None or parsed.utcoffset() is None):
        raise MonitorContractError(f"{field} must be timezone-aware")
    return text


def _optional_string(value: Any, field: str) -> None:
    if value is not None and not isinstance(value, str):
        raise MonitorContractError(f"{field} must be a string or null")


def _required(item: dict[str, Any], fields: set[str], field: str) -> None:
    missing = sorted(fields - item.keys())
    if missing:
        raise MonitorContractError(f"missing required field: {field}.{missing[0]}")


RESEARCH_STATUSES = frozenset({"ok", "not_configured", "unavailable", "skipped"})
REVIEW_PROMPT_KINDS = frozenset({"valuation_stretched", "opportunity_cost", "research_unavailable", "research_opportunity"})
STANCE_ACTIONS = frozenset({"现在分批", "等价格", "等证据", "回避"})
# Added in 0.1.35; optional so earlier snapshots remain readable, but all-or-none when present.
_RESEARCH_STANCE_FIELDS = ("stance_action", "stance_price", "stance_valid_until", "expected_return", "v2_zone")
_RESEARCH_ROW_FIELDS = {
    "code", "card", "card_status", "verdict", "report_id", "completed_at",
    "close", "close_date", "zone", "neutral_return", "conservative_return",
    "optimistic_return", "note",
}


def _research_code(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value) is None:
        raise MonitorContractError("research code must be six digits")


def _research_number(value: Any) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))):
        raise MonitorContractError("research number must be numeric or null")


def _research_date(value: Any) -> None:
    if value is not None:
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            raise MonitorContractError("research close_date must be an ISO date or null") from None


def stance_add_trigger(frozen: dict) -> dict | None:
    """Project a new checkpoint or the legacy evidence wait without rewriting the card."""
    return frozen.get("trigger" if frozen.get("action") == "等证据" else "add_trigger")


def _research_trigger(value: Any) -> None:
    trigger = _object(value, "research.card.add_trigger")
    if set(trigger) != {"metric", "period", "op", "value"}:
        raise MonitorContractError("research add_trigger requires metric, period, op, value")
    if not isinstance(trigger["metric"], str) or not re.fullmatch(r"T\.[A-Za-z0-9_]+", trigger["metric"]):
        raise MonitorContractError("research add_trigger metric must be a T.* row")
    _research_date(trigger["period"])
    if trigger["period"] is None:
        raise MonitorContractError("research add_trigger period is required")
    _supported(trigger["op"], frozenset({">=", "<="}), "research.add_trigger.op")
    _research_number(trigger["value"])
    if trigger["value"] is None or not math.isfinite(trigger["value"]):
        raise MonitorContractError("research add_trigger value must be finite")


_RESEARCH_CAUTION_FIELDS = ("open_questions", "report_boundary", "current_boundary")


def research_cautions(card: dict | None) -> dict:
    """Preserve supplied cautions; an absent legacy field is not false or an empty list."""
    card = card or {}
    out = (
        {"open_questions": deepcopy(card["open_questions"])}
        if "open_questions" in card
        else {}
    )
    for field, valuation in (
        ("report_boundary", card.get("valuation_calc") or {}),
        ("current_boundary", ((card.get("current") or {}).get("valuation") or {})),
    ):
        if "boundary" in valuation:
            out[field] = valuation["boundary"]
    return out


def _research_caution_values(values: dict) -> None:
    for field in ("report_boundary", "current_boundary"):
        if (
            field in values
            and values[field] is not None
            and type(values[field]) is not bool
        ):
            raise MonitorContractError(f"research {field} must be boolean or null")
    if "open_questions" in values:
        ids = []
        for raw in _array(values["open_questions"], "research.open_questions"):
            question = _object(raw, "research.open_question")
            _required(question, {"id", "where", "problem"}, "research.open_question")
            for field in ("id", "where", "problem"):
                _nonempty(question[field], f"research.open_question.{field}")
            ids.append(question["id"])
        if len(ids) != len(set(ids)):
            raise MonitorContractError("research open_questions has duplicate ids")


def _research_boundary(valuation: dict) -> None:
    if "boundary" not in valuation:
        return
    _research_caution_values({"report_boundary": valuation["boundary"]})
    if valuation["boundary"] is not None:
        for number in (
            valuation.get("close"),
            (valuation.get("v2") or {}).get("support_below"),
        ):
            _research_number(number)
            if number is None or number <= 0:
                raise MonitorContractError(
                    "research boundary requires positive close and v2 hurdle"
                )


def _review_cautions(evidence: dict, card: dict | None) -> None:
    actual = {key: evidence[key] for key in _RESEARCH_CAUTION_FIELDS if key in evidence}
    _research_caution_values(actual)
    if actual != research_cautions(card):
        raise MonitorContractError("research caution evidence is inconsistent")


def _research_card(raw: Any) -> dict:
    card = _object(raw, "research.card")
    _required(card, {"code", "origin", "status", "verdict", "report_id", "current", "pending_events"}, "research.card")
    _research_code(card["code"])
    _supported(card["origin"], frozenset({"external", "l5"}), "research.card.origin")
    _supported(card["status"], frozenset({"有效", "待复核"}), "research.card.status")
    if type(card["report_id"]) is not int or card["report_id"] <= 0:
        raise MonitorContractError("research report_id must be a positive integer")
    _optional_string(card.get("verdict"), "research.card.verdict")
    for raw_event in _array(card["pending_events"], "research.card.pending_events"):
        event = _object(raw_event, "research.card.pending_event")
        for field in ("title", "kind", "type"):
            _optional_string(event.get(field), "research.card.event")
    current = _object(card["current"], "research.card.current")
    _required(current, {"last_close", "last_close_date", "current_zone", "valuation"}, "research.card.current")
    _research_number(current["last_close"])
    _research_date(current["last_close_date"])
    for field in ("current_zone", "note", "reason"):
        _optional_string(current.get(field), "research.card.current")
    if current["valuation"] is not None:
        valuation = _object(current["valuation"], "research.card.valuation")
        _required(valuation, {"annual_returns", "horizon"}, "research.card.valuation")
        for field in ("horizon", "support_below", "stretched_above", "close", "neutral_return_without_dividends"):
            _research_number(valuation.get(field))
        _optional_string(valuation.get("reason"), "research.card.valuation.reason")
        _optional_string(valuation.get("assumption"), "research.card.valuation.assumption")
        if valuation.get("v2") is not None:
            v2 = _object(valuation["v2"], "research.card.valuation.v2")
            for field in ("annual_return", "support_below", "stretched_above"):
                _research_number(v2.get(field))
            _optional_string(v2.get("zone"), "research.card.valuation.v2.zone")
        if valuation["annual_returns"] is not None:
            returns = _object(valuation["annual_returns"], "research.card.annual_returns")
            _required(returns, {"保守", "中性", "乐观"}, "research.card.annual_returns")
            for number in returns.values():
                _research_number(number)
    frozen_value = card.get("valuation_calc")
    if frozen_value is not None:
        frozen_value = _object(frozen_value, "research.card.valuation_calc")
        if frozen_value.get("v2") is not None:
            _object(frozen_value["v2"], "research.card.valuation_calc.v2")
        _research_boundary(frozen_value)
    if current["valuation"] is not None:
        _research_boundary(current["valuation"])
    _research_caution_values(research_cautions(card))
    stance = card.get("stance")
    if stance is not None:
        stance = _object(stance, "research.card.stance")
    # Research V4 frozen stance (Research 2026-11-04); older cards have none.
    if card.get("stance_calc") is not None:
        frozen = _object(card["stance_calc"], "research.card.stance_calc")
        _supported(frozen.get("action"), STANCE_ACTIONS, "research.card.stance_calc.action")
        _research_number(frozen.get("price"))
        _research_date(frozen.get("valid_until"))
        for field, action in (("add_trigger", "现在分批"), ("trigger", "等证据")):
            if field in frozen:
                if frozen["action"] != action:
                    raise MonitorContractError("research checkpoint action is inconsistent")
                _research_trigger(frozen[field])
            if stance is not None and stance.get(field) != frozen.get(field):
                raise MonitorContractError("research frozen checkpoint is inconsistent with stance")
        if frozen["action"] == "等证据" and "trigger" not in frozen:
            raise MonitorContractError("legacy evidence stance requires trigger")
    return card


def validate_research_cards(payload: dict) -> None:
    """Check the optional HTTP boundary before it can affect a monitor snapshot."""
    _reject_nonfinite(payload)
    _required(payload, {"cards", "missing"}, "research")
    codes = []
    for raw in _array(payload["cards"], "research.cards"):
        codes.append(_research_card(raw)["code"])
    for raw in _array(payload["missing"], "research.missing"):
        item = _object(raw, "research.missing")
        _research_code(item.get("code"))
        _supported(item.get("origin"), frozenset({"external", "l5"}), "research.missing.origin")
        codes.append(item["code"])
    if len(set(codes)) != len(codes):
        raise MonitorContractError("research response has duplicate codes")


def _validate_research_fields(value: dict, holding_codes: set[str], untradable: list[str] | None = None) -> None:
    # Old snapshots remain readable; when present the additive pair is validated.
    if "research" not in value and "review_prompts" not in value:
        return
    _required(value, {"research", "review_prompts"}, "snapshot")
    research = _object(value["research"], "research")
    _required(research, {"status", "holdings", "candidates", "missing"}, "research")
    status = _supported(research["status"], RESEARCH_STATUSES, "research.status")
    _optional_string(research.get("reason"), "research.reason")
    for group in ("holdings", "candidates"):
        codes = []
        for raw in _array(research[group], f"research.{group}"):
            row = _object(raw, "research.row")
            _required(row, _RESEARCH_ROW_FIELDS, "research.row")
            _research_code(row["code"])
            codes.append(row["code"])
            if row["card"] is not None:
                card = _research_card(row["card"])
                if status != "ok" or any(row[left] != card.get(right) for left, right in (
                    ("code", "code"), ("card_status", "status"), ("report_id", "report_id"),
                    ("completed_at", "completed_at"), ("verdict", "verdict"),
                )):
                    raise MonitorContractError("research card projection is inconsistent")
                if group == "candidates" and card["origin"] != "l5":
                    raise MonitorContractError("research candidate must originate from l5")
                current = card["current"]
                returns = (current.get("valuation") or {}).get("annual_returns") or {}
                expected = {
                    "close": current["last_close"], "close_date": current["last_close_date"],
                    "zone": current["current_zone"], "neutral_return": returns.get("中性"),
                    "conservative_return": returns.get("保守"), "optimistic_return": returns.get("乐观"),
                }
                if any(row[field] != number for field, number in expected.items()):
                    raise MonitorContractError("research current projection is inconsistent")
            elif any(row[field] is not None for field in _RESEARCH_ROW_FIELDS - {"code", "card"}):
                raise MonitorContractError("missing research card requires null projection")
            for field in ("close", "neutral_return", "conservative_return", "optimistic_return"):
                _research_number(row[field])
            for field in ("zone", "note"):
                _optional_string(row[field], f"research.row.{field}")
            # Added in 0.1.31; earlier snapshots without it remain readable.
            if "assumption" in row:
                _optional_string(row["assumption"], "research.row.assumption")
                source = ((row["card"] or {}).get("current") or {}).get("valuation") or {}
                if row["assumption"] != source.get("assumption"):
                    raise MonitorContractError("research assumption projection is inconsistent")
            _research_date(row["close_date"])
            _research_stance(row)
        if len(codes) != len(set(codes)) or (
            group == "holdings" and set(codes) != holding_codes
            or group == "candidates" and set(codes) & holding_codes
        ):
            raise MonitorContractError("research holding/candidate scope is inconsistent")
    for code in _array(research["missing"], "research.missing"):
        _research_code(code)
    rows = {row["code"]: row for group in ("holdings", "candidates") for row in research[group]}
    candidate_codes = {row["code"] for row in research["candidates"]}
    # Candidates stay researched on any board; advice may only name boards the account can trade.
    advisable_codes = {code for code in candidate_codes if boards.advisable(code, untradable)}
    for comparison in _array(research.get("opportunity_costs", []), "research.opportunity_costs"):
        candidate = comparison.get("candidate") if isinstance(comparison, dict) else None
        if isinstance(candidate, dict) and candidate.get("code") in candidate_codes - advisable_codes:
            raise MonitorContractError("opportunity_cost names a board the account cannot trade")
        _opportunity_evidence(comparison, holding_codes, advisable_codes, rows)
    for raw in _array(value["review_prompts"], "review_prompts"):
        prompt = _object(raw, "review_prompt")
        _required(prompt, {"code", "kind", "detail", "evidence"}, "review_prompt")
        kind = _supported(prompt["kind"], REVIEW_PROMPT_KINDS, "review_prompt.kind")
        _nonempty(prompt["detail"], "review_prompt.detail")
        evidence = _object(prompt["evidence"], "review_prompt.evidence")
        if kind == "research_unavailable":
            if prompt["code"] is not None or status != "unavailable":
                raise MonitorContractError("research_unavailable requires unavailable research")
        elif kind == "research_opportunity":
            if prompt["code"] not in candidate_codes or status != "ok":
                raise MonitorContractError("research_opportunity requires a researched candidate")
            if prompt["code"] not in advisable_codes:
                raise MonitorContractError("research_opportunity names a board the account cannot trade")
            _review_evidence_row(evidence)
            row = next(r for r in research["candidates"] if r["code"] == prompt["code"])
            fields = ("code", "report_id", "close_date", "close", *_RESEARCH_STANCE_FIELDS)
            if "stance_add_trigger" in row:
                fields = (*fields, "stance_add_trigger")
            _review_cautions(evidence, row["card"])
            expected = {field: row[field] for field in fields if field in row} | research_cautions(row["card"])
            if any(field not in row for field in fields) or evidence != expected:
                raise MonitorContractError("research_opportunity evidence is inconsistent")
            # Date-free part of the runtime trigger; the freshness/validity dates are runtime-only.
            if (row["card_status"] != "有效" or row["verdict"] != "值得继续研究" or row["stance_action"] == "回避"
                    or not (row["v2_zone"] == "测算支持区" and row["expected_return"] is not None
                            or row["stance_action"] in {"现在分批", "等证据"}
                            or row["stance_action"] == "等价格" and row["stance_price"] is not None
                            and row["close"] is not None and row["close"] <= row["stance_price"])):
                raise MonitorContractError("research_opportunity candidate is not eligible")
        elif kind == "opportunity_cost" and "opportunity_costs" in research:
            if prompt["code"] not in candidate_codes or status != "ok":
                raise MonitorContractError("opportunity_cost requires a researched candidate")
            if prompt["code"] not in advisable_codes:
                raise MonitorContractError("opportunity_cost names a board the account cannot trade")
            comparisons = _array(evidence.get("comparisons"), "opportunity_cost.comparisons")
            if not comparisons:
                raise MonitorContractError("opportunity_cost requires comparisons")
            for comparison in comparisons:
                _opportunity_evidence(comparison, holding_codes, {prompt["code"]}, rows)
                if comparison not in research["opportunity_costs"]:
                    raise MonitorContractError("opportunity_cost comparison missing from research")
        else:
            if prompt["code"] not in holding_codes or status != "ok":
                raise MonitorContractError("review prompt requires an active researched holding")
            if kind == "opportunity_cost":  # Pre-0.1.30 snapshots remain readable.
                _opportunity_evidence(evidence, holding_codes, advisable_codes, rows)
            else:
                _review_evidence_row(evidence)
                _review_cautions(evidence, rows[prompt["code"]]["card"])


def _research_stance(row: dict) -> None:
    if "stance_add_trigger" in row:
        frozen = (row["card"] or {}).get("stance_calc") or {}
        if row["stance_add_trigger"] is not None:
            _research_trigger(row["stance_add_trigger"])
        if row["stance_add_trigger"] != stance_add_trigger(frozen):
            raise MonitorContractError("research add_trigger projection is inconsistent")
    present = [field in row for field in _RESEARCH_STANCE_FIELDS]
    if not any(present):
        return
    if not all(present):
        raise MonitorContractError("research stance projection is incomplete")
    card = row["card"] or {}
    frozen = card.get("stance_calc") or {}
    v2 = ((card.get("current") or {}).get("valuation") or {}).get("v2") or {}
    expected = {"stance_action": frozen.get("action"), "stance_price": frozen.get("price"),
                "stance_valid_until": frozen.get("valid_until"),
                "expected_return": v2.get("annual_return"), "v2_zone": v2.get("zone")}
    if any(row[field] != value for field, value in expected.items()):
        raise MonitorContractError("research stance projection is inconsistent")


def _review_evidence_row(raw: Any) -> dict:
    item = _object(raw, "review_prompt.evidence.row")
    _required(item, {"code", "report_id", "close_date", "close"}, "review_prompt.evidence.row")
    _research_code(item["code"])
    if type(item["report_id"]) is not int or item["report_id"] <= 0:
        raise MonitorContractError("review_prompt report_id must be a positive integer")
    _research_date(item["close_date"])
    _research_number(item["close"])
    return item


def _opportunity_evidence(raw: Any, holding_codes: set[str], candidate_codes: set[str], rows: dict) -> None:
    evidence = _object(raw, "opportunity_cost.evidence")
    numbers = {"holding_neutral_return", "candidate_neutral_return", "candidate_horizon",
               "annual_switch_cost", "net_advantage"}
    _required(evidence, {"holding", "candidate"} | numbers, "opportunity_cost.evidence")
    for field, codes in (("holding", holding_codes), ("candidate", candidate_codes)):
        code = _review_evidence_row(evidence[field])["code"]
        if code not in codes:
            raise MonitorContractError("opportunity_cost evidence scope is inconsistent")
        _review_cautions(evidence[field], rows[code]["card"])
    for field in numbers:
        _research_number(evidence[field])
        if evidence[field] is None:
            raise MonitorContractError("opportunity_cost evidence requires numbers")


def validate_monitor_snapshot(payload: object) -> dict[str, Any]:
    """Validate monitor-v1 without discarding additive fields."""
    value = _object(payload, "snapshot")
    _reject_nonfinite(value)
    missing = sorted(_REQUIRED_FIELDS - value.keys())
    if missing:
        raise MonitorContractError(f"missing required field: {missing[0]}")

    version = value["schema_version"]
    if type(version) is not int or version != 1:
        raise MonitorContractError("schema_version must be integer 1")
    _iso8601(value["as_of"], "as_of", aware=True)
    _nonempty(value["runtime_version"], "runtime_version")
    data_status = _supported(value["data_status"], DATA_STATUSES, "data_status")
    _supported(value["valuation_status"], VALUATION_STATUSES, "valuation_status")
    _supported(value["review_status"], REVIEW_STATUSES, "review_status")
    action_status = _supported(value["action_status"], ACTION_STATUSES, "action_status")
    account = _object(value["account"], "account")
    _required(
        account,
        {
            "portfolio_value",
            "denominator_status",
            "stock_market_value",
            "stock_weight_pct",
            "total_stop_risk",
            "total_stop_risk_pct",
            "known_stop_risk_lower_bound",
            "risk_budget_status",
            "risk_policy",
        },
        "account",
    )
    for field in (
        "portfolio_value",
        "stock_market_value",
        "stock_weight_pct",
        "total_stop_risk",
        "total_stop_risk_pct",
        "known_stop_risk_lower_bound",
    ):
        number = account[field]
        if number is not None and (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or number < 0
            or field == "portfolio_value"
            and number == 0
        ):
            raise MonitorContractError(
                f"account.{field} must be a valid number or null"
            )
    denominator_status = _supported(
        account["denominator_status"],
        frozenset({"explicit", "missing"}),
        "account.denominator_status",
    )
    if (account["portfolio_value"] is not None) != (denominator_status == "explicit"):
        raise MonitorContractError("account denominator evidence is inconsistent")
    budget_status = account["risk_budget_status"]
    if budget_status is not None:
        _supported(
            budget_status,
            frozenset({"within_budget", "over_budget"}),
            "risk_budget_status",
        )
        if account["portfolio_value"] is None:
            raise MonitorContractError("risk budget requires an explicit denominator")
    if budget_status == "within_budget" and (
        account["total_stop_risk"] is None or account["total_stop_risk_pct"] is None
    ):
        raise MonitorContractError("within_budget requires complete risk totals")
    if (
        account["total_stop_risk"] is None
        and account["total_stop_risk_pct"] is not None
    ):
        raise MonitorContractError(
            "incomplete risk total cannot have a complete percentage"
        )
    if account["total_stop_risk"] is not None and (
        account["known_stop_risk_lower_bound"] != account["total_stop_risk"]
    ):
        raise MonitorContractError(
            "complete risk total must match its known lower bound"
        )
    policy = _object(account["risk_policy"], "account.risk_policy")
    _required(
        policy,
        {
            "policy_id",
            "source",
            "max_position_risk_pct",
            "max_portfolio_risk_pct",
            "field_sources",
        },
        "account.risk_policy",
    )
    _nonempty(policy["policy_id"], "risk_policy.policy_id")
    _nonempty(policy["source"], "risk_policy.source")
    sources = _object(policy["field_sources"], "risk_policy.field_sources")
    not_applied = policy.get("status") == "not_applied_account_scope_required"
    if not_applied and (
        policy["source"] != "not_applied_account_scope_required"
        or account.get("account_scope") is not None
        or account.get("denominator_requires_review") is not True
        or budget_status is not None
        or account["stock_weight_pct"] is not None
        or account["total_stop_risk_pct"] is not None
    ):
        raise MonitorContractError("unapplied risk_policy has inconsistent account evidence")
    for field in ("max_position_risk_pct", "max_portfolio_risk_pct"):
        number = policy[field]
        if not_applied:
            if number is not None or sources.get(field) != policy["source"]:
                raise MonitorContractError(f"unapplied risk_policy.{field} must be null")
        elif (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or number <= 0
        ):
            raise MonitorContractError(f"risk_policy.{field} must be positive")
        _nonempty(sources.get(field), f"risk_policy.field_sources.{field}")
    # Added in 0.1.36; absent means every board is tradable.
    if "untradable_boards" in policy:
        boards_value = policy["untradable_boards"]
        if (not isinstance(boards_value, list) or any(b not in boards.RESTRICTABLE for b in boards_value)
                or len(set(boards_value)) != len(boards_value)
                or sources.get("untradable_boards") != "policy_file"):
            raise MonitorContractError("risk_policy.untradable_boards is invalid")

    coverage = _object(value["quote_coverage"], "quote_coverage")
    _required(coverage, {"priced", "active", "complete"}, "quote_coverage")
    for field in ("priced", "active"):
        number = coverage[field]
        if type(number) is not int or number < 0:
            raise MonitorContractError(
                f"quote_coverage.{field} must be a non-negative integer"
            )
    if type(coverage["complete"]) is not bool:
        raise MonitorContractError("quote_coverage.complete must be a boolean")
    if coverage["priced"] > coverage["active"]:
        raise MonitorContractError("quote_coverage.priced cannot exceed active")
    if coverage["complete"] and coverage["priced"] != coverage["active"]:
        raise MonitorContractError("complete quote coverage requires priced == active")

    industry = _object(value["industry_context"], "industry_context")
    _required(
        industry,
        {"status", "error_code", "freshness_days", "source", "fetched_at"},
        "industry_context",
    )
    industry_status = _supported(
        industry["status"], INDUSTRY_CONTEXT_STATUSES, "industry_context.status"
    )
    _optional_string(industry["error_code"], "industry_context.error_code")
    _optional_string(industry["source"], "industry_context.source")
    freshness = industry["freshness_days"]
    if freshness is not None and (type(freshness) is not int or freshness < 0):
        raise MonitorContractError(
            "industry_context.freshness_days must be a non-negative integer or null"
        )
    if industry["fetched_at"] is not None:
        _iso8601(industry["fetched_at"], "industry_context.fetched_at")
    if industry_status == "complete" and industry["error_code"] is not None:
        raise MonitorContractError("complete industry_context cannot have error_code")

    holdings = _array(value["holdings"], "holdings")
    if coverage["active"] != len(holdings):
        raise MonitorContractError("quote_coverage.active must match holdings count")
    holding_fields = {"id", "code", "name", "framework", "framework_confident", "quote"}
    for index, raw in enumerate(holdings):
        holding = _object(raw, f"holdings[{index}]")
        _required(holding, holding_fields, f"holdings[{index}]")
        if type(holding["id"]) is not int or holding["id"] <= 0:
            raise MonitorContractError(
                f"holdings[{index}].id must be a positive integer"
            )
        if (
            not isinstance(holding["code"], str)
            or re.fullmatch(r"\d{6}", holding["code"]) is None
        ):
            raise MonitorContractError(
                f"holdings[{index}].code must be a six-digit string"
            )
        _nonempty(holding["name"], f"holdings[{index}].name")
        _nonempty(holding["framework"], f"holdings[{index}].framework")
        if type(holding["framework_confident"]) is not bool:
            raise MonitorContractError(
                f"holdings[{index}].framework_confident must be a boolean"
            )
        _object(holding["quote"], f"holdings[{index}].quote")
        for field in (
            "market_value",
            "portfolio_weight_pct",
            "stop_risk",
            "stop_risk_pct",
        ):
            number = holding.get(field)
            if number is not None and (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or number < 0
            ):
                raise MonitorContractError(
                    f"holdings[{index}].{field} must be non-negative or null"
                )
            if budget_status == "within_budget" and number is None:
                raise MonitorContractError(
                    "within_budget requires complete holding risk evidence"
                )

    _validate_research_fields(value, {holding["code"] for holding in holdings},
                              account["risk_policy"].get("untradable_boards"))

    escalations = _array(value["escalations"], "escalations")
    for index, raw in enumerate(escalations):
        item = _object(raw, f"escalations[{index}]")
        _required(
            item,
            {"code", "reason_code", "candidate", "detail"},
            f"escalations[{index}]",
        )
        if item["code"] is not None and (
            not isinstance(item["code"], str)
            or re.fullmatch(r"\d{6}", item["code"]) is None
        ):
            raise MonitorContractError(
                f"escalations[{index}].code must be six digits or null"
            )
        _supported(
            item["reason_code"],
            ESCALATION_REASON_CODES,
            f"escalations[{index}].reason_code",
        )
        _supported(
            item["candidate"],
            frozenset({"review", "trade"}),
            f"escalations[{index}].candidate",
        )
        _nonempty(item["detail"], f"escalations[{index}].detail")

    thesis_reviews = [item for item in escalations if item["reason_code"] == "research_thesis_review"]
    pending_codes = {
        row["code"] for row in value.get("research", {}).get("holdings", [])
        if row["card_status"] == "待复核"
    }
    if (len(thesis_reviews) != len(pending_codes)
            or {item["code"] for item in thesis_reviews} != pending_codes
            or any(item["candidate"] != "review" for item in thesis_reviews)
            or thesis_reviews and value["review_status"] == "cleared"):
        raise MonitorContractError("research thesis reviews require pending holding cards and review status")

    gaps = _array(value["data_gaps"], "data_gaps")
    for index, raw in enumerate(gaps):
        item = _object(raw, f"data_gaps[{index}]")
        _required(item, {"code", "field", "minimum_action"}, f"data_gaps[{index}]")
        if item["code"] is not None and (
            not isinstance(item["code"], str)
            or re.fullmatch(r"\d{6}", item["code"]) is None
        ):
            raise MonitorContractError(
                f"data_gaps[{index}].code must be six digits or null"
            )
        _nonempty(item["field"], f"data_gaps[{index}].field")
        _nonempty(item["minimum_action"], f"data_gaps[{index}].minimum_action")

    stop_reason = value["stop_reason"]
    if stop_reason is not None and (
        not isinstance(stop_reason, str) or stop_reason != "clean_fast_gate"
    ):
        raise MonitorContractError("stop_reason is unsupported")
    confirmation = value["requires_user_confirmation"]
    if type(confirmation) is not bool:
        raise MonitorContractError("requires_user_confirmation must be a boolean")
    manifest = _object(value["manifest"], "manifest")
    _required(manifest, {"writes", "stop_reason"}, "manifest")
    if type(manifest["writes"]) is not bool or manifest["writes"]:
        raise MonitorContractError("manifest.writes must be false")
    if manifest["stop_reason"] != stop_reason:
        raise MonitorContractError("manifest.stop_reason must match stop_reason")

    budget_escalations = [
        item for item in escalations if item["reason_code"] == "risk_budget_exceeded"
    ]
    if any(
        item["code"] is not None
        and item["code"] not in {holding["code"] for holding in holdings}
        for item in budget_escalations
    ):
        raise MonitorContractError(
            "position budget escalation requires an active holding code"
        )
    if len({item["code"] for item in budget_escalations}) != len(budget_escalations):
        raise MonitorContractError("duplicate risk budget escalation scope/code")
    if budget_escalations:
        known = account["known_stop_risk_lower_bound"]
        if known is None or known <= 0:
            raise MonitorContractError(
                "budget escalation requires a positive known risk lower bound"
            )
        for escalation in budget_escalations:
            if not any(
                (escalation["code"] is None or item["code"] == escalation["code"])
                and item.get("stop_risk") is not None
                and item["stop_risk"] > 0
                for item in holdings
            ):
                raise MonitorContractError(
                    "budget escalation requires corresponding holding risk evidence"
                )
    if budget_escalations and (
        budget_status != "over_budget"
        or any(item["candidate"] != "review" for item in budget_escalations)
    ):
        raise MonitorContractError(
            "budget escalations require over_budget and review candidates"
        )
    if budget_status == "over_budget" and (
        not budget_escalations
        or value["review_status"] == "cleared"
        or action_status == "no_action"
        or stop_reason == "clean_fast_gate"
    ):
        raise MonitorContractError(
            "over_budget cannot clear review or produce no_action/clean"
        )
    if (
        holdings
        and budget_status is None
        and (value["review_status"] == "cleared" or action_status == "no_action")
    ):
        raise MonitorContractError("unknown holding risk budget cannot clear review")
    if stop_reason == "clean_fast_gate" and (
        budget_status != "within_budget"
        or account["portfolio_value"] is None
        or holdings
        and (
            account["stock_market_value"] is None
            or account["stock_weight_pct"] is None
            or any(
                item.get(field) is None
                for item in holdings
                for field in (
                    "market_value",
                    "stop_risk",
                    "stop_risk_pct",
                    "portfolio_weight_pct",
                )
            )
        )
    ):
        raise MonitorContractError(
            "clean_fast_gate requires complete budget safety evidence"
        )

    if (
        action_status == "no_action" or value["review_status"] == "cleared"
    ) and stop_reason != "clean_fast_gate":
        raise MonitorContractError(
            "cleared/no_action requires clean_fast_gate safety evidence"
        )

    if data_status != "complete" and action_status == "no_action":
        raise MonitorContractError("incomplete data cannot produce no_action")
    if action_status == "trade_candidate" and not any(
        item["candidate"] == "trade" for item in escalations
    ):
        raise MonitorContractError("trade_candidate requires a trade escalation")
    if confirmation != (action_status == "trade_candidate"):
        raise MonitorContractError(
            "requires_user_confirmation does not match action_status"
        )
    if (
        industry_status in {"stale", "invalid", "unavailable"}
        and holdings
        and not any(item["field"] == "industry_context" for item in gaps)
    ):
        raise MonitorContractError("degraded industry_context requires a data gap")
    if stop_reason == "clean_fast_gate":
        for holding in holdings:
            quote = holding["quote"]
            if quote.get("status") != "fresh":
                raise MonitorContractError(
                    "clean_fast_gate requires fresh holding quotes"
                )
            for field, number in (
                ("price", quote.get("price")),
                ("stop_loss_20", holding.get("stop_loss_20")),
            ):
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or number <= 0
                ):
                    raise MonitorContractError(
                        f"clean_fast_gate requires valid {field}"
                    )
            if type(holding.get("shares")) is not int or holding["shares"] <= 0:
                raise MonitorContractError("clean_fast_gate requires valid shares")
            _nonempty(quote.get("source"), "clean quote.source")
            _iso8601(quote.get("as_of"), "clean quote.as_of")
    if stop_reason == "clean_fast_gate" and (
        value["data_status"] != "complete"
        or value["valuation_status"] != "exact"
        or value["review_status"] != "cleared"
        or action_status != "no_action"
        or escalations
        or gaps
        or not coverage["complete"]
    ):
        raise MonitorContractError("invalid clean_fast_gate")
    return value


def loads_monitor_snapshot(text: str) -> dict[str, Any]:
    """Parse and validate one monitor-v1 snapshot."""
    try:
        payload = json.loads(
            text,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                MonitorContractError(f"all numbers must be finite ({constant})")
            ),
        )
    except MonitorContractError:
        raise
    except (TypeError, json.JSONDecodeError) as exc:
        raise MonitorContractError(f"invalid monitor JSON: {exc}") from exc
    return validate_monitor_snapshot(payload)


def dumps_monitor_snapshot(payload: object) -> str:
    """Validate and emit deterministic monitor-v1 JSON."""
    return json.dumps(
        validate_monitor_snapshot(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
