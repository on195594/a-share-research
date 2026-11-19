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


def _fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


_REASON_ORDER = {reason: index for index, reason in enumerate(monitor_contract.ESCALATION_REASON_ORDER)}


def _number(value: Any) -> float | None:
    return float(value) if risk_budget.finite_number(value) else None


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
                      t.tier1_status, t.tier2_status, t.tier3_status,
                      t.exit_path, t.exit_target_pct, t.exemption_framework,
                      t.updated_at
               FROM holdings h
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
                "tier": {
                    "tier1_status": row[11],
                    "tier2_status": row[12],
                    "tier3_status": row[13],
                    "exit_path": row[14],
                    "exit_target_pct": _number(row[15]),
                    "exemption_framework": row[16],
                    "updated_at": row[17],
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


def _check_monitor_holding(
    holding: dict[str, Any],
    quote: commands_holdings.PriceQuote | None,
    *,
    duplicate_codes: list[str],
    risk_value: float | None,
    current: datetime,
    gaps: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool, bool, bool, bool, bool]:
    today = current.date()
    today_text = today.isoformat()
    partial = stale = conflicted = trade_requested = False
    code = holding["code"]
    required_fields = {
        "shares": risk_budget.valid_shares(holding["shares"]),
        "framework": bool(holding["framework"] and holding["framework_confident"]),
        "stop_loss_15": risk_budget.finite_number(holding["stop_loss_15"])
        and holding["stop_loss_15"] > 0,
        "stop_loss_20": risk_budget.finite_number(holding["stop_loss_20"])
        and holding["stop_loss_20"] > 0,
    }
    for field, valid in required_fields.items():
        if not valid:
            partial = True
            _add_gap(gaps, code, field, f"reconcile holding {field}")

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

    risk = commands_holdings.calculate_position_risk(
        holding["shares"] if required_fields["shares"] and code not in duplicate_codes else None,
        current_price,
        holding["stop_loss_20"] if required_fields["stop_loss_20"] else None,
        risk_value,
        holding["reference_cost"],
    )
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
        if quote_valid and quote and risk_budget.finite_number(quote.industry_change_pct)
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
            if not risk_budget.finite_number(target_pct) or target_pct <= 0:
                tier_gap = "restore the entry-time path B target"
        elif tier["tier1_status"] == "pending":
            target_pct = 25
        if target_pct is not None and tier_gap is None:
            reference_cost = holding["reference_cost"]
            if not risk_budget.finite_number(reference_cost) or reference_cost <= 0:
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
    output = {
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
        "p3_gate": p3,
        "alerts": alert_output,
        "l3": l3,
        "tier": holding["tier"],
    }
    return output, quote_valid, partial, stale, conflicted, trade_requested


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
        output, quote_valid, holding_partial, holding_stale, holding_conflicted, holding_trade = (
            _check_monitor_holding(
                holding, quotes.get(holding["code"]), duplicate_codes=duplicate_codes,
                risk_value=risk_value, current=current, gaps=gaps, escalations=escalations,
            )
        )
        partial = partial or holding_partial
        stale = stale or holding_stale
        conflicted = conflicted or holding_conflicted
        trade_requested = trade_requested or holding_trade
        if quote_valid:
            priced += 1
        if output["market_value"] is not None:
            valued += 1
            total_market_value += output["market_value"]
        output_holdings.append(output)

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
    stop_reason = "clean_fast_gate" if clean else None
    payload = {
        "schema_version": 1,
        "as_of": current.isoformat(),
        "runtime_version": _V0_VERSION,
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
        "stop_reason": stop_reason,
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
                "quotes": "fetch_monitor_price_quotes",
                "portfolio_risk": "calculate_position_risk",
            },
            "fallback_reason": None,
            "stage_timings_ms": stage_timings_ms or {},
            "research_subagents": 0,
            "writes": False,
            "stop_reason": stop_reason,
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
        "runtime_version": _V0_VERSION,
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
                "quotes": "fetch_monitor_price_quotes",
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
    cadences = {
        "quarterly": "季度",
        "semiannual": "半年",
        "annual": "年度",
        "event": "事件",
    }
    for item in holdings:
        for condition in (item.get("l3") or {}).get("conditions", []):
            due = condition.get("next_review")
            status = condition.get("status")
            if status == "not_triggered" and due and today < due <= horizon:
                upcoming.setdefault(due, []).append(item["code"])
            elif status != "not_triggered" or not due or due <= today:
                cadence = cadences.get(
                    condition.get("evidence_cadence"), "未分类，季报不能改变状态"
                )
                attention.append(
                    f"{item['code']} L3#{condition['id']} {l3_labels.get(status, status)}"
                    f"（证据日期 {condition.get('as_of') or '未记录'}，证据频率 {cadence}），"
                    f"下次复核 {due or '未设'}"
                )
    for due, codes in sorted(upcoming.items()):
        attention.append(
            f"{due} 有{len(codes)}项L3例行复核，当前均未触发"
            f"（{'、'.join(dict.fromkeys(codes))}）"
        )
    attention += extra
    # review_due only schedules a re-check; an unresolved alert stays listed until resolved.
    unresolved = [
        f"{item['code']} {'红色' if alert.get('level') == 'red' else '黄色'}预警 "
        f"{alert.get('reason_code')}：{alert.get('reason') or '无说明'}"
        f"（{alert.get('status')}，开立 {(alert.get('opened_at') or '未记录')[:10]}，"
        f"复核日 {alert.get('review_due') or '未设'}）"
        for level in ("red", "yellow")
        for item in holdings
        for alert in item.get("alerts") or []
        if (alert.get("level") == "red") == (level == "red")
    ]
    lines.append(
        "需关注："
        + (
            ""
            if attention
            else "本轮程序检查未触发（止损、跌幅、行业偏离、Tier、到期预警与L3、"
            "研究论点、风险预算）；不覆盖未自动计算的基本面条件"
        )
    )
    lines += [f"- {text}" for text in attention]
    if unresolved:
        lines.append("尚未关闭：")
        lines += [f"- {text}" for text in unresolved]
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
