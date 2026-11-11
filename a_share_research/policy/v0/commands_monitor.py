"""Structured L3, tier, and alert commands."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

from a_share_research.core.market_data import (
    CACHE_CORRUPT,
    CACHE_FUTURE_TIMESTAMP,
    CACHE_MALFORMED,
    CACHE_STALE,
    MarketDataResult,
)

from a_share_research.policy.v0 import __version__ as _V0_VERSION
from a_share_research.policy.v0 import (
    commands_holdings,
    db,
    domain,
    monitor_contract,
    monitor_research,
    risk_budget,
    risk_gates,
)

from a_share_research.policy.v0.risk_policy import RiskParameters

_SCOPES = {"aggregate", "core_driver", "non_core", "governance"}
_ACTIONS = {"review", "reduce", "exit"}
_EVIDENCE_CADENCES = {"quarterly", "semiannual", "annual", "event", "unspecified"}


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


_ESCALATION_REASON_ORDER = (
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
)
_REASON_ORDER = {reason: index for index, reason in enumerate(_ESCALATION_REASON_ORDER)}


def _finite(value: Any) -> bool:
    return risk_budget.finite_number(value)


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _runtime_version() -> str:
    return _V0_VERSION


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _due(value: Any, today: date) -> bool | None:
    if value in (None, ""):
        return False
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10]) <= today
    except ValueError:
        return None


def _load_monitor_local_snapshot() -> dict[str, Any]:
    """Read local inputs in one transaction; release it before fetching quotes."""
    with db.read_only_db_session() as conn, conn:
        if not conn.in_transaction:
            conn.execute("BEGIN DEFERRED")
        holding_rows = conn.execute(
            """SELECT h.id, h.code, h.name, h.cost_price, h.shares, h.buy_date,
                      h.stop_loss_15, h.stop_loss_20, h.framework,
                      h.framework_confident, h.reference_cost,
                      f.data, f.updated_at, f.ttl_hours,
                      t.tier1_status, t.tier2_status, t.tier3_status,
                      t.exit_path, t.exit_target_pct, t.exemption_framework,
                      t.updated_at
               FROM holdings h
               LEFT JOIN stock_fundamentals f ON f.code=h.code
               LEFT JOIN holding_tier_state t ON t.holding_id=h.id
               WHERE h.exit_date IS NULL ORDER BY h.code, h.id"""
        ).fetchall()
        closed_count = conn.execute(
            "SELECT COUNT(*) FROM holdings WHERE exit_date IS NOT NULL"
        ).fetchone()[0]
        alert_rows = conn.execute(
            """SELECT a.holding_id, a.level, a.category, a.reason_code, a.status,
                      a.reason, a.review_due, a.evidence, a.opened_at, a.updated_at
               FROM holding_alerts a JOIN holdings h ON h.id=a.holding_id
               WHERE h.exit_date IS NULL AND a.status!='resolved'
               ORDER BY a.holding_id, a.opened_at, a.id"""
        ).fetchall()
        thesis_rows = conn.execute(
            """SELECT t.holding_id, t.id, t.version, t.l1, t.l2, t.created_at
               FROM holding_thesis_versions t
               JOIN holdings h ON h.id=t.holding_id
               WHERE h.exit_date IS NULL AND t.status='active'
               ORDER BY t.holding_id, t.id"""
        ).fetchall()
        l3_rows = conn.execute(
            """SELECT l.holding_id, l.id, l.thesis_version_id, l.status,
                      l.condition_text, l.as_of, l.next_review_date, l.evidence,
                      l.temporary_exit_rule, l.condition_scope, l.action_level,
                      l.materiality_basis, l.updated_at, l.evidence_cadence
               FROM holding_l3_conditions l
               JOIN holdings h ON h.id=l.holding_id
               WHERE h.exit_date IS NULL AND l.is_active=1
               ORDER BY l.holding_id, l.id"""
        ).fetchall()

    alerts: dict[int, list[dict[str, Any]]] = {}
    for row in alert_rows:
        alerts.setdefault(row[0], []).append(
            dict(
                zip(
                    (
                        "level",
                        "category",
                        "reason_code",
                        "status",
                        "reason",
                        "review_due",
                        "evidence",
                        "opened_at",
                        "updated_at",
                    ),
                    row[1:],
                    strict=True,
                )
            )
        )
    theses: dict[int, list[dict[str, Any]]] = {}
    for row in thesis_rows:
        theses.setdefault(row[0], []).append(
            {
                "id": row[1],
                "version": row[2],
                "l1": row[3],
                "l2": row[4],
                "created_at": row[5],
            }
        )
    conditions: dict[int, list[dict[str, Any]]] = {}
    for row in l3_rows:
        conditions.setdefault(row[0], []).append(
            dict(
                zip(
                    (
                        "id",
                        "thesis_version_id",
                        "status",
                        "condition",
                        "as_of",
                        "next_review",
                        "evidence",
                        "temporary_exit_rule",
                        "scope",
                        "action",
                        "materiality_basis",
                        "updated_at",
                        "evidence_cadence",
                    ),
                    row[1:],
                    strict=True,
                )
            )
        )
    holdings = []
    for row in holding_rows:
        try:
            fundamentals = (
                json.loads(row[11], parse_constant=_reject_json_constant)
                if row[11]
                else None
            )
            malformed_fundamentals = row[11] is not None and not isinstance(
                fundamentals, dict
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            fundamentals = None
            malformed_fundamentals = True
        holdings.append(
            {
                "id": row[0],
                "code": row[1],
                "name": row[2],
                "cost_price": _number(row[3]),
                "shares": row[4],
                "buy_date": row[5],
                "stop_loss_15": _number(row[6]),
                "stop_loss_20": _number(row[7]),
                "framework": row[8],
                "framework_confident": bool(row[9]),
                "reference_cost": _number(row[10]),
                "fundamentals": fundamentals,
                "fundamentals_updated_at": row[12],
                "fundamentals_ttl_hours": row[13],
                "fundamentals_malformed": malformed_fundamentals,
                "tier": {
                    "tier1_status": row[14],
                    "tier2_status": row[15],
                    "tier3_status": row[16],
                    "exit_path": row[17],
                    "exit_target_pct": _number(row[18]),
                    "exemption_framework": row[19],
                    "updated_at": row[20],
                },
                "alerts": alerts.get(row[0], []),
                "theses": theses.get(row[0], []),
                "l3_conditions": conditions.get(row[0], []),
            }
        )
    return {"holdings": holdings, "closed_count": closed_count}


def _l3_view(holding: dict[str, Any], today: date) -> tuple[dict, list[str], bool]:
    theses = holding["theses"]
    conditions = holding["l3_conditions"]
    gaps: list[str] = []
    unresolved = False
    if not theses:
        contract = "legacy"
        gaps.append("active thesis version missing")
        unresolved = True
    elif len(theses) != 1 or not conditions:
        contract = "invalid"
        gaps.append("active L3 contract incomplete")
        unresolved = True
    else:
        contract = "versioned"
        thesis_id = theses[0]["id"]
        for condition in conditions:
            valid = (
                condition["thesis_version_id"] == thesis_id
                and condition["scope"] in _SCOPES
                and condition["action"] in _ACTIONS
                and not (
                    condition["scope"] == "non_core" and condition["action"] != "review"
                )
                and bool(condition["materiality_basis"])
                and bool(condition["temporary_exit_rule"])
                and bool(condition["as_of"])
                and bool(condition["evidence"])
            )
            condition["valid_for_action"] = valid
            if not valid:
                contract = "invalid"
                gaps.append(f"L3 condition {condition['id']} contract invalid")
                unresolved = True
            # A verified watch row is resolved evidence with heightened risk; it is
            # reported separately as l3_watch instead of making the data partial.
            if condition["status"] == "pending":
                unresolved = True
            due = _due(condition["next_review"], today)
            condition["review_due"] = due
            if due is None:
                gaps.append(f"L3 condition {condition['id']} review date invalid")
                unresolved = True
    return (
        {
            "contract": contract,
            "thesis": theses[0] if len(theses) == 1 else None,
            "conditions": conditions,
        },
        gaps,
        unresolved,
    )


def _add_escalation(
    escalations: list[dict[str, Any]],
    code: str | None,
    reason_code: str,
    detail: str,
    *,
    candidate: str = "review",
    evidence_cadence: str | None = None,
) -> None:
    item = {
        "code": code,
        "reason_code": reason_code,
        "candidate": candidate,
        "detail": detail,
    }
    if evidence_cadence is not None:
        item["evidence_cadence"] = evidence_cadence
    if item not in escalations:
        escalations.append(item)


def _add_gap(
    gaps: list[dict[str, Any]], code: str | None, field: str, action: str
) -> None:
    item = {"code": code, "field": field, "minimum_action": action}
    if item not in gaps:
        gaps.append(item)


def _industry_context(
    result: MarketDataResult[dict[str, str]] | None,
) -> dict[str, Any]:
    error_code = getattr(result, "error_code", None)
    status = (
        "not_applicable"
        if result is None
        else "complete"
        if result.status == "ok"
        else "stale"
        if error_code == CACHE_STALE
        else "invalid"
        if error_code in {CACHE_CORRUPT, CACHE_MALFORMED, CACHE_FUTURE_TIMESTAMP}
        else "unavailable"
    )
    return {
        "status": status,
        "error_code": error_code,
        "freshness_days": getattr(result, "freshness_days", None),
        "source": getattr(result, "source", None),
        "fetched_at": getattr(result, "fetched_at", None),
    }


def _denominator_source(portfolio_value, denominator_evidence) -> str | None:
    components = (denominator_evidence or {}).get("denominator_components") or {}
    if portfolio_value is None:
        return None
    source = (denominator_evidence or {}).get("portfolio_value_source", "cli")
    return source + (
        "+policy_file" if components.get("off_account_strategy_cash") else ""
    )


def build_monitor_snapshot(
    local: dict[str, Any],
    quotes: dict[str, commands_holdings.PriceQuote | None],
    portfolio_value: float | None,
    *,
    industry_result: MarketDataResult[dict[str, str]] | None = None,
    now: datetime | None = None,
    stage_timings_ms: dict[str, int] | None = None,
    risk_policy: dict | None = None,
    denominator_evidence: dict | None = None,
    account_scope_required: bool = False,
    research_result: dict | None = None,
) -> dict[str, Any]:
    """Purely combine one local snapshot and one shared quote snapshot."""
    portfolio_value = portfolio_value if risk_budget.positive_number(portfolio_value) else None
    limits = risk_policy if risk_policy is not None else risk_budget.policy()
    current = now or domain.cst_now()
    today = current.date()
    today_text = today.isoformat()
    holdings = local["holdings"]
    escalations: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    output_holdings: list[dict[str, Any]] = []
    risk_value = None if account_scope_required else portfolio_value
    partial = risk_value is None
    stale = False
    conflicted = False
    trade_requested = False
    industry_context = _industry_context(industry_result)

    if holdings and industry_context["status"] in {"unavailable", "stale", "invalid"}:
        stale = stale or industry_context["status"] == "stale"
        partial = True
        error_code = industry_context["error_code"] or "unknown"
        _add_gap(
            gaps,
            None,
            "industry_context",
            f"restore same-industry context ({error_code})",
        )

    denominator_details = []
    if portfolio_value is None:
        denominator_details.append("explicit portfolio value is required for weights and risk budgets")
        _add_gap(gaps, None, "portfolio_value", "provide --portfolio-value")
    if account_scope_required:
        denominator_details.append(
            "account scope is unconfirmed; policy not applied; weights and risk budgets unavailable"
        )
        _add_gap(gaps, None, "account_scope", "confirm and provide --account-scope")
    if denominator_details:
        _add_escalation(
            escalations, None, "denominator_missing", "; ".join(denominator_details)
        )

    duplicate_codes = sorted(
        {
            item["code"]
            for item in holdings
            if sum(h["code"] == item["code"] for h in holdings) > 1
        }
    )
    for code in duplicate_codes:
        conflicted = True
        _add_escalation(
            escalations,
            code,
            "data_conflict",
            "multiple active holding rows for one code",
        )
        _add_gap(gaps, code, "active_holding", "reconcile the active lifecycle")

    priced = 0
    valued = 0
    total_market_value = 0.0
    for holding in holdings:
        code = holding["code"]
        required_fields = {
            "shares": risk_budget.valid_shares(holding["shares"]),
            "framework": bool(holding["framework"] and holding["framework_confident"]),
            "stop_loss_15": _finite(holding["stop_loss_15"])
            and holding["stop_loss_15"] > 0,
            "stop_loss_20": _finite(holding["stop_loss_20"])
            and holding["stop_loss_20"] > 0,
        }
        for field, valid in required_fields.items():
            if not valid:
                partial = True
                _add_gap(gaps, code, field, f"reconcile holding {field}")

        quote = quotes.get(code)
        quote_valid = risk_budget.valid_quote(quote, today_text)
        if quote and quote.conflicted:
            conflicted = True
            _add_escalation(
                escalations, code, "data_conflict", "quote sources conflict"
            )
            _add_gap(gaps, code, "quote", "resolve quote source conflict")
        elif quote and quote.quote_date and quote.quote_date != today_text:
            stale = True
            _add_escalation(escalations, code, "quote_gap", "quote is stale")
            _add_gap(gaps, code, "quote", "refresh the current quote")
        elif not quote_valid:
            partial = True
            _add_escalation(
                escalations, code, "quote_gap", "quote or timestamp unavailable"
            )
            _add_gap(gaps, code, "quote", "retrieve a fresh timestamped quote")
        current_price = float(quote.price) if quote_valid and quote else None
        if quote_valid:
            priced += 1

        risk = commands_holdings.calculate_position_risk(
            holding["shares"] if required_fields["shares"] and code not in duplicate_codes else None,
            current_price,
            holding["stop_loss_20"] if required_fields["stop_loss_20"] else None,
            risk_value,
            holding["reference_cost"],
        )
        if risk["market_value"] is not None:
            valued += 1
            total_market_value += risk["market_value"]
        if risk["stop_risk"] is None or (
            risk_value is not None and risk["stop_risk_pct"] is None
        ):
            partial = True
            _add_gap(gaps, code, "stop_risk", "restore valid risk inputs")

        previous_close = _number(quote.previous_close) if quote else None
        if previous_close is None or previous_close <= 0:
            previous_close = None
            partial = True
            _add_gap(gaps, code, "previous_close", "retrieve a valid previous close")
        if not quote_valid:
            previous_close = None
        daily_change_pct = (
            (current_price - previous_close) / previous_close * 100
            if current_price is not None
            and previous_close is not None
            and previous_close > 0
            else None
        )
        industry_change_pct = (
            quote.industry_change_pct
            if quote_valid and quote and _finite(quote.industry_change_pct)
            else None
        )
        if quote_valid and industry_change_pct is None:
            partial = True
            _add_gap(
                gaps,
                code,
                "industry_change_pct",
                "supply a same-day industry comparison",
            )
        relative_change_pct = (
            daily_change_pct - industry_change_pct
            if daily_change_pct is not None and industry_change_pct is not None
            else None
        )

        p3 = {"status": "not_applicable", "reason_code": "stop_loss_20_not_triggered"}
        if current_price is not None and required_fields["stop_loss_20"]:
            if current_price <= holding["stop_loss_20"]:
                _add_escalation(
                    escalations,
                    code,
                    "price_stop_2",
                    "current price reached the stored second stop line",
                    candidate="trade",
                )
                existing_defer = next(
                    (
                        alert
                        for alert in holding["alerts"]
                        if alert["reason_code"] == "price_stop2_liquidity_defer"
                    ),
                    None,
                )
                try:
                    defer_payload = (
                        json.loads(
                            existing_defer["evidence"],
                            parse_constant=_reject_json_constant,
                        )
                        if existing_defer and existing_defer["evidence"]
                        else None
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    defer_payload = None
                p3 = risk_gates.liquidity_shock_gate(
                    {
                        "stop_loss_triggered": True,
                        "stop_loss_20": holding["stop_loss_20"],
                        "quote": quote,
                        "existing_defer": defer_payload,
                        "now": current,
                    }
                )
                if not p3.get("action_eligible"):
                    partial = True
                    _add_gap(
                        gaps,
                        code,
                        "p3_liquidity_gate",
                        f"complete P3 review: {p3['reason_code']}",
                    )
                else:
                    trade_requested = True
        if (
            current_price is not None
            and required_fields["stop_loss_15"]
            and current_price <= holding["stop_loss_15"]
            and not (
                required_fields["stop_loss_20"]
                and current_price <= holding["stop_loss_20"]
            )
        ):
            _add_escalation(
                escalations,
                code,
                "price_stop_1",
                "current price reached the stored first stop line",
            )
        if daily_change_pct is not None and daily_change_pct <= -3:
            _add_escalation(
                escalations,
                code,
                "daily_drop",
                f"daily change {daily_change_pct:.2f}% is at or below -3%",
            )
        if relative_change_pct is not None and relative_change_pct <= -2:
            _add_escalation(
                escalations,
                code,
                "relative_underperformance",
                f"relative change {relative_change_pct:.2f}pts is at or below -2pts",
            )

        # Route existing Step 4 entrances only; valuation/report evidence is
        # not in this snapshot and must be reviewed before advancing a tier.
        tier = holding["tier"]
        framework = domain.FRAMEWORK_ALIASES.get(holding["framework"])
        exit_path = tier["exit_path"]
        tier_gap = None
        target_pct = None
        if exit_path is not None or framework in {"A通用", "E消费", "F科技"}:
            if (
                exit_path not in {None, "A", "B", "C"}
                or tier["tier1_status"] not in {"pending", "completed", "exempted"}
                or tier["tier2_status"] not in {"pending", "completed"}
                or tier["tier3_status"] not in {"pending", "completed"}
                or (exit_path is not None and tier["exemption_framework"] is not None)
            ):
                tier_gap = "reconcile structured Tier status and entry-time path configuration"
            elif exit_path == "A":
                tier_gap = "verify quarterly/interim L3 evidence and three-star timing for path A"
            elif exit_path == "B":
                target_pct = tier["exit_target_pct"]
                if not _finite(target_pct) or target_pct <= 0:
                    tier_gap = "restore the entry-time path B target"
            elif tier["tier1_status"] == "pending":
                target_pct = 25
            if target_pct is not None and tier_gap is None:
                reference_cost = holding["reference_cost"]
                if not _finite(reference_cost) or reference_cost <= 0:
                    tier_gap = "restore reference_cost for the configured Tier target"
                elif (
                    current_price is not None
                    and current_price >= reference_cost + reference_cost * target_pct / 100
                ):
                    _add_escalation(
                        escalations,
                        code,
                        "tier_review_due",
                        f"{'path ' + exit_path if exit_path else 'Tier1'} price target reached; review Step 4",
                    )
            if tier_gap is not None:
                partial = True
                _add_gap(gaps, code, "tier", tier_gap)
                _add_escalation(escalations, code, "tier_review_due", tier_gap)

        alert_output = []
        for alert in holding["alerts"]:
            due = _due(alert["review_due"], today)
            alert_output.append({**alert, "review_due_now": due})
            if due is None:
                partial = True
                _add_gap(gaps, code, "alert.review_due", "repair the alert review date")
            elif due:
                _add_escalation(
                    escalations,
                    code,
                    "alert_review_due",
                    f"alert {alert['reason_code']} is due",
                )

        l3, l3_gaps, l3_unresolved = _l3_view(holding, today)
        for gap in l3_gaps:
            partial = True
            _add_gap(gaps, code, "l3", gap)
        if l3_unresolved:
            partial = True
            _add_escalation(
                escalations,
                code,
                "l3_candidate",
                f"L3 state requires verification ({l3['contract']})",
            )
        for condition in l3["conditions"]:
            if condition.get("review_due"):
                _add_escalation(
                    escalations,
                    code,
                    "l3_review_due",
                    f"L3 condition {condition['id']} is due",
                    evidence_cadence=condition.get("evidence_cadence", "unspecified"),
                )
            if condition["status"] == "watch":
                _add_escalation(
                    escalations,
                    code,
                    "l3_watch",
                    f"L3 condition {condition['id']} is watch (as_of {condition['as_of']}, "
                    f"next_review {condition['next_review']}); no new risk",
                    evidence_cadence=condition.get("evidence_cadence", "unspecified"),
                )
            if condition["status"] == "triggered":
                candidate = (
                    "trade"
                    if condition.get("valid_for_action")
                    and condition["action"] in {"reduce", "exit"}
                    else "review"
                )
                trade_requested = trade_requested or candidate == "trade"
                _add_escalation(
                    escalations,
                    code,
                    "l3_candidate",
                    f"L3 condition {condition['id']} is triggered",
                    candidate=candidate,
                )

        fundamentals = holding["fundamentals"]
        gate_state = risk_gates.aggregate_fundamentals_gates(fundamentals)
        gate_updated_at = holding["fundamentals_updated_at"]
        gate_ttl = holding["fundamentals_ttl_hours"]
        gate_freshness_available = bool(
            isinstance(gate_updated_at, str)
            and gate_updated_at
            and _finite(gate_ttl)
            and gate_ttl >= 0
        )
        fundamentals_stale = gate_freshness_available and domain.is_expired(
            gate_updated_at, gate_ttl
        )
        if holding["fundamentals_malformed"]:
            partial = True
            _add_gap(
                gaps, code, "governance_gates", "repair malformed fundamentals JSON"
            )
        if not gate_freshness_available:
            partial = True
            _add_gap(
                gaps,
                code,
                "governance_gates",
                "restore risk-gate freshness metadata",
            )
        if fundamentals_stale:
            stale = True
            _add_gap(gaps, code, "governance_gates", "refresh expired governance gates")
        if gate_state is None or not gate_freshness_available or fundamentals_stale:
            gates = {}
            gate_status = "incomplete"
            gate_reasons = ["governance gates unavailable or stale"]
        else:
            gate_status, gate_reasons, gates = gate_state
        if gate_status != "clear":
            _add_escalation(
                escalations,
                code,
                "governance_gate",
                ", ".join(gate_reasons) or gate_status,
            )
            if gate_status == "incomplete":
                partial = True
                _add_gap(
                    gaps, code, "governance_gates", "refresh required gate evidence"
                )

        quote_output = {
            "status": (
                "conflicted"
                if quote and quote.conflicted
                else "fresh"
                if quote_valid
                else "stale"
                if quote and quote.quote_date and quote.quote_date != today_text
                else "unavailable"
            ),
            "price": current_price,
            "quote_date": quote.quote_date if quote else None,
            "quote_time": quote.quote_time if quote else None,
            "as_of": quote.quote_as_of if quote else None,
            "source": quote.source if quote else None,
            "previous_close": previous_close,
            "daily_change_pct": daily_change_pct,
            "industry_change_pct": industry_change_pct,
            "industry_source": quote.industry_source if quote else None,
            "industry_as_of": quote.industry_as_of if quote else None,
            "relative_change_pct": relative_change_pct,
        }
        output_holdings.append(
            {
                "id": holding["id"],
                "code": code,
                "name": holding["name"],
                "shares": holding["shares"] if required_fields["shares"] else None,
                "cost_price": holding["cost_price"],
                "reference_cost": holding["reference_cost"],
                "framework": holding["framework"],
                "framework_confident": holding["framework_confident"],
                "stop_loss_15": _number(holding["stop_loss_15"]),
                "stop_loss_20": _number(holding["stop_loss_20"]),
                "quote": quote_output,
                **risk,
                "risk_gates": gates,
                "p3_gate": p3,
                "alerts": alert_output,
                "l3": l3,
                "tier": holding["tier"],
            }
        )

    active_count = len(holdings)
    stock_market_value = _number(total_market_value) if valued else None
    stock_weight_pct = (
        _number(total_market_value / risk_value * 100)
        if valued and risk_value is not None else None
    )
    if valued and (stock_market_value is None or risk_value is not None and stock_weight_pct is None):
        partial = True
        _add_gap(gaps, None, "account_valuation", "restore finite account valuation inputs")
    if active_count and priced == 0 and not stale and not conflicted:
        data_status = "unavailable"
    elif conflicted:
        data_status = "conflicted"
    elif stale:
        data_status = "stale"
    elif partial:
        data_status = "partial"
    else:
        data_status = "complete"
    valuation_status = (
        "exact"
        if valued == active_count
        else "priced_positions_lower_bound"
        if valued
        else "unavailable"
        if active_count
        else "exact"
    )
    quote_complete = priced == active_count
    denominator_source = _denominator_source(portfolio_value, denominator_evidence)
    budget = risk_budget.assess(
        output_holdings, risk_value, limits,
        account_scope=denominator_evidence.get("account_scope") if denominator_evidence else None,
        denominator_source=denominator_source or "cli",
    )
    for breach in budget.pop("breaches"):
        _add_escalation(
            escalations, breach["code"], "risk_budget_exceeded", breach["detail"]
        )

    gaps.sort(
        key=lambda item: (item["code"] or "", item["field"], item["minimum_action"])
    )
    if data_status in {"stale", "unavailable", "conflicted"}:
        review_status = "blocked"
    elif data_status == "partial" or escalations or budget["risk_budget_status"] is None:
        review_status = "review_required"
    else:
        review_status = "cleared"
    action_status = (
        "trade_candidate"
        if trade_requested and data_status == "complete"
        else "review_candidate"
        if review_status != "cleared"
        else "no_action"
    )
    clean = (
        data_status == "complete"
        and valuation_status == "exact"
        and review_status == "cleared"
        and action_status == "no_action"
        and budget["risk_budget_status"] == "within_budget"
        and quote_complete
        and not escalations
        and not gaps
    )
    payload = {
        "schema_version": 1,
        "as_of": current.isoformat(),
        "runtime_version": _runtime_version(),
        "data_status": data_status,
        "valuation_status": valuation_status,
        "review_status": review_status,
        "action_status": action_status,
        "account": {
            "portfolio_value": portfolio_value,
            "denominator_status": "explicit"
            if portfolio_value is not None
            else "missing",
            "stock_market_value": stock_market_value,
            "stock_weight_pct": stock_weight_pct,
            **budget,
            "risk_policy": limits,
            **(denominator_evidence if denominator_evidence is not None
               else RiskParameters(portfolio_value, limits).denominator_evidence()),
        },
        "quote_coverage": {
            "priced": priced,
            "active": active_count,
            "complete": quote_complete,
        },
        "industry_context": industry_context,
        "holdings": output_holdings,
        "escalations": escalations,
        "data_gaps": gaps,
        "stop_reason": "clean_fast_gate" if clean else None,
        "requires_user_confirmation": action_status == "trade_candidate",
        "manifest": {
            "sources": {
                "local_state": "sqlite_read_only",
                "quotes": sorted(
                    {
                        item["quote"]["source"]
                        for item in output_holdings
                        if item["quote"]["source"]
                    }
                ),
                "quote_as_of": sorted(
                    {
                        item["quote"]["as_of"]
                        for item in output_holdings
                        if item["quote"]["as_of"]
                    }
                ),
            },
            "active_holdings": active_count,
            "closed_holdings": local["closed_count"],
            "denominator_source": denominator_source,
            "owners": {
                "holdings_alerts_l3_tier_gates": "read_only_db_session",
                "quotes": "fetch_current_price_quotes",
                "portfolio_risk": "calculate_position_risk",
            },
            "fallback_reason": None,
            "stage_timings_ms": stage_timings_ms or {},
            "research_subagents": 0,
            "writes": False,
            "stop_reason": "clean_fast_gate" if clean else None,
        },
    }
    return _with_research(payload, research_result)


def unavailable_monitor_snapshot(
    portfolio_value: float | None, risk_policy: dict | None = None,
    denominator_evidence: dict | None = None,
    research_result: dict | None = None,
) -> dict[str, Any]:
    portfolio_value = portfolio_value if risk_budget.positive_number(portfolio_value) else None
    payload = {
        "schema_version": 1,
        "as_of": domain.cst_now().isoformat(),
        "runtime_version": _runtime_version(),
        "data_status": "unavailable",
        "valuation_status": "unavailable",
        "review_status": "blocked",
        "action_status": "review_candidate",
        "account": {
            "portfolio_value": portfolio_value,
            "denominator_status": "explicit"
            if portfolio_value is not None
            else "missing",
            "stock_market_value": None,
            "stock_weight_pct": None,
            "total_stop_risk": None,
            "total_stop_risk_pct": None,
            "risk_budget_status": None,
            "known_stop_risk_lower_bound": None,
            "risk_policy": risk_policy if risk_policy is not None else risk_budget.policy(),
            **(denominator_evidence if denominator_evidence is not None
               else RiskParameters(portfolio_value, {}).denominator_evidence()),
        },
        "quote_coverage": {"priced": 0, "active": 0, "complete": False},
        "industry_context": {
            "status": "not_applicable",
            "error_code": None,
            "freshness_days": None,
            "source": None,
            "fetched_at": None,
        },
        "holdings": [],
        "escalations": [],
        "data_gaps": [
            {
                "code": None,
                "field": "database",
                "minimum_action": "restore the required state database",
            }
        ],
        "stop_reason": None,
        "requires_user_confirmation": False,
        "manifest": {
            "sources": {"local_state": "unavailable", "quotes": [], "quote_as_of": []},
            "active_holdings": None,
            "closed_holdings": None,
            "denominator_source": _denominator_source(portfolio_value, denominator_evidence),
            "owners": {
                "holdings_alerts_l3_tier_gates": "read_only_db_session",
                "quotes": "fetch_current_price_quotes",
                "portfolio_risk": "calculate_position_risk",
            },
            "fallback_reason": None,
            "stage_timings_ms": {},
            "research_subagents": 0,
            "writes": False,
            "stop_reason": None,
        },
    }
    return _with_research(payload, research_result)


def _with_research(payload: dict, result: dict | None) -> dict:
    research, prompts, escalations = monitor_research.summarize(
        result or {"status": "not_configured"},
        [holding["code"] for holding in payload["holdings"]],
        date.fromisoformat(payload["as_of"][:10]),
        payload["account"]["risk_policy"].get("untradable_boards"),
    )
    payload["research"], payload["review_prompts"] = research, prompts
    payload["escalations"].extend(escalations)
    payload["escalations"].sort(key=lambda item: (
        0 if item["candidate"] == "trade" else 1,
        _REASON_ORDER[item["reason_code"]], item["code"] or "", item["detail"],
    ))
    if escalations:
        # A thesis review is an escalation like any other: it ends the clean fast path.
        if payload["review_status"] == "cleared":
            payload["review_status"] = "review_required"
        if payload["action_status"] == "no_action":
            payload["action_status"] = "review_candidate"
        payload["stop_reason"] = payload["manifest"]["stop_reason"] = None
    monitor_contract.validate_monitor_snapshot(payload)
    return payload


def _parse_monitor_snapshot_args(args: list[str]) -> RiskParameters:
    if "--json" not in args:
        _fail("monitor-snapshot requires --json")
    return commands_holdings._parse_portfolio_risk_args(
        [arg for arg in args if arg not in {"--json", "--no-research"}], allow_missing_scope=True
    )


def cmd_monitor_snapshot(args: list[str]) -> None:
    """Emit one read-only Level-1 monitoring snapshot as stable JSON."""
    started = time.perf_counter()
    parameters = _parse_monitor_snapshot_args(args)
    portfolio_value, limits = parameters.portfolio_value, parameters.risk_policy
    research_result = monitor_research.fetch_cards(skip="--no-research" in args)
    preflight_done = time.perf_counter()
    try:
        local = _load_monitor_local_snapshot()
    except (OSError, sqlite3.Error):
        print(
            monitor_contract.dumps_monitor_snapshot(
                unavailable_monitor_snapshot(
                    portfolio_value, limits, parameters.denominator_evidence(), research_result
                )
            )
        )
        raise SystemExit(1) from None
    local_done = time.perf_counter()
    codes = list(dict.fromkeys(item["code"] for item in local["holdings"]))
    quotes, industry_result = commands_holdings.fetch_monitor_price_quotes(codes)
    quote_done = time.perf_counter()
    payload = build_monitor_snapshot(
        local,
        quotes,
        portfolio_value,
        industry_result=industry_result,
        risk_policy=limits,
        denominator_evidence=parameters.denominator_evidence(),
        account_scope_required=parameters.account_scope_required,
        research_result=research_result,
        now=domain.cst_now(),
        stage_timings_ms={
            "preflight": round((preflight_done - started) * 1000),
            "local_snapshot": round((local_done - preflight_done) * 1000),
            "quote_batch": round((quote_done - local_done) * 1000),
        },
    )
    print(monitor_contract.dumps_monitor_snapshot(payload))
    if payload["data_status"] != "complete":
        raise SystemExit(1)


_BRIEF_REASONS = {
    "price_stop_1": "触及第一档止损",
    "price_stop_2": "触及第二档止损",
    "daily_drop": "日跌幅达3%",
    "relative_underperformance": "显著跑输行业",
    "tier_review_due": "Tier到期复核",
    "alert_review_due": "预警到期复核",
    "l3_review_due": "L3到期复核",
    "l3_candidate": "L3待复核",
    "l3_watch": "L3观察中",
    "research_thesis_review": "研究论点待复核",
    "governance_gate": "门禁未通过",
    "risk_budget_exceeded": "风险预算超限",
    "quote_gap": "行情缺失",
    "data_conflict": "数据冲突",
}


def format_daily_brief(snapshot: dict, extra: list[str], today: str) -> str:
    """Plain-text post-close brief; empty when no holding has a quote for today."""
    holdings = snapshot["holdings"]
    if not any(
        item["quote"].get("price") and item["quote"].get("quote_date") == today
        for item in holdings
    ):
        return ""
    lines = [f"📈 A股持仓盘后简报 {snapshot['as_of'][:16].replace('T', ' ')}"]
    for item in holdings:
        quote = item["quote"]
        price = quote.get("price")
        if not price:
            lines.append(f"{item['name']} {item['code']} 行情缺失")
            continue
        change = quote.get("daily_change_pct")
        stops = " ".join(
            f"距{label}{(price - stop) / price:+.1%}"
            for label, stop in (
                ("止损1", item["stop_loss_15"]),
                ("止损2", item["stop_loss_20"]),
            )
            if stop
        )
        change_text = f"（{change:+.2f}%）" if change is not None else ""
        weight = item.get("portfolio_weight_pct")
        weight_text = f" 仓位{weight:.2f}%" if weight is not None else ""
        lines.append(
            f"{item['name']} {item['code']} {price:.2f}{change_text} {stops}{weight_text}"
        )
    attention = [
        f"{event['code'] or '组合'} {_BRIEF_REASONS.get(event['reason_code'], event['reason_code'])}："
        f"{event['detail']}"
        for event in snapshot["escalations"]
        if event["reason_code"] != "denominator_missing"
    ]
    horizon = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    upcoming: dict[str, list[str]] = {}
    l3_labels = {
        "pending": "待核实",
        "not_triggered": "未触发",
        "watch": "观察中",
        "triggered": "已触发",
    }
    for item in holdings:
        for condition in (item.get("l3") or {}).get("conditions", []):
            due = condition.get("next_review")
            status = condition.get("status")
            if status == "not_triggered" and due and today < due <= horizon:
                upcoming.setdefault(due, []).append(item["code"])
            elif status != "not_triggered" or due and due <= today:
                attention.append(
                    f"{item['code']} L3#{condition['id']} {l3_labels.get(status, status)}，"
                    f"下次复核 {due or '未设'}"
                )
    for due, codes in sorted(upcoming.items()):
        attention.append(
            f"{due} 有{len(codes)}项L3例行复核，当前均未触发"
            f"（{'、'.join(dict.fromkeys(codes))}）"
        )
    attention += extra
    lines.append("需关注：" + ("" if attention else "无触发项"))
    lines += [f"- {text}" for text in attention]
    account = snapshot.get("account") or {}
    value = account.get("portfolio_value")
    if value is None:
        lines.append("总资产尚无保存记录；后续随账户截图更新。")
    else:
        weight = account.get("stock_weight_pct")
        if weight is not None:
            label = (
                "股票仓位"
                if snapshot.get("valuation_status") == "exact"
                else "已取价股票仓位（下限）"
            )
            lines.append(f"{label} {weight:.2f}%（总资产 {value:,.2f} 元）")
        policy = account.get("risk_policy") or {}
        risk = account.get("total_stop_risk_pct")
        if risk is not None:
            lines.append(
                f"组合第二档止损风险 {risk:.2f}% / 预算{policy['max_portfolio_risk_pct']:g}%"
                f"；单股预算{policy['max_position_risk_pct']:g}%"
            )
        else:
            lines.append("组合止损风险不完整，已知下限不视为完整风险。")
        if account.get("portfolio_value_source") == "account_snapshot":
            lines.append(
                f"资产基线：系统保存，记录于 {account['portfolio_value_recorded_at']}。"
            )
        if account.get("portfolio_value_as_of"):
            lines.append(f"资产估值时点：{account['portfolio_value_as_of']}。")
        else:
            lines.append("资产估值时点未标注；不以本次采集时间代替。")
    return "\n".join(lines)


def cmd_daily_brief(args: list[str]) -> None:
    """Print a post-close brief for push delivery; prints nothing on non-trading days."""
    if args:
        _fail("daily-brief 不接受参数")
    parameters = commands_holdings._parse_portfolio_risk_args(
        [], allow_missing_scope=True
    )
    local = _load_monitor_local_snapshot()
    codes = list(dict.fromkeys(item["code"] for item in local["holdings"]))
    quotes, industry_result = commands_holdings.fetch_monitor_price_quotes(codes)
    snapshot = build_monitor_snapshot(
        local,
        quotes,
        parameters.portfolio_value,
        industry_result=industry_result,
        now=domain.cst_now(),
        risk_policy=parameters.risk_policy,
        denominator_evidence=parameters.denominator_evidence(),
        account_scope_required=parameters.account_scope_required,
    )
    _, findings, failures = commands_holdings.scan_unrecorded_dividends()
    extra = [
        f"{code} 疑似漏记分红：登记日{item['record_date']} 应收{item['cash']:.2f}元"
        "（运行 dividend-check 核对）"
        for code, _name, _shares, item in findings
    ] + [f"{code} 分红未核对：数据获取失败" for code, _name, _exc in failures]
    brief = format_daily_brief(snapshot, extra, domain.cst_today())
    if brief:
        print(brief)


def _validated_rewrite_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("stdin 必须是一个有效 JSON 对象")
    required = {"l1", "l2", "rewrite_reason", "retire_l3_ids", "new_l3"}
    if not isinstance(payload, dict) or set(payload) != required:
        _fail(f"JSON 字段必须且只能为：{', '.join(sorted(required))}")
    for field in ("l1", "l2", "rewrite_reason"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            _fail(f"{field} 必须是非空字符串")
        payload[field] = payload[field].strip()
    retire_ids = payload["retire_l3_ids"]
    if (
        not isinstance(retire_ids, list)
        or any(type(item) is not int or item <= 0 for item in retire_ids)
        or len(retire_ids) != len(set(retire_ids))
    ):
        _fail("retire_l3_ids 必须是不重复的正整数数组")
    if not isinstance(payload["new_l3"], list) or not payload["new_l3"]:
        _fail("new_l3 必须是非空数组")
    l3_fields = {
        "condition",
        "scope",
        "action",
        "materiality_basis",
        "temporary_exit_rule",
    }
    for index, item in enumerate(payload["new_l3"], start=1):
        if (
            not isinstance(item, dict)
            or not l3_fields <= set(item)
            or set(item) - l3_fields - {"evidence_cadence"}
        ):
            _fail(f"new_l3[{index}] 字段不完整或包含未知字段")
        if any(
            not isinstance(item[field], str) or not item[field].strip()
            for field in l3_fields
        ):
            _fail(f"new_l3[{index}] 的所有字段必须是非空字符串")
        for field in l3_fields:
            item[field] = item[field].strip()
        cadence = item.setdefault("evidence_cadence", "unspecified")
        if not isinstance(cadence, str) or cadence not in _EVIDENCE_CADENCES:
            _fail(f"new_l3[{index}] 的 evidence_cadence 非法")
        if item["scope"] not in _SCOPES or item["action"] not in _ACTIONS:
            _fail(f"new_l3[{index}] 的 scope/action 非法")
        if item["scope"] == "non_core" and item["action"] != "review":
            _fail("non_core 条件最多只能配置 review，不能配置 reduce/exit")
    return payload


def _l3_cadence_args(args: list[str]) -> tuple[list[str], str | None]:
    args = list(args)
    cadence = None
    if "--evidence-cadence" in args:
        index = args.index("--evidence-cadence")
        if index + 1 >= len(args) or args[index + 1] not in _EVIDENCE_CADENCES:
            _fail(
                "evidence_cadence 必须为 quarterly/semiannual/annual/event/unspecified"
            )
        cadence = args[index + 1]
        del args[index : index + 2]
    if any(arg.startswith("--") for arg in args):
        _fail("未知或重复的 L3 参数")
    return args, cadence


def cmd_l3_add(args: list[str]) -> None:
    """Add a structured L3 condition. Usage: l3-add <code> <origin> <condition> [temporary_exit_rule]."""
    args, cadence = _l3_cadence_args(args)
    if len(args) not in (3, 4):
        print(
            "错误：需要参数 <代码> <original|recovered|new_monitoring> "
            "<条件文本> [临时出场规则]",
            file=sys.stderr,
        )
        sys.exit(1)
    code, origin, condition_text = args[:3]
    temporary_exit_rule = args[3] if len(args) > 3 else None
    if origin not in ("original", "recovered", "new_monitoring"):
        print("错误：origin 非法", file=sys.stderr)
        sys.exit(1)
    with db.db_session() as conn:
        holding_id, _, _ = commands_holdings._single_open_holding(conn, code)
        if conn.execute(
            """SELECT 1 FROM holding_thesis_versions
               WHERE holding_id=? AND status='active'""",
            (holding_id,),
        ).fetchone():
            _fail("该持仓已启用论文版本，请使用 thesis-rewrite 原子重写 L3")
        now_iso = domain.utc_now_iso()
        cursor = conn.execute(
            """INSERT INTO holding_l3_conditions
               (holding_id, condition_text, origin_type, temporary_exit_rule,
                created_at, updated_at, evidence_cadence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                holding_id,
                condition_text,
                origin,
                temporary_exit_rule,
                now_iso,
                now_iso,
                cadence or "unspecified",
            ),
        )
        conn.commit()
    print(f"L3条件已记录：id={cursor.lastrowid} {code} [{origin}] {condition_text}")


def cmd_l3_update(args: list[str]) -> None:
    """Update evidence-backed status. Usage: l3-update <id> <status> <as_of> <evidence> [next_review]."""
    args, cadence = _l3_cadence_args(args)
    if len(args) not in (4, 5):
        print(
            "错误：需要参数 <条件id> <pending|not_triggered|watch|triggered> "
            "<as-of> <证据> [下次复核日期]",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        condition_id = int(args[0])
    except ValueError:
        print("错误：条件id必须为整数", file=sys.stderr)
        sys.exit(1)
    status, as_of, evidence = args[1:4]
    if status not in ("pending", "not_triggered", "watch", "triggered"):
        print("错误：L3状态非法", file=sys.stderr)
        sys.exit(1)
    if not as_of.strip() or not evidence.strip():
        _fail("as-of 和证据必须是非空字符串")
    next_review = args[4] if len(args) > 4 else None
    for candidate in (as_of, next_review):
        if candidate:
            try:
                date.fromisoformat(candidate)
            except ValueError:
                print("错误：日期必须为 YYYY-MM-DD", file=sys.stderr)
                sys.exit(1)
    with db.db_session() as conn:
        cursor = conn.execute(
            """UPDATE holding_l3_conditions
               SET status=?, evidence=?, as_of=?, next_review_date=?, updated_at=?,
                   evidence_cadence=COALESCE(?, evidence_cadence)
               WHERE id=? AND is_active=1""",
            (
                status,
                evidence,
                as_of,
                next_review,
                domain.utc_now_iso(),
                cadence,
                condition_id,
            ),
        )
        if cursor.rowcount == 0:
            _fail(f"未找到活动L3条件 id={condition_id}；已退役条件不可更新")
        conn.commit()
    print(f"L3状态已更新：id={condition_id} → {status}")


def cmd_l3_list(args: list[str]) -> None:
    show_all = "--all" in args
    active_only = "--active" in args
    json_output = "--json" in args
    if show_all and active_only:
        _fail("--all 与 --active 不能同时使用")
    positionals = [arg for arg in args if not arg.startswith("--")]
    if len(positionals) != 1:
        _fail("需要参数 <代码> [--active|--all] [--json]")
    code = positionals[0]
    with db.db_session() as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(holding_l3_conditions)")
        }
        thesis_columns = {
            "thesis_version_id",
            "is_active",
            "retired_at",
            "retired_reason",
            "condition_scope",
            "action_level",
            "materiality_basis",
        }
        thesis_table_exists = bool(
            conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='holding_thesis_versions'"""
            ).fetchone()
        )
        active_thesis_index_exists = thesis_table_exists and any(
            row[1] == "idx_thesis_one_active_per_holding"
            and row[2] == 1
            and row[4] == 1
            for row in conn.execute("PRAGMA index_list(holding_thesis_versions)")
        )
        if not (thesis_columns & columns) and not thesis_table_exists:
            warning = f"{code} legacy contract（尚未迁移论文版本，保留旧L3行为）"
            rows = conn.execute(
                """SELECT l.id, l.origin_type, l.status, l.condition_text, l.as_of,
                          l.next_review_date, l.evidence, l.temporary_exit_rule
                   FROM holding_l3_conditions l
                   JOIN holdings h ON h.id=l.holding_id
                   WHERE h.code=? AND h.exit_date IS NULL ORDER BY l.id""",
                (code,),
            ).fetchall()
            if json_output:
                print(
                    json.dumps(
                        {
                            "code": code,
                            "contract": "legacy",
                            "warning": warning,
                            "thesis": None,
                            "conditions": [
                                {
                                    "id": row[0],
                                    "origin": row[1],
                                    "status": row[2],
                                    "active": True,
                                    "condition": row[3],
                                    "as_of": row[4],
                                    "next_review": row[5],
                                    "evidence": row[6],
                                    "temporary_exit_rule": row[7],
                                    "scope": None,
                                    "action": None,
                                    "materiality_basis": None,
                                    "evidence_cadence": "unspecified",
                                    "valid_for_action": False,
                                }
                                for row in rows
                            ],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return
            print(f"警告：{warning}")
            for row in rows:
                print(
                    f"#{row[0]} [{row[1]}] {row[2]} | {row[3]} | as-of:{row[4] or '─'} "
                    f"| 下次:{row[5] or '─'} | 证据:{row[6] or '─'} | 临时出场:{row[7] or '─'} | evidence_cadence:unspecified"
                )
            if not rows:
                print(f"{code} 无结构化L3条件")
            return
        if (
            not thesis_columns <= columns
            or not thesis_table_exists
            or not active_thesis_index_exists
        ):
            warning = "论文版本迁移不完整，冻结交易；请先完成schema恢复"
            if json_output:
                print(
                    json.dumps(
                        {
                            "code": code,
                            "contract": "invalid",
                            "warning": warning,
                            "thesis": None,
                            "conditions": [],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return
            print(f"警告：{warning}")
            return

        holding = conn.execute(
            "SELECT 1 FROM holdings WHERE code=? AND exit_date IS NULL LIMIT 1",
            (code,),
        ).fetchone()
        if holding is None:
            if json_output:
                print(
                    json.dumps(
                        {
                            "code": code,
                            "contract": "none",
                            "warning": None,
                            "thesis": None,
                            "conditions": [],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                return
            print(f"{code} 无结构化L3条件")
            return
        holding_id = commands_holdings._single_open_holding(conn, code)[0]
        active_thesis = conn.execute(
            """SELECT id, version, l1, l2 FROM holding_thesis_versions
               WHERE holding_id=? AND status='active'""",
            (holding_id,),
        ).fetchone()
        if not json_output and show_all:
            for version, status, l1, l2, reason in conn.execute(
                """SELECT version, status, l1, l2, rewrite_reason
                   FROM holding_thesis_versions WHERE holding_id=? ORDER BY version""",
                (holding_id,),
            ):
                print(
                    f"thesis:v{version} {status} | L1:{l1} | L2:{l2} "
                    f"| 重写原因:{reason}"
                )
        elif not json_output and active_thesis is not None:
            print(
                f"thesis:v{active_thesis[1]} active | L1:{active_thesis[2]} "
                f"| L2:{active_thesis[3]}"
            )
        all_rows = conn.execute(
            """SELECT l.id, l.origin_type, l.status, l.condition_text, l.as_of,
                      l.next_review_date, l.evidence, l.temporary_exit_rule,
                      l.thesis_version_id, l.is_active, l.retired_at, l.retired_reason,
                      l.condition_scope, l.action_level, l.materiality_basis, t.version,
                      l.evidence_cadence
               FROM holding_l3_conditions l
               LEFT JOIN holding_thesis_versions t ON t.id=l.thesis_version_id
               WHERE l.holding_id=? ORDER BY l.id""",
            (holding_id,),
        ).fetchall()
    warning = None

    def valid(row) -> bool:
        return False

    if active_thesis is None:
        warning = f"{code} legacy contract（尚未重写论文，保留旧L3行为）"
        if not json_output:
            print(f"警告：{warning}")
        rows = all_rows if show_all else [row for row in all_rows if row[9] == 1]
    else:
        active_thesis_id = active_thesis[0]

        def versioned_valid(row) -> bool:
            evidence_contract_valid = True
            if row[2] == "triggered":
                try:
                    date.fromisoformat(row[4])
                except (TypeError, ValueError):
                    evidence_contract_valid = False
                evidence_contract_valid = evidence_contract_valid and bool(
                    row[6] and row[6].strip()
                )
            return (
                row[9] == 1
                and row[8] == active_thesis_id
                and row[12] in _SCOPES
                and row[13] in _ACTIONS
                and not (row[12] == "non_core" and row[13] != "review")
                and bool(row[14])
                and bool(row[7])
                and evidence_contract_valid
            )

        valid = versioned_valid
        invalid_active = [row for row in all_rows if row[9] == 1 and not valid(row)]
        if invalid_active:
            warning = "触发文本成立，但交易契约无效/已过期，冻结交易并重做论文"
            if not json_output:
                print(f"警告：{warning}")
        rows = all_rows if show_all else [row for row in all_rows if valid(row)]
    if json_output:
        print(
            json.dumps(
                {
                    "code": code,
                    "contract": "versioned" if active_thesis is not None else "legacy",
                    "warning": warning,
                    "thesis": (
                        {
                            "id": active_thesis[0],
                            "version": active_thesis[1],
                            "l1": active_thesis[2],
                            "l2": active_thesis[3],
                        }
                        if active_thesis is not None
                        else None
                    ),
                    "conditions": [
                        {
                            "id": row[0],
                            "origin": row[1],
                            "status": row[2],
                            "active": bool(row[9]),
                            "condition": row[3],
                            "as_of": row[4],
                            "next_review": row[5],
                            "evidence": row[6],
                            "temporary_exit_rule": row[7],
                            "scope": row[12],
                            "action": row[13],
                            "materiality_basis": row[14],
                            "valid_for_action": bool(valid(row)),
                            "thesis_version": row[15],
                            "evidence_cadence": row[16],
                            "retired_at": row[10],
                            "retired_reason": row[11],
                        }
                        for row in rows
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return
    if not rows:
        print(f"{code} 无活动结构化L3条件")
        return
    for row in rows:
        lifecycle = "active" if row[9] else "retired"
        thesis = f"v{row[15]}" if row[15] is not None else "legacy"
        print(
            f"#{row[0]} [{row[1]}] {row[2]} {lifecycle} thesis:{thesis} "
            f"[{row[12]}/{row[13]}] | {row[3]} | as-of:{row[4] or '─'} "
            f"| 下次:{row[5] or '─'} | 证据:{row[6] or '─'} | 重要性:{row[14] or '─'} "
            f"| 临时出场:{row[7] or '─'} | 退役:{row[10] or '─'} {row[11] or ''} "
            f"| evidence_cadence:{row[16]}"
        )


def cmd_thesis_rewrite(args: list[str]) -> None:
    """Atomically version a holding thesis and replace its active L3 set."""
    if len(args) != 1:
        _fail("需要参数 <代码>")
    code = args[0]
    payload = _validated_rewrite_payload()
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            holdings = conn.execute(
                "SELECT id FROM holdings WHERE code=? AND exit_date IS NULL",
                (code,),
            ).fetchall()
            if len(holdings) != 1:
                raise ValueError(f"{code} 必须且只能有一个活动持仓")
            holding_id = holdings[0][0]
            requested = set(payload["retire_l3_ids"])
            if requested:
                placeholders = ",".join("?" for _ in requested)
                matched = conn.execute(
                    f"""SELECT id FROM holding_l3_conditions
                        WHERE id IN ({placeholders}) AND holding_id=? AND is_active=1""",
                    (*sorted(requested), holding_id),
                ).fetchall()
                if {row[0] for row in matched} != requested:
                    raise ValueError("retire_l3_ids 含跨持仓、不存在或已退役的条件")
            active_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM holding_l3_conditions WHERE holding_id=? AND is_active=1",
                    (holding_id,),
                )
            }
            if requested != active_ids:
                raise ValueError("retire_l3_ids 必须完整覆盖该持仓全部活动L3")
            prior = conn.execute(
                """SELECT id FROM holding_thesis_versions
                   WHERE holding_id=? AND status='active'""",
                (holding_id,),
            ).fetchone()
            if prior:
                conn.execute(
                    "UPDATE holding_thesis_versions SET status='superseded' WHERE id=?",
                    (prior[0],),
                )
            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM holding_thesis_versions WHERE holding_id=?",
                (holding_id,),
            ).fetchone()[0]
            now_iso = domain.utc_now_iso()
            thesis_id = conn.execute(
                """INSERT INTO holding_thesis_versions
                   (holding_id, version, l1, l2, rewrite_reason, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                (
                    holding_id,
                    version,
                    payload["l1"],
                    payload["l2"],
                    payload["rewrite_reason"],
                    now_iso,
                ),
            ).lastrowid
            if requested:
                placeholders = ",".join("?" for _ in requested)
                conn.execute(
                    f"""UPDATE holding_l3_conditions
                        SET is_active=0, retired_at=?, retired_reason=?, updated_at=?
                        WHERE id IN ({placeholders})""",
                    (
                        now_iso,
                        payload["rewrite_reason"],
                        now_iso,
                        *sorted(requested),
                    ),
                )
            for item in payload["new_l3"]:
                conn.execute(
                    """INSERT INTO holding_l3_conditions
                       (holding_id, thesis_version_id, condition_text, origin_type,
                        temporary_exit_rule, condition_scope, action_level,
                        materiality_basis, created_at, updated_at, evidence_cadence)
                       VALUES (?, ?, ?, 'new_monitoring', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        holding_id,
                        thesis_id,
                        item["condition"],
                        item["temporary_exit_rule"],
                        item["scope"],
                        item["action"],
                        item["materiality_basis"],
                        now_iso,
                        now_iso,
                        item["evidence_cadence"],
                    ),
                )
            if conn.execute(
                """SELECT 1 FROM holding_l3_conditions
                   WHERE holding_id=? AND is_active=1
                     AND (thesis_version_id IS NULL
                          OR condition_scope='legacy_unclassified'
                          OR action_level='legacy_unclassified') LIMIT 1""",
                (holding_id,),
            ).fetchone():
                raise ValueError("重写后仍存在活动 legacy-unclassified L3")
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            _fail(str(exc))
        except BaseException:
            conn.rollback()
            raise
    print(
        f"论文已原子重写：{code} thesis:v{version}，退役{len(requested)}条，新增{len(payload['new_l3'])}条L3"
    )


def cmd_tier_config(args: list[str]) -> None:
    """Persist entry-time tier choices outside notes."""
    if len(args) < 2:
        print(
            "错误：需要参数 <代码> <A|B|C|none> [目标涨幅%|none] [E|F|none]",
            file=sys.stderr,
        )
        sys.exit(1)
    code = args[0]
    exit_path = None if args[1].lower() == "none" else args[1].upper()
    if exit_path not in (None, "A", "B", "C"):
        print("错误：出场路径只能为 A/B/C/none", file=sys.stderr)
        sys.exit(1)
    target_pct = None
    if len(args) > 2 and args[2].lower() != "none":
        target_pct = commands_holdings._parse_cli_finite_float(args[2], "目标涨幅")
    if exit_path == "B" and (target_pct is None or target_pct <= 0):
        print("错误：路径B必须设置大于0的目标涨幅", file=sys.stderr)
        sys.exit(1)
    if exit_path != "B" and target_pct is not None:
        print("错误：只有路径B可以设置目标涨幅", file=sys.stderr)
        sys.exit(1)
    exemption = None
    if len(args) > 3 and args[3].lower() != "none":
        exemption = args[3].upper()
        if exemption not in ("E", "F"):
            print("错误：豁免框架只能为 E/F/none", file=sys.stderr)
            sys.exit(1)
    if exemption and exit_path is not None:
        print("错误：轻仓试探出场路径与正式仓位Tier1豁免不能同时设置", file=sys.stderr)
        sys.exit(1)
    with db.db_session() as conn:
        holding_id, buy_date, framework = commands_holdings._single_open_holding(
            conn, code
        )
        if exemption and buy_date != domain.cst_today():
            print("错误：Tier1估值豁免只能在建仓当日声明", file=sys.stderr)
            sys.exit(1)
        if exemption and not (framework or "").startswith(exemption):
            print(
                f"错误：持仓框架{framework or '未知'}与豁免框架{exemption}不一致",
                file=sys.stderr,
            )
            sys.exit(1)
        conn.execute(
            """INSERT INTO holding_tier_state
               (holding_id, exit_path, exit_target_pct, exemption_framework,
                exemption_declared_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(holding_id) DO UPDATE SET
                 exit_path=excluded.exit_path,
                 exit_target_pct=excluded.exit_target_pct,
                 exemption_framework=excluded.exemption_framework,
                 exemption_declared_at=excluded.exemption_declared_at,
                 updated_at=excluded.updated_at""",
            (
                holding_id,
                exit_path,
                target_pct,
                exemption,
                domain.cst_today() if exemption else None,
                domain.utc_now_iso(),
            ),
        )
        conn.commit()
    print(
        f"Tier配置已记录：{code} 路径={exit_path or 'none'} 豁免={exemption or 'none'}"
    )


def cmd_holding_framework(args: list[str]) -> None:
    if len(args) != 2:
        print("错误：需要参数 <代码> <A|B|C|D|E|F>", file=sys.stderr)
        sys.exit(1)
    code, token = args[0], args[1]
    normalized = domain.FRAMEWORK_ALIASES.get(
        token.upper()
    ) or domain.FRAMEWORK_ALIASES.get(token)
    if normalized is None:
        print("错误：框架必须为 A/B/C/D/E/F 或完整标签", file=sys.stderr)
        sys.exit(1)
    with db.db_session() as conn:
        holding_id, _, old_framework = commands_holdings._single_open_holding(
            conn, code
        )
        row = conn.execute(
            "SELECT reference_cost, cost_price FROM holdings WHERE id=?",
            (holding_id,),
        ).fetchone()
        reference_cost = row[0] or row[1]
        sl15_pct, sl20_pct = domain.get_stop_loss_pct(normalized)
        now_iso = domain.utc_now_iso()
        conn.execute(
            """UPDATE holdings
               SET framework=?, framework_confident=1,
                   stop_loss_15=?, stop_loss_20=?, updated_at=?
               WHERE id=?""",
            (
                normalized,
                round(reference_cost * sl15_pct, 3),
                round(reference_cost * sl20_pct, 3),
                now_iso,
                holding_id,
            ),
        )
        conn.execute(
            """INSERT INTO holding_events
               (holding_id, code, event_type, event_date, notes, created_at)
               VALUES (?, ?, 'adjustment', ?, ?, ?)""",
            (
                holding_id,
                code,
                domain.cst_today(),
                f"framework:{old_framework or 'unknown'}->{normalized}",
                now_iso,
            ),
        )
        conn.commit()
    print(
        f"持仓框架已迁移：{code} {old_framework or 'unknown'} → {normalized}，止损线已重算"
    )


def cmd_tier_update(args: list[str]) -> None:
    if len(args) != 3:
        print("错误：需要参数 <代码> <tier1|tier2|tier3> <状态>", file=sys.stderr)
        sys.exit(1)
    code, tier, status = args
    allowed = {
        "tier1": {"pending", "completed", "exempted"},
        "tier2": {"pending", "completed"},
        "tier3": {"pending", "completed"},
    }
    if tier not in allowed or status not in allowed[tier]:
        print("错误：Tier或状态非法", file=sys.stderr)
        sys.exit(1)
    with db.db_session() as conn:
        holding_id, _, _ = commands_holdings._single_open_holding(conn, code)
        conn.execute(
            """INSERT INTO holding_tier_state (holding_id, updated_at)
               VALUES (?, ?) ON CONFLICT(holding_id) DO NOTHING""",
            (holding_id, domain.utc_now_iso()),
        )
        conn.execute(
            f"UPDATE holding_tier_state SET {tier}_status=?, updated_at=? WHERE holding_id=?",
            (status, domain.utc_now_iso(), holding_id),
        )
        conn.commit()
    print(f"Tier状态已更新：{code} {tier}={status}")


def cmd_alert_open(args: list[str]) -> None:
    """Open/update a deduplicated structured alert."""
    if len(args) < 6:
        print(
            "错误：需要参数 <代码> <yellow|red> "
            "<holding_deterioration|entry_valuation|unverified> "
            "<reason_code> <review_due|none> <原因> [证据]",
            file=sys.stderr,
        )
        sys.exit(1)
    code, level, category, reason_code, review_due, reason = args[:6]
    evidence = args[6] if len(args) > 6 else None
    if level not in ("yellow", "red"):
        print("错误：预警级别非法", file=sys.stderr)
        sys.exit(1)
    if category not in ("holding_deterioration", "entry_valuation", "unverified"):
        print("错误：预警类别非法", file=sys.stderr)
        sys.exit(1)
    review_due = None if review_due.lower() == "none" else review_due
    if review_due:
        try:
            date.fromisoformat(review_due)
        except ValueError:
            try:
                parsed_due = __import__("datetime").datetime.fromisoformat(review_due)
            except ValueError:
                print(
                    "错误：复核日期必须为 YYYY-MM-DD 或带时区 ISO 8601 或 none",
                    file=sys.stderr,
                )
                sys.exit(1)
            if parsed_due.tzinfo is None:
                print("错误：ISO 8601 复核时间必须带时区", file=sys.stderr)
                sys.exit(1)
    with db.db_session() as conn:
        holding_id, _, _ = commands_holdings._single_open_holding(conn, code)
        now_iso = domain.utc_now_iso()
        conn.execute(
            """INSERT INTO holding_alerts
               (holding_id, code, level, category, reason_code, reason, evidence,
                opened_at, review_due, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(holding_id, reason_code) WHERE status != 'resolved'
               DO UPDATE SET level=excluded.level, category=excluded.category,
                 reason=excluded.reason, evidence=excluded.evidence,
                 review_due=CASE
                   WHEN holding_alerts.reason_code='price_stop2_liquidity_defer'
                        AND holding_alerts.review_due IS NOT NULL
                   THEN holding_alerts.review_due
                   ELSE excluded.review_due
                 END,
                 updated_at=excluded.updated_at""",
            (
                holding_id,
                code,
                level,
                category,
                reason_code,
                reason,
                evidence,
                now_iso,
                review_due,
                now_iso,
            ),
        )
        conn.commit()
    print(f"结构化预警已记录：{code} {level} {reason_code}")


def cmd_alert_resolve(args: list[str]) -> None:
    if len(args) < 3:
        print("错误：需要参数 <代码> <reason_code> <解除证据>", file=sys.stderr)
        sys.exit(1)
    code, reason_code, evidence = args[0], args[1], " ".join(args[2:])
    with db.db_session() as conn:
        now_iso = domain.utc_now_iso()
        cursor = conn.execute(
            """UPDATE holding_alerts
               SET status='resolved', resolved_at=?, resolution_evidence=?, updated_at=?
               WHERE code=? AND reason_code=? AND status!='resolved' """,
            (now_iso, evidence, now_iso, code, reason_code),
        )
        if cursor.rowcount == 0:
            print("错误：未找到对应的活动预警", file=sys.stderr)
            sys.exit(1)
        conn.commit()
    print(f"预警已解除：{code} {reason_code}")


def cmd_alert_pending(args: list[str]) -> None:
    if len(args) < 3:
        print("错误：需要参数 <代码> <reason_code> <待核实说明>", file=sys.stderr)
        sys.exit(1)
    code, reason_code, evidence = args[0], args[1], " ".join(args[2:])
    with db.db_session() as conn:
        cursor = conn.execute(
            """UPDATE holding_alerts
               SET status='pending', evidence=?, updated_at=?
               WHERE code=? AND reason_code=? AND status='active' """,
            (evidence, domain.utc_now_iso(), code, reason_code),
        )
        if cursor.rowcount == 0:
            print("错误：未找到对应的活动预警", file=sys.stderr)
            sys.exit(1)
        conn.commit()
    print(f"预警已转待核实：{code} {reason_code}")


def cmd_alerts(args: list[str]) -> None:
    active_only = "--active" in args
    json_output = "--json" in args
    positionals = [arg for arg in args if not arg.startswith("--")]
    if len(positionals) != 1:
        print("错误：需要参数 <代码>", file=sys.stderr)
        sys.exit(1)
    code = positionals[0]
    with db.db_session() as conn:
        rows = conn.execute(
            """SELECT level, category, reason_code, status, reason, review_due,
                      evidence, resolution_evidence
               FROM holding_alerts WHERE code=?"""
            + (" AND status!='resolved'" if active_only else "")
            + " ORDER BY status='resolved', opened_at, id",
            (code,),
        ).fetchall()
    if json_output:
        print(
            json.dumps(
                [
                    {
                        "level": row[0],
                        "category": row[1],
                        "reason_code": row[2],
                        "status": row[3],
                        "reason": row[4],
                        "review_due": row[5],
                        "evidence": row[6],
                        "resolution_evidence": row[7],
                    }
                    for row in rows
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return
    if not rows:
        print(f"{code} 无结构化预警")
        return
    for row in rows:
        print(
            f"{row[0]} [{row[1]}] {row[2]} {row[3]} | {row[4]} "
            f"| 复核:{row[5] or '─'} | 证据:{row[6] or '─'} "
            f"| 解除:{row[7] or '─'}"
        )


def cmd_set_flag(args: list[str]) -> None:
    """⑤ 记录红黄线预警（追加，不覆盖历史）"""
    if len(args) < 3:
        print("错误：需要参数 <代码> <yellow|red> <原因>", file=sys.stderr)
        sys.exit(1)
    code, level, reason = args[0], args[1], " ".join(args[2:])
    if level not in ("yellow", "red"):
        print("错误：level 必须为 yellow 或 red", file=sys.stderr)
        sys.exit(1)
    today = domain.cst_today()
    new_flag = json.dumps(
        {"level": level, "reason": reason, "date": today}, ensure_ascii=False
    )
    with db.db_session() as conn:
        cursor = conn.execute(
            "UPDATE analysis_results "
            "SET flags = json_insert(COALESCE(NULLIF(flags, ''), '[]'), '$[#]', json(?)) "
            "WHERE code=? AND date=?",
            (new_flag, code, today),
        )
        if cursor.rowcount == 0:
            print(
                f"未找到今日分析记录，请先执行 set-analysis（代码：{code}）",
                file=sys.stderr,
            )
            sys.exit(1)
        conn.commit()
    icon = "🔴" if level == "red" else "⚠️"
    print(f"预警已记录：{code} {icon} {reason}")


def cmd_clear_flag(args: list[str]) -> None:
    """⑤ 清除指定股票今日所有预警标记"""
    if len(args) < 1:
        print("错误：需要参数 <代码>", file=sys.stderr)
        sys.exit(1)
    code = args[0]
    today = domain.cst_today()
    with db.db_session() as conn:
        conn.execute(
            "UPDATE analysis_results SET flags=NULL WHERE code=? AND date=?",
            (code, today),
        )
        conn.commit()
    print(f"已清除 {code} 的今日预警标记")
