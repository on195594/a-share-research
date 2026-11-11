"""Pure, fail-closed investment risk gates.

The module deliberately contains no database, network, or CLI access.  Adapters
normalise provider facts before calling these functions; missing or untrusted
facts remain ``incomplete`` rather than being inferred as safe.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

CST = timezone(timedelta(hours=8))


def _source_hash(url: str) -> str:
    """Hash the canonical official source identity without network access."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


REGULATORY_RULES: dict[tuple[str, str], dict[str, Any]] = {
    ("SSE", "main"): {
        "effective_from": "2026-04-24",
        "rule_version": "SSE stock listing rules 2026-04 revision",
        "dividend_ratio": 0.30,
        "dividend_amount": 50_000_000,
        "official_url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/mainipo/c/c_20260424_10816589.shtml",
        "verified_at": "2026-09-13",
        "review_after": "2026-12-13",
        "source_hash": "b0730d419c004554e879eab5d1ea721fb95f0102ada6c046ee77900797a865c9",
    },
    ("SSE", "star"): {
        "effective_from": "2026-04-24",
        "rule_version": "SSE STAR stock listing rules 2026-04 revision",
        "dividend_ratio": 0.30,
        "dividend_amount": 30_000_000,
        "official_url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/staripo/c/c_20260424_10816592.shtml",
        "verified_at": "2026-09-13",
        "review_after": "2026-12-13",
        "source_hash": "48735fc5c74c6077b16949b096f918bf3b1144aaee00b04dfcdd9a06ad2a033f",
    },
    ("SZSE", "main"): {
        "effective_from": "2026-04-24",
        "rule_version": "SZSE stock listing rules 2026 revision",
        "dividend_ratio": 0.30,
        "dividend_amount": 50_000_000,
        "official_url": "https://docs.static.szse.cn/www/lawrules/rule/stock/W020260424747613955674.pdf",
        "verified_at": "2026-09-13",
        "review_after": "2026-12-13",
        "source_hash": "6281c29de692d66ca7ea5a97d9b6c839ccb054269809a6abf5ba4c14d7b721c8",
    },
    ("SZSE", "chinext"): {
        "effective_from": "2026-04-24",
        "rule_version": "SZSE ChiNext stock listing rules 2026 revision",
        "dividend_ratio": 0.30,
        "dividend_amount": 30_000_000,
        "official_url": "https://docs.static.szse.cn/www/lawrules/rule/stock/W020260424747613955674.pdf",
        "verified_at": "2026-09-13",
        "review_after": "2026-12-13",
        "source_hash": "6281c29de692d66ca7ea5a97d9b6c839ccb054269809a6abf5ba4c14d7b721c8",
    },
    ("BSE", "beijing"): {
        "effective_from": "2026-04-24",
        "rule_version": "BSE stock listing rules 2026 revision",
        "dividend_ratio": None,
        "dividend_amount": None,
        "official_url": "https://www.bse.cn/uploads/6/file/public/202604/20260424210610_3lvk6oirgc.pdf",
        "verified_at": "2026-09-13",
        "review_after": "2026-12-13",
        "source_hash": "a1539c6205b91b712fbe138c9d6ab69013d51c742b49d5c33465e78450cfb17c",
    },
}

P0_STATUSES = {"clear", "blocked", "incomplete"}
GATE_NAMES = ("regulatory_gate", "roe_structural_gate", "cash_flow_gate")


def evaluate_rule_freshness(rule: Mapping[str, Any], today: date) -> str:
    """Return current, review_due, or invalid_metadata for one rule."""
    required = (
        "effective_from",
        "verified_at",
        "review_after",
        "official_url",
        "source_hash",
    )
    if type(today) is not date or any(
        not isinstance(rule.get(field), str) or not rule[field].strip()
        for field in required
    ):
        return "invalid_metadata"
    try:
        effective_from = date.fromisoformat(rule["effective_from"])
        verified_at = date.fromisoformat(rule["verified_at"])
        review_after = date.fromisoformat(rule["review_after"])
    except ValueError:
        return "invalid_metadata"
    if not effective_from <= verified_at <= review_after:
        return "invalid_metadata"
    if rule["source_hash"] != _source_hash(rule["official_url"]):
        return "invalid_metadata"
    return "review_due" if today > review_after else "current"


OFFICIAL_SOURCE_MARKERS = (
    "official",
    "exchange",
    "sse",
    "szse",
    "bse",
    "csrc",
    "证监会",
    "交易所",
    "公司公告",
    "年报",
    "审计",
)
_STATEMENT_MIRROR_MARKER = "company-filed statement mirror"
_UNTRUSTED_SOURCE_MARKERS = ("news", "search", "media", "雪球", "东方财富", "新闻")


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _sources(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _official(sources: list[str], *, allow_statement_mirror: bool = False) -> bool:
    if not sources:
        return False
    lowered = " ".join(sources).lower()
    markers = OFFICIAL_SOURCE_MARKERS + (
        (_STATEMENT_MIRROR_MARKER,) if allow_statement_mirror else ()
    )
    return not any(
        marker.lower() in lowered for marker in _UNTRUSTED_SOURCE_MARKERS
    ) and any(marker.lower() in lowered for marker in markers)


def official_sources(
    sources: list[str], *, allow_statement_mirror: bool = False
) -> bool:
    return _official(sources, allow_statement_mirror=allow_statement_mirror)


def _as_of(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _base_check(
    status: str,
    reason_code: str,
    *,
    sources: list[str] | None = None,
    as_of: Any = None,
    report_period: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    sources = sources or []
    complete_evidence = bool(_as_of(as_of) and _official(sources))
    if status in {"clear", "blocked"} and not complete_evidence:
        status, reason_code = "incomplete", "missing_official_evidence"
    result: dict[str, Any] = {
        "status": status,
        "action_eligible": status in {"clear", "not_applicable"},
        "reason_code": reason_code,
        "sources": sources,
        "as_of": _as_of(as_of),
        "report_period": report_period if isinstance(report_period, str) else None,
    }
    result.update(extra)
    return result


def identify_board(code: str | None) -> tuple[str | None, str | None]:
    """Return the code-prefix candidate; it is not sufficient evidence to clear P0."""
    if not isinstance(code, str):
        return None, None
    code = code.strip()
    if code.startswith("688"):
        return "SSE", "star"
    if code.startswith("6"):
        return "SSE", "main"
    if code.startswith(("300", "301")):
        return "SZSE", "chinext"
    if code.startswith(("4", "8", "920")):
        return "BSE", "beijing"
    if code.startswith(("000", "001", "002", "003")):
        return "SZSE", "main"
    return None, None


def _provided_check(raw: Any, *, default_period: Any = None) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or raw.get("status") not in P0_STATUSES | {
        "not_applicable"
    }:
        return None
    sources = _sources(raw.get("sources", raw.get("source")))
    return _base_check(
        str(raw["status"]),
        str(raw.get("reason_code") or "provided_status"),
        sources=sources,
        as_of=raw.get("as_of"),
        report_period=raw.get("report_period", default_period),
        **{
            key: value
            for key, value in raw.items()
            if key
            not in {
                "status",
                "reason_code",
                "sources",
                "source",
                "as_of",
                "report_period",
                "action_eligible",
            }
        },
    )


def _listing_gate(
    facts: Mapping[str, Any], common: Mapping[str, Any]
) -> dict[str, Any]:
    provided = _provided_check(
        facts.get("listing_risk"), default_period=common.get("report_period")
    )
    if provided is not None:
        return provided
    # An explicit JSON null is missing evidence, not the "none" (no-risk) token.
    state = (
        str(facts.get("listing_status") or facts.get("official_status") or "")
        .strip()
        .lower()
    )
    risk = str(facts.get("risk_type") or "").strip().lower()
    if state in {
        "st",
        "*st",
        "delisting",
        "退市整理",
        "terminated",
        "终止上市",
    } or risk in {"st", "*st", "delisting", "退市整理", "terminated", "终止上市"}:
        reason = {
            "st": "listing_risk_st",
            "*st": "listing_risk_star_st",
            "delisting": "listing_risk_delisting",
            "退市整理": "listing_risk_delisting",
            "terminated": "listing_terminated",
            "终止上市": "listing_terminated",
        }.get(state or risk, "listing_risk_flagged")
        return _base_check(
            "blocked",
            reason,
            sources=_sources(facts.get("sources", facts.get("source"))),
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    if state in {"clear", "normal", "正常", "none", "no_risk"}:
        return _base_check(
            "clear",
            "listing_risk_clear",
            sources=_sources(facts.get("sources", facts.get("source"))),
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    return _base_check(
        "incomplete",
        "listing_status_missing",
        sources=[],
        as_of=None,
        report_period=common.get("report_period"),
    )


def _audit_gate(facts: Mapping[str, Any], common: Mapping[str, Any]) -> dict[str, Any]:
    provided = _provided_check(
        facts.get("audit_opinion"), default_period=common.get("report_period")
    )
    if provided is not None:
        return provided
    financial = str(facts.get("financial_opinion", "")).strip().lower()
    internal = str(facts.get("internal_control_opinion", "")).strip().lower()
    internal_disclosed = facts.get("internal_control_disclosed", True)
    bad_financial = {"qualified", "保留", "否定", "adverse", "无法表示", "disclaimer"}
    bad_internal = {"否定", "adverse", "无法表示", "disclaimer"}
    sources = _sources(facts.get("sources", facts.get("source")))
    if financial in bad_financial:
        return _base_check(
            "blocked",
            "audit_financial_non_clean",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    if internal in bad_internal or internal_disclosed is False:
        return _base_check(
            "blocked",
            "audit_internal_control_non_clean_or_missing",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    if (
        facts.get("going_concern_uncertainty")
        or facts.get("emphasis_of_matter")
        or facts.get("uncorrected_material_misstatement")
    ):
        return _base_check(
            "incomplete",
            "audit_clean_opinion_has_qualification_note",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    if financial in {"unqualified", "无保留", "clean"} and internal in {
        "unqualified",
        "无保留",
        "clean",
        "not_applicable",
        "不适用",
    }:
        return _base_check(
            "clear",
            "audit_opinion_clear",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    return _base_check(
        "incomplete",
        "audit_opinion_missing",
        sources=[],
        as_of=None,
        report_period=common.get("report_period"),
    )


def _investigation_gate(
    facts: Mapping[str, Any], common: Mapping[str, Any]
) -> dict[str, Any]:
    provided = _provided_check(
        facts.get("investigation"), default_period=common.get("report_period")
    )
    if provided is not None:
        return provided
    status = str(facts.get("investigation_status") or "").strip().lower()
    company = facts.get("company_investigated")
    related = facts.get("related_person_investigated")
    financial_impact = facts.get("affects_financial_truth") or facts.get(
        "affects_funds_or_qualification"
    )
    sources = _sources(facts.get("sources", facts.get("source")))
    if company is True or status in {"company", "formal_company", "公司", "正式立案"}:
        return _base_check(
            "blocked",
            "company_formally_investigated",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    if related is True or status in {"related_person", "控股股东", "实控人", "董监高"}:
        return _base_check(
            "blocked" if financial_impact else "incomplete",
            "related_party_investigation_material"
            if financial_impact
            else "related_party_investigation_scope_unclear",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    if status in {"clear", "none", "no_investigation", "无"}:
        return _base_check(
            "clear",
            "investigation_clear",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
        )
    return _base_check(
        "incomplete",
        "investigation_status_missing",
        sources=[],
        as_of=None,
        report_period=common.get("report_period"),
    )


def _dividend_gate(
    facts: Mapping[str, Any], common: Mapping[str, Any], rule: Mapping[str, Any] | None
) -> dict[str, Any]:
    provided = _provided_check(
        facts.get("dividend_compliance"), default_period=common.get("report_period")
    )
    if provided is not None:
        return provided
    preconditions = {
        key: _number(facts.get(key))
        for key in (
            "latest_annual_attributable_profit",
            "parent_unallocated_profit_at_year_end",
            "consolidated_unallocated_profit_at_year_end",
        )
    }
    sources = _sources(facts.get("sources", facts.get("source")))
    if rule is None:
        return _base_check(
            "incomplete",
            "dividend_rule_not_configured",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
            preconditions=preconditions,
        )
    if rule.get("dividend_ratio") is None:
        return _base_check(
            "not_applicable",
            "dividend_rule_not_applicable_bse",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
            preconditions=preconditions,
        )
    required_preconditions = [
        preconditions["latest_annual_attributable_profit"],
        preconditions["parent_unallocated_profit_at_year_end"],
    ]
    if facts.get("exchange") == "SZSE":
        required_preconditions.append(
            preconditions["consolidated_unallocated_profit_at_year_end"]
        )
    if any(value is None for value in required_preconditions):
        return _base_check(
            "incomplete",
            "dividend_preconditions_missing",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
            preconditions=preconditions,
        )
    if any(value <= 0 for value in required_preconditions if value is not None):
        return _base_check(
            "not_applicable",
            "dividend_redline_precondition_not_met",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
            preconditions=preconditions,
        )
    total_dividend = _number(facts.get("three_year_cash_dividend"))
    annual_profits = facts.get("three_year_annual_profits")
    if (
        total_dividend is None
        or not isinstance(annual_profits, (list, tuple))
        or len(annual_profits) != 3
        or not all(_finite(value) for value in annual_profits)
    ):
        return _base_check(
            "incomplete",
            "three_year_dividend_data_missing",
            sources=sources,
            as_of=facts.get("as_of"),
            report_period=common.get("report_period"),
            preconditions=preconditions,
        )
    avg_profit = sum(float(value) for value in annual_profits) / 3
    ratio_ok = total_dividend >= avg_profit * float(rule["dividend_ratio"])
    amount_ok = total_dividend >= float(rule["dividend_amount"])
    exempt = bool(facts.get("dividend_exempt"))
    return _base_check(
        "clear" if ratio_ok or amount_ok or exempt else "blocked",
        "dividend_thresholds_clear"
        if ratio_ok or amount_ok or exempt
        else "dividend_thresholds_both_breached",
        sources=sources,
        as_of=facts.get("as_of"),
        report_period=common.get("report_period"),
        preconditions=preconditions,
        three_year_cash_dividend=total_dividend,
        three_year_average_profit=avg_profit,
        ratio_threshold=float(rule["dividend_ratio"]),
        amount_threshold=float(rule["dividend_amount"]),
        ratio_ok=ratio_ok,
        amount_ok=amount_ok,
        exempt=exempt,
    )


def regulatory_gate(
    payload: Mapping[str, Any] | None = None,
    *,
    today: date | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Evaluate the P0 four-subgate regulatory contract."""
    facts: dict[str, Any] = dict(payload or {})
    facts.update(kwargs)
    facts["as_of"] = facts.get("as_of") or facts.get("checked_as_of")
    exchange = facts.get("exchange")
    board = facts.get("board")
    explicit_board_evidence = bool(exchange and board)
    if not explicit_board_evidence:
        exchange, board = identify_board(facts.get("code"))
    exchange = str(exchange).upper() if exchange else None
    board = str(board).lower() if board else None
    facts["exchange"] = exchange
    facts["board"] = board
    rule = REGULATORY_RULES.get((exchange, board)) if exchange and board else None
    rule_freshness = (
        evaluate_rule_freshness(rule, today or datetime.now(CST).date())
        if rule is not None
        else "invalid_metadata"
    )
    common = {"report_period": facts.get("report_period")}
    checks = {
        "listing_risk": _listing_gate(facts, common),
        "audit_opinion": _audit_gate(facts, common),
        "investigation": _investigation_gate(facts, common),
        "dividend_compliance": _dividend_gate(facts, common, rule),
    }
    statuses = [item["status"] for item in checks.values()]
    status = (
        "blocked"
        if "blocked" in statuses
        else "incomplete"
        if "incomplete" in statuses
        else "clear"
    )
    sources = sorted(
        {source for item in checks.values() for source in item.get("sources", [])}
    )
    as_of = _as_of(facts.get("as_of"))
    if status == "clear" and (
        not explicit_board_evidence
        or not as_of
        or not _official(sources)
        or rule is None
        or rule_freshness != "current"
    ):
        status = "incomplete"
    return {
        "status": status,
        "action_eligible": status == "clear",
        "as_of": as_of,
        "exchange": exchange,
        "board": board,
        "rule_version": rule.get("rule_version") if rule else None,
        "effective_from": rule.get("effective_from") if rule else None,
        "rule_url": rule.get("official_url") if rule else None,
        "verified_at": rule.get("verified_at") if rule else None,
        "review_after": rule.get("review_after") if rule else None,
        "source_hash": rule.get("source_hash") if rule else None,
        "rule_freshness": rule_freshness,
        "checks": checks,
        "sources": sources,
        "reason_code": "regulatory_clear"
        if status == "clear"
        else "regulatory_blocked"
        if status == "blocked"
        else "regulatory_incomplete",
    }


REGULATORY_CHECKS = ("listing_risk", "audit_opinion", "investigation", "dividend_compliance")


def regulatory_evidence_gate(
    facts: Mapping[str, Any], code: str, *, today: date | None = None
) -> dict[str, Any]:
    """Evaluate raw P0 facts and keep them so later refreshes can re-judge them.

    Pre-decided sub-check objects are rejected: the runtime, not the caller,
    decides each status.
    """
    preset = sorted(set(REGULATORY_CHECKS) & set(facts))
    if preset:
        raise ValueError(f"P0 证据只接受事实字段，不接受预设子门：{', '.join(preset)}")
    evidence = {key: value for key, value in facts.items() if key != "code"}
    gate = regulatory_gate({**evidence, "code": code}, today=today)
    gate["evidence_facts"] = evidence
    return gate


def carry_forward_regulatory_gate(
    previous: Any, code: str, annual_period: str | None, *, today: date | None = None
) -> dict[str, Any] | None:
    """Re-judge supplied P0 facts for the same annual report period.

    Per the 2026-10-22 spec, clear/blocked evidence stays valid until a newer
    annual report period appears; the stored facts are re-evaluated under the
    current rules, so changed thresholds, stale rules or non-official sources
    still decide the result.
    """
    if not isinstance(previous, Mapping) or previous.get("status") not in {
        "clear",
        "blocked",
    }:
        return None
    facts = previous.get("evidence_facts")
    if (
        not annual_period
        or not isinstance(facts, Mapping)
        or facts.get("report_period") != annual_period
    ):
        return None
    try:
        return regulatory_evidence_gate(facts, code, today=today)
    except ValueError:
        return None


def _period_values(payload: Mapping[str, Any], key: str) -> list[float] | None:
    values = payload.get(key)
    if isinstance(values, Mapping):
        values = list(values.values())
    if (
        not isinstance(values, (list, tuple))
        or not values
        or not all(_finite(value) for value in values)
    ):
        return None
    return [float(value) for value in values]


def _direction(current: float | None, mean: float | None) -> str:
    if current is None or mean is None:
        return "incomplete"
    if math.isclose(current, mean, abs_tol=1e-12):
        return "flat"
    return "up" if current > mean else "down"


def roe_structural_gate(
    payload: Mapping[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Evaluate same-basis TTM ROE against five complete fiscal years."""
    data: dict[str, Any] = dict(payload or {})
    data.update(kwargs)
    data["as_of"] = data.get("as_of") or data.get("checked_as_of")
    source_list = _sources(data.get("sources", data.get("source")))
    as_of = data.get("as_of")
    latest = _number(data.get("latest_roe_ttm"))
    if latest is None:
        annual = _number(data.get("annual_net_profit"))
        ytd = _number(data.get("current_ytd_net_profit"))
        prior_ytd = _number(data.get("prior_ytd_net_profit"))
        avg_equity = _number(data.get("average_equity"))
        if (
            annual is not None
            and ytd is not None
            and prior_ytd is not None
            and avg_equity not in (None, 0)
        ):
            latest = annual + ytd - prior_ytd
            latest = latest / avg_equity * 100
    years = (
        _period_values(data, "roe_5y")
        or _period_values(data, "roe_5y_values")
        or _period_values(data, "five_year_roe")
    )
    mean = _number(data.get("roe_5y_mean"))
    if mean is None and years is not None and len(years) == 5:
        mean = sum(years) / 5
    ratio = (
        latest / mean if latest is not None and mean is not None and mean > 0 else None
    )
    if latest is not None and latest < 0:
        status, reason = "blocked", "latest_roe_negative"
    elif latest is None:
        status, reason = "incomplete", "latest_roe_missing_or_invalid"
    elif mean is None or (years is not None and len(years) != 5):
        status, reason = "incomplete", "roe_five_year_basis_missing_or_invalid"
    elif mean <= 0:
        status, reason = "incomplete", "roe_five_year_mean_non_positive"
    elif ratio < 0.75:
        status, reason = "blocked", "roe_retention_below_0_75"
    else:
        status, reason = "clear", "roe_retention_at_or_above_0_75"
    current_dupont = (
        data.get("dupont_current")
        if isinstance(data.get("dupont_current"), Mapping)
        else {}
    )
    historical_dupont = (
        data.get("dupont_5y") if isinstance(data.get("dupont_5y"), Mapping) else {}
    )
    dupont: dict[str, Any] = {}
    for name in ("net_margin", "asset_turnover", "equity_multiplier"):
        current_value = _number(current_dupont.get(name))
        history = _period_values(historical_dupont, name)
        mean_value = (
            sum(history) / len(history) if history and len(history) == 5 else None
        )
        dupont[name] = {
            "current": current_value,
            "five_year_mean": mean_value,
            "direction": _direction(current_value, mean_value),
            "status": "ok"
            if current_value is not None and mean_value is not None
            else "incomplete",
        }
    if status in {"clear", "blocked"} and not (
        as_of and _official(source_list, allow_statement_mirror=True)
    ):
        status, reason = "incomplete", "missing_official_evidence"
    eligible = status == "clear" and bool(
        as_of and _official(source_list, allow_statement_mirror=True)
    )
    if status == "clear" and not eligible:
        status, reason = "incomplete", "missing_official_evidence"
    return {
        "status": status,
        "action_eligible": eligible if status == "clear" else False,
        "reason_code": reason,
        "latest_roe_ttm": latest,
        "roe_5y_mean": mean,
        "roe_5y_values": years,
        "roe_retention_ratio": ratio,
        "threshold": 0.75,
        "dupont": dupont,
        "sources": source_list,
        "as_of": _as_of(as_of),
        "report_period": data.get("report_period"),
    }


def _ttm_value(
    data: Mapping[str, Any],
    direct_key: str,
    annual_key: str,
    current_key: str,
    prior_key: str,
) -> float | None:
    direct = _number(data.get(direct_key))
    if direct is not None:
        return direct
    annual, current, prior = (
        _number(data.get(key)) for key in (annual_key, current_key, prior_key)
    )
    return (
        annual + current - prior
        if annual is not None and current is not None and prior is not None
        else None
    )


def cash_flow_gate(
    payload: Mapping[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Evaluate TTM CFO/Capex/FCF and the C/D cycle exception."""
    data: dict[str, Any] = dict(payload or {})
    data.update(kwargs)
    data["as_of"] = data.get("as_of") or data.get("checked_as_of")
    framework = str(data.get("framework", "")).strip()
    source_list = _sources(data.get("sources", data.get("source")))
    cfo = _ttm_value(data, "ttm_cfo", "annual_cfo", "current_ytd_cfo", "prior_ytd_cfo")
    capex = _ttm_value(
        data, "ttm_capex", "annual_capex", "current_ytd_capex", "prior_ytd_capex"
    )
    dividends = _ttm_value(
        data,
        "ttm_cash_dividends",
        "annual_cash_dividends",
        "current_ytd_cash_dividends",
        "prior_ytd_cash_dividends",
    )
    # Adapters may supply a signed cash-flow row; Capex is stored as a positive outflow.
    if capex is not None and capex < 0:
        capex = -capex
    fcf = cfo - capex if cfo is not None and capex is not None else None
    market_cap = _number(data.get("total_market_cap"))
    quote_as_of = _as_of(data.get("quote_as_of"))
    flow_as_of = _as_of(data.get("as_of"))
    fcf_yield = (
        fcf / market_cap * 100
        if fcf is not None and market_cap is not None and market_cap > 0 and quote_as_of
        else None
    )
    capex_to_cfo = (
        capex / cfo if capex is not None and cfo is not None and cfo > 0 else None
    )
    financial = framework.startswith("B") or str(data.get("industry", "")).strip() in {
        "银行",
        "证券",
        "券商",
        "保险",
    }
    if financial:
        return {
            "status": "not_applicable",
            "action_eligible": True,
            "reason_code": "financial_framework_not_applicable",
            "framework": framework,
            "ttm_cfo": cfo,
            "ttm_capex": capex,
            "ttm_cash_dividends": dividends,
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            "capex_to_cfo": capex_to_cfo,
            "cash_generation_redline": "not_applicable",
            "capex_redline": "not_applicable",
            "cd_capex_review": {"status": "not_applicable"},
            "sources": source_list,
            "as_of": flow_as_of,
            "quote_as_of": quote_as_of,
        }
    if (
        cfo is None
        or capex is None
        or not source_list
        or not flow_as_of
        or not _official(source_list, allow_statement_mirror=True)
    ):
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "cash_flow_input_missing_or_unofficial",
            "framework": framework,
            "ttm_cfo": cfo,
            "ttm_capex": capex,
            "ttm_cash_dividends": dividends,
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            "capex_to_cfo": capex_to_cfo,
            "cash_generation_redline": "incomplete",
            "capex_redline": "incomplete",
            "cd_capex_review": {"status": "incomplete"},
            "sources": source_list,
            "as_of": flow_as_of,
            "quote_as_of": quote_as_of,
        }
    if cfo <= 0:
        return {
            "status": "blocked",
            "action_eligible": False,
            "reason_code": "cfo_non_positive",
            "framework": framework,
            "ttm_cfo": cfo,
            "ttm_capex": capex,
            "ttm_cash_dividends": dividends,
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            "capex_to_cfo": capex_to_cfo,
            "cash_generation_redline": "breached",
            "capex_redline": "not_triggered",
            "cd_capex_review": {"status": "not_applicable"},
            "sources": source_list,
            "as_of": flow_as_of,
            "quote_as_of": quote_as_of,
        }
    elif capex > cfo and not framework:
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "framework_required_for_capex_review",
            "framework": framework,
            "ttm_cfo": cfo,
            "ttm_capex": capex,
            "ttm_cash_dividends": dividends,
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            "capex_to_cfo": capex_to_cfo,
            "cash_generation_redline": "clear",
            "capex_redline": "review_required",
            "cd_capex_review": {"status": "incomplete"},
            "sources": source_list,
            "as_of": flow_as_of,
            "quote_as_of": quote_as_of,
        }
    elif capex > cfo and framework in {"C资源", "D公用", "C", "D"}:
        review = (
            data.get("cd_capex_review")
            if isinstance(data.get("cd_capex_review"), Mapping)
            else {}
        )
        required = (
            "cycle_stage",
            "capex_type",
            "dividend_stress_test",
            "cash_dividend_coverage",
        )
        coverage = _number(review.get("cash_dividend_coverage"))
        if coverage is None and dividends is not None and dividends > 0:
            coverage = cfo / dividends
        coverage_not_applicable = (
            dividends == 0
            and review.get("cash_dividend_coverage") == "not_applicable"
            and bool(review.get("dividend_evidence"))
        )
        if not review:
            status, reason = "review_required", "cd_cycle_review_required"
        elif (
            not all(review.get(key) for key in required)
            or not review.get("sources")
            or not _official(_sources(review.get("sources")))
            or (coverage is None and not coverage_not_applicable)
        ):
            status, reason = "incomplete", "cd_cycle_review_incomplete"
        elif review.get("cycle_stage") not in {
            "bottom",
            "up",
            "construction",
            "底部区",
            "上行期",
            "建设期",
        } or review.get("capex_type") not in {
            "expansion",
            "acquisition",
            "construction",
            "扩产",
            "并购",
            "建设期",
            "大坝",
            "机组",
        }:
            status, reason = "blocked", "cd_capex_not_exclusive_cycle"
        elif not coverage_not_applicable and coverage < 1:
            status, reason = "blocked", "cash_dividend_coverage_below_1"
        else:
            status, reason = "clear", "cd_capex_review_clear"
        cash_redline, capex_redline = (
            "clear" if status == "clear" else status,
            "reviewed_cycle" if status == "clear" else "review_required",
        )
        review_output = dict(review)
        review_output["cash_dividend_coverage"] = (
            "not_applicable" if coverage_not_applicable else coverage
        )
        review_output["fcf_dividend_coverage"] = (
            None
            if coverage_not_applicable or dividends in (None, 0)
            else fcf / dividends
        )
        return {
            "status": status,
            "action_eligible": status == "clear",
            "reason_code": reason,
            "framework": framework,
            "ttm_cfo": cfo,
            "ttm_capex": capex,
            "ttm_cash_dividends": dividends,
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            "capex_to_cfo": capex_to_cfo,
            "cash_generation_redline": cash_redline,
            "capex_redline": capex_redline,
            "cd_capex_review": review_output,
            "sources": source_list,
            "as_of": flow_as_of,
            "quote_as_of": quote_as_of,
        }
    elif capex > cfo and framework in {"A通用", "A"}:
        review = (
            data.get("a_capex_review")
            if isinstance(data.get("a_capex_review"), Mapping)
            else {}
        )
        required = (
            "capex_type",
            "roe_ttm",
            "expansion_rationale",
        )
        roe_ttm = _number(review.get("roe_ttm"))
        capex_ratio = capex_to_cfo
        if not review:
            status, reason = "blocked", "capex_exceeds_cfo"
        elif (
            not all(review.get(key) for key in required)
            or not review.get("sources")
            or not _official(_sources(review.get("sources")), allow_statement_mirror=True)
            or roe_ttm is None
        ):
            status, reason = "incomplete", "a_capex_review_incomplete"
        elif review.get("capex_type") not in {
            "expansion",
            "acquisition",
            "capacity_expansion",
            "tech_upgrade",
            "扩产",
            "并购",
            "产能扩张",
            "技术升级",
        }:
            status, reason = "blocked", "a_capex_type_invalid"
        elif roe_ttm < 15.0:
            status, reason = "blocked", "a_capex_roe_below_threshold"
        elif capex_ratio is not None and capex_ratio > 1.25:
            status, reason = "blocked", "a_capex_ratio_above_1_25"
        else:
            status, reason = "clear", "a_capex_review_clear"
        cash_redline, capex_redline = (
            "clear" if status == "clear" else status,
            "reviewed_expansion"
            if status == "clear"
            else "breached"
            if status == "blocked"
            else "review_required",
        )
        review_output = dict(review)
        return {
            "status": status,
            "action_eligible": status == "clear",
            "reason_code": reason,
            "framework": framework,
            "ttm_cfo": cfo,
            "ttm_capex": capex,
            "ttm_cash_dividends": dividends,
            "fcf": fcf,
            "fcf_yield": fcf_yield,
            "capex_to_cfo": capex_to_cfo,
            "cash_generation_redline": cash_redline,
            "capex_redline": capex_redline,
            "cd_capex_review": {"status": "not_applicable"},
            "a_capex_review": review_output,
            "sources": source_list,
            "as_of": flow_as_of,
            "quote_as_of": quote_as_of,
        }
    status = "blocked" if capex > cfo else "clear"
    return {
        "status": status,
        "action_eligible": status == "clear",
        "reason_code": "capex_exceeds_cfo"
        if status == "blocked"
        else "cash_flow_clear",
        "framework": framework,
        "ttm_cfo": cfo,
        "ttm_capex": capex,
        "ttm_cash_dividends": dividends,
        "fcf": fcf,
        "fcf_yield": fcf_yield,
        "capex_to_cfo": capex_to_cfo,
        "cash_generation_redline": "clear",
        "capex_redline": "breached" if capex > cfo else "not_triggered",
        "cd_capex_review": {"status": "not_applicable"},
        "sources": source_list,
        "as_of": flow_as_of,
        "quote_as_of": quote_as_of,
    }


def aggregate_fundamentals_gates(
    fundamentals: Mapping[str, Any] | None,
) -> tuple[str, list[str], dict[str, dict]] | None:
    """Aggregate stored P0-P2 gates; legacy payloads are explicitly detectable."""
    data = fundamentals if isinstance(fundamentals, Mapping) else {}
    if not any(name in data for name in GATE_NAMES):
        return None
    gates: dict[str, dict] = {}
    reasons: list[str] = []
    for name in GATE_NAMES:
        value = data.get(name)
        if not isinstance(value, dict):
            value = {
                "status": "incomplete",
                "action_eligible": False,
                "reason_code": "legacy_field_absent",
            }
        status = value.get("status")
        if status not in {
            "clear",
            "blocked",
            "incomplete",
            "review_required",
            "not_applicable",
        }:
            status = "incomplete"
            value = {
                **value,
                "status": status,
                "action_eligible": False,
                "reason_code": "invalid_gate_status",
            }
        gates[name] = value
        if status != "clear" and not (
            name == "cash_flow_gate" and status == "not_applicable"
        ):
            reasons.append(f"{name}:{value.get('reason_code', 'missing_reason_code')}")
    status = (
        "blocked"
        if any(item["status"] == "blocked" for item in gates.values())
        else "incomplete"
        if reasons
        else "clear"
    )
    return status, reasons, gates


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)


def market_snapshot_status(
    snapshot: Mapping[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """Validate P3 market snapshot freshness and coverage without fetching it."""
    snap = snapshot or {}
    count = _number(snap.get("limit_down_count"))
    count_valid = count is not None and count >= 0 and count.is_integer()
    universe = _number(snap.get("universe", snap.get("eligible")))
    eligible = _number(snap.get("eligible_count", snap.get("eligible")))
    universe_valid = universe is not None and universe > 0 and universe.is_integer()
    eligible_valid = (
        eligible is not None
        and eligible >= 0
        and eligible.is_integer()
        and eligible <= universe
        if universe is not None
        else False
    )
    sources = _sources(snap.get("sources", snap.get("source")))
    as_of = _parse_time(snap.get("as_of"))
    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    age = (current - as_of).total_seconds() if as_of else None
    final = bool(snap.get("is_final", snap.get("final")))
    complete = (
        count_valid
        and universe_valid
        and eligible_valid
        and bool(snap.get("coverage", snap.get("coverage_status")))
        and bool(snap.get("status", snap.get("snapshot_status")))
        and bool(sources)
        and _official(sources)
        and as_of is not None
        and (final or age is not None and 0 <= age <= 120)
    )
    if final and as_of is not None and as_of.date() != current.date():
        complete = False
    lower_bound_confirmed = bool(
        count_valid
        and count > 500
        and _official(sources)
        and as_of is not None
        and (
            final
            and as_of.date() == current.date()
            or not final
            and age is not None
            and 0 <= age <= 120
        )
    )
    return {
        "status": "clear" if complete else "incomplete",
        "complete": complete,
        "lower_bound_confirmed": lower_bound_confirmed,
        "limit_down_count": int(count) if count_valid else None,
        "lower_bound": int(count) if count_valid else None,
        "as_of": snap.get("as_of"),
        "sources": sources,
        "reason_code": "market_snapshot_valid"
        if complete
        else "market_snapshot_incomplete",
    }


def liquidity_shock_gate(
    payload: Mapping[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Pure P3 one-shot defer state machine for a second-stop candidate."""
    data: dict[str, Any] = dict(payload or {})
    data.update(kwargs)
    if not data.get("stop_loss_triggered"):
        return {
            "status": "not_applicable",
            "action_eligible": False,
            "reason_code": "stop_loss_20_not_triggered",
        }
    raw_quote = data.get("quote")
    quote = (
        dict(raw_quote)
        if isinstance(raw_quote, Mapping)
        else {
            name: getattr(raw_quote, name, None)
            for name in (
                "price",
                "source",
                "suspended",
                "limit_down_locked",
                "trading_status",
            )
        }
        if raw_quote is not None
        else {}
    )
    price = _number(quote.get("price", data.get("price")))
    line = _number(data.get("stop_loss_20"))
    if price is None or line is None or line <= 0:
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "quote_or_stop_line_missing",
        }
    market = market_snapshot_status(data.get("market_snapshot"), data.get("now"))
    existing = (
        data.get("existing_defer")
        if isinstance(data.get("existing_defer"), Mapping)
        else {}
    )
    started = _parse_time(existing.get("defer_started_at"))
    now = (
        data.get("now") if isinstance(data.get("now"), datetime) else datetime.now(CST)
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    if started is None and market.get("lower_bound_confirmed") is True:
        started = now
    if started is not None:
        line = _number(existing.get("original_stop_loss_20")) or line
        due = started + timedelta(hours=24)
        result = {
            "status": "deferred",
            "action_eligible": False,
            "reason_code": "price_stop2_liquidity_defer",
            "defer_started_at": started.isoformat(),
            "review_due": due.isoformat(),
            "original_price": existing.get("original_price", price),
            "original_stop_loss_20": existing.get("original_stop_loss_20", line),
            "market_snapshot": data.get("market_snapshot"),
        }
        if now < due:
            return result
        trading_status = quote.get("trading_status")
        if (
            quote.get("suspended") is None
            or quote.get("limit_down_locked") is None
            or trading_status is None
            or not quote.get("source")
        ):
            result.update(
                status="incomplete", reason_code="review_quote_tradability_missing"
            )
            return result
        if (
            quote.get("suspended")
            or quote.get("limit_down_locked")
            or str(trading_status).lower() not in {"trading", "active", "可交易"}
        ):
            result.update(status="untradeable", reason_code="review_quote_untradeable")
            return result
        recovered = price > line
        result.update(
            status="clear",
            action_eligible=not recovered,
            reason_code="stop_loss_20_recovered"
            if recovered
            else "stop_loss_20_reconfirmed",
        )
        return result
    if not market["complete"] and not market["lower_bound_confirmed"]:
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "market_snapshot_incomplete",
        }
    if market["lower_bound_confirmed"]:
        return {
            "status": "deferred",
            "action_eligible": False,
            "reason_code": "price_stop2_liquidity_defer",
            "defer_started_at": now.isoformat(),
            "review_due": (now + timedelta(hours=24)).isoformat(),
            "original_price": price,
            "original_stop_loss_20": line,
            "market_snapshot": data.get("market_snapshot"),
        }
    if (
        quote.get("suspended") is None
        or quote.get("limit_down_locked") is None
        or quote.get("trading_status") is None
        or not quote.get("source")
    ):
        return {
            "status": "incomplete",
            "action_eligible": False,
            "reason_code": "quote_tradability_missing",
        }
    if (
        quote.get("suspended")
        or quote.get("limit_down_locked")
        or str(quote.get("trading_status")).lower()
        not in {"trading", "active", "可交易"}
    ):
        return {
            "status": "untradeable",
            "action_eligible": False,
            "reason_code": "quote_untradeable",
        }
    return {
        "status": "clear",
        "action_eligible": True,
        "reason_code": "stop_loss_20_clear",
        "price": price,
        "stop_loss_20": line,
    }
