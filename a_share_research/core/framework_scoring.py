"""Deterministic, report-only fundamental scoring for frameworks A-F."""
from __future__ import annotations

import functools
import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from a_share_research.core.contracts import (
    CycleStage,
    FrameworkKey,
    RatingTier,
    SubjectiveAssessment,
    SubjectiveCategory,
    required_subjective_categories,
)


@dataclass(frozen=True)
class DimensionScore:
    key: str
    score: float
    max_score: float
    reason: str
    missing: bool = False


@dataclass(frozen=True)
class FrameworkScore:
    framework: FrameworkKey
    rule_version: str
    rule_hash: str
    dimensions: tuple[DimensionScore, ...]
    subtotal: float
    complete: bool
    missing_inputs: tuple[str, ...]
    red_flags: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.red_flags)


class RuleDirection(str, Enum):
    HIGHER_BETTER = "higher_better"
    LOWER_BETTER = "lower_better"


class RuleBand(str, Enum):
    EXCELLENT = "excellent"
    PASS = "pass"
    FAIL = "fail"
    MISSING = "missing"


@dataclass(frozen=True)
class FrameworkThresholdRule:
    framework: FrameworkKey
    rule_id: str
    metric_key: str
    direction: RuleDirection
    excellent_threshold: float | None
    pass_threshold: float | None


@dataclass(frozen=True)
class RuleClassification:
    rule: FrameworkThresholdRule
    value: float | None
    band: RuleBand


def _rule(
    framework: FrameworkKey,
    rule_id: str,
    metric_key: str,
    direction: RuleDirection,
    excellent: float | None,
    passed: float | None,
) -> FrameworkThresholdRule:
    return FrameworkThresholdRule(framework, rule_id, metric_key, direction, excellent, passed)


FRAMEWORK_THRESHOLD_RULES: dict[tuple[FrameworkKey, str], FrameworkThresholdRule] = {
    (rule.framework, rule.rule_id): rule
    for rule in (
        _rule(FrameworkKey.A, "roe_3y_avg", "roe_3y_avg", RuleDirection.HIGHER_BETTER, 15, 10),
        _rule(FrameworkKey.A, "net_profit_growth", "net_profit_growth_3y", RuleDirection.HIGHER_BETTER, 15, 8),
        _rule(FrameworkKey.A, "debt_ratio", "debt_ratio", RuleDirection.LOWER_BETTER, 40, 60),
        _rule(FrameworkKey.A, "gross_margin", "gross_margin", RuleDirection.HIGHER_BETTER, 30, None),
        _rule(FrameworkKey.B, "roe_3y_avg", "roe_weighted_annualized", RuleDirection.HIGHER_BETTER, 13, 9),
        _rule(FrameworkKey.C, "roe_3y_avg", "roe_3y_avg", RuleDirection.HIGHER_BETTER, 12, 8),
        _rule(FrameworkKey.C, "eps", "eps", RuleDirection.HIGHER_BETTER, None, 0),
        _rule(FrameworkKey.C, "debt_ratio", "debt_ratio", RuleDirection.LOWER_BETTER, 45, 65),
        _rule(FrameworkKey.C, "payout_ratio", "payout_ratio", RuleDirection.HIGHER_BETTER, None, 40),
        _rule(FrameworkKey.D, "debt_ratio", "debt_ratio", RuleDirection.LOWER_BETTER, 55, 70),
        _rule(FrameworkKey.E, "roe_3y_avg", "roe_3y_avg", RuleDirection.HIGHER_BETTER, 20, 12),
        _rule(FrameworkKey.E, "net_profit_growth", "net_profit_growth_3y", RuleDirection.HIGHER_BETTER, 15, 8),
        _rule(FrameworkKey.E, "gross_margin", "gross_margin", RuleDirection.HIGHER_BETTER, 50, 30),
        _rule(FrameworkKey.F, "revenue_growth_3y", "revenue_growth_3y", RuleDirection.HIGHER_BETTER, 30, 15),
        _rule(FrameworkKey.F, "gross_margin", "gross_margin", RuleDirection.HIGHER_BETTER, 50, 30),
        _rule(FrameworkKey.F, "operating_cf_to_net_profit", "operating_cf_to_net_profit", RuleDirection.HIGHER_BETTER, None, 0.8),
    )
}


def get_framework_rule(framework: FrameworkKey | str, rule_id: str) -> FrameworkThresholdRule:
    """Return one consumer-facing A-F threshold rule."""
    key = framework if isinstance(framework, FrameworkKey) else FrameworkKey(framework.upper())
    try:
        return FRAMEWORK_THRESHOLD_RULES[(key, rule_id)]
    except KeyError as exc:
        raise KeyError(f"unknown framework rule: {key.value}.{rule_id}") from exc


def framework_threshold_rules(framework: FrameworkKey | str) -> tuple[FrameworkThresholdRule, ...]:
    """Return all consumer-facing threshold rules for a framework."""
    key = framework if isinstance(framework, FrameworkKey) else FrameworkKey(framework.upper())
    return tuple(rule for (rule_framework, _), rule in FRAMEWORK_THRESHOLD_RULES.items() if rule_framework is key)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def classify_framework_rule(
    framework: FrameworkKey | str, rule_id: str, value: object
) -> RuleClassification:
    """Classify a value with strict owner thresholds; invalid values are missing."""
    rule = get_framework_rule(framework, rule_id)
    parsed = _finite_number(value)
    if parsed is None:
        return RuleClassification(rule, None, RuleBand.MISSING)
    excellent = rule.excellent_threshold
    passed = rule.pass_threshold
    if rule.direction is RuleDirection.HIGHER_BETTER:
        if excellent is not None and parsed > excellent:
            band = RuleBand.EXCELLENT
        elif passed is not None and parsed > passed:
            band = RuleBand.PASS
        else:
            band = RuleBand.FAIL
    else:
        if excellent is not None and parsed < excellent:
            band = RuleBand.EXCELLENT
        elif passed is not None and parsed < passed:
            band = RuleBand.PASS
        else:
            band = RuleBand.FAIL
    return RuleClassification(rule, parsed, band)


def calculate_payout_ratio(dps: object, eps: object) -> float | None:
    """Return payout ratio percent when inputs have economic meaning."""
    parsed_dps, parsed_eps = _finite_number(dps), _finite_number(eps)
    if parsed_dps is None or parsed_eps is None or parsed_dps < 0 or parsed_eps <= 0:
        return None
    ratio = parsed_dps / parsed_eps * 100
    return ratio if math.isfinite(ratio) else None


def calculate_operating_cf_to_net_profit(
    operating_cf_per_share: object, eps: object
) -> float | None:
    """Return the per-share OCF/profit proxy when EPS is positive."""
    parsed_cf, parsed_eps = _finite_number(operating_cf_per_share), _finite_number(eps)
    if parsed_cf is None or parsed_eps is None or parsed_eps <= 0:
        return None
    ratio = parsed_cf / parsed_eps
    return ratio if math.isfinite(ratio) else None


Metrics = Mapping[str, Any]
ScorerResult = tuple[list[DimensionScore], list[str], list[str]]
# Bump RULE_VERSION whenever scoring behavior changes (thresholds, weights,
# branches, red flags or gates). tests/test_framework_scoring.py pins both the
# rule hashes and a behavior snapshot so an unbumped change fails CI.
RULE_VERSION = "2026-08-23.v1"
RULE_HASH_SCHEME = "canonical-json.v1"


def _rule_payload(rule: FrameworkThresholdRule) -> dict[str, object]:
    return {
        "rule_id": rule.rule_id,
        "metric_key": rule.metric_key,
        "direction": rule.direction.value,
        "excellent": rule.excellent_threshold,
        "pass": rule.pass_threshold,
    }


@functools.cache
def _framework_rule_hash(key: FrameworkKey) -> str:
    payload = {
        "scheme": RULE_HASH_SCHEME,
        "rule_version": RULE_VERSION,
        "framework": key.value,
        "subjective": sorted(category.value for category in required_subjective_categories(key)),
        "thresholds": sorted(
            (_rule_payload(rule) for rule in framework_threshold_rules(key)),
            key=lambda item: str(item["rule_id"]),
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def framework_rule_hash(framework: FrameworkKey | str) -> str:
    """Stable identity of one framework's rule set, independent of source formatting."""
    key = framework if isinstance(framework, FrameworkKey) else FrameworkKey(framework.upper())
    return _framework_rule_hash(key)


def score_fundamentals(
    framework: FrameworkKey | str,
    metrics: Metrics,
    subjective: Sequence[SubjectiveAssessment],
    *,
    cycle_stage: CycleStage | None = None,
) -> FrameworkScore:
    """Score one framework without writing state or producing an action."""
    key = framework if isinstance(framework, FrameworkKey) else FrameworkKey(framework.upper())
    assessments = {item.category: item for item in subjective}
    scorers = {
        FrameworkKey.A: _score_a,
        FrameworkKey.B: _score_b,
        FrameworkKey.C: _score_c,
        FrameworkKey.D: _score_d,
        FrameworkKey.E: _score_e,
        FrameworkKey.F: _score_f,
    }
    dimensions, red_flags, gates = scorers[key](metrics, assessments, cycle_stage)
    missing = [item.key for item in dimensions if item.missing]
    missing.extend(gates)
    missing.extend(
        f"subjective:{category.value}"
        # Sorted: iterating the frozenset would make the order vary per process.
        for category in sorted(required_subjective_categories(key), key=lambda item: item.value)
        if category not in assessments
    )
    return FrameworkScore(
        framework=key,
        rule_version=RULE_VERSION,
        rule_hash=framework_rule_hash(key),
        dimensions=tuple(dimensions),
        subtotal=round(sum(item.score for item in dimensions), 2),
        complete=not missing,
        missing_inputs=tuple(dict.fromkeys(missing)),
        red_flags=tuple(red_flags),
    )


def _number(metrics: Metrics, key: str) -> float | None:
    return _finite_number(metrics.get(key))


def _boolean(metrics: Metrics, key: str) -> bool | None:
    value = metrics.get(key)
    return value if isinstance(value, bool) else None


def _missing(key: str, maximum: float) -> DimensionScore:
    return DimensionScore(key, 0.0, maximum, "missing", True)


def _higher(key: str, value: float | None, excellent: float, passed: float, maximum: float) -> DimensionScore:
    if value is None:
        return _missing(key, maximum)
    if value > excellent:
        return DimensionScore(key, maximum, maximum, f"{value:g}>{excellent:g}")
    if value > passed:
        return DimensionScore(key, maximum / 2, maximum, f"{value:g}>{passed:g}")
    return DimensionScore(key, 0.0, maximum, f"{value:g}<=pass threshold")


def _lower(key: str, value: float | None, excellent: float, passed: float, maximum: float) -> DimensionScore:
    if value is None:
        return _missing(key, maximum)
    if value < excellent:
        return DimensionScore(key, maximum, maximum, f"{value:g}<{excellent:g}")
    if value < passed:
        return DimensionScore(key, maximum / 2, maximum, f"{value:g}<{passed:g}")
    return DimensionScore(key, 0.0, maximum, f"{value:g}>=pass threshold")


def _rule_dimension(
    framework: FrameworkKey, rule_id: str, value: object, maximum: float
) -> DimensionScore:
    result = classify_framework_rule(framework, rule_id, value)
    rule, parsed = result.rule, result.value
    if parsed is None:
        return _missing(rule.metric_key, maximum)
    if result.band is RuleBand.EXCELLENT:
        score = maximum
        reason = f"{parsed:g}{'>' if rule.direction is RuleDirection.HIGHER_BETTER else '<'}{rule.excellent_threshold:g}"
    elif result.band is RuleBand.PASS:
        score = maximum / 2
        reason = f"{parsed:g}{'>' if rule.direction is RuleDirection.HIGHER_BETTER else '<'}{rule.pass_threshold:g}"
    else:
        score = 0.0
        operator = "<=" if rule.direction is RuleDirection.HIGHER_BETTER else ">="
        reason = f"{parsed:g}{operator}pass threshold"
    return DimensionScore(rule.metric_key, score, maximum, reason)


def _subjective(
    key: str,
    category: SubjectiveCategory,
    assessments: Mapping[SubjectiveCategory, SubjectiveAssessment],
    maximum: float,
) -> DimensionScore:
    item = assessments.get(category)
    if item is None:
        return _missing(key, maximum)
    score = maximum if item.rating is RatingTier.HIGH else maximum / 2
    return DimensionScore(key, score, maximum, f"{item.rating.value}; evidence={len(item.evidence)}")


def _scaled(item: DimensionScore, factor: float, reason: str) -> DimensionScore:
    return DimensionScore(item.key, round(item.score * factor, 2), item.max_score, f"{item.reason}; {reason}", item.missing)


def _common_red_flags(metrics: Metrics) -> list[str]:
    value = metrics.get("red_flags", ())
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    return ["invalid_red_flags"] if value not in (None, ()) else []


def _score_a(
    metrics: Metrics,
    assessments: Mapping[SubjectiveCategory, SubjectiveAssessment],
    _cycle: CycleStage | None,
) -> ScorerResult:
    debt = _number(metrics, "debt_ratio")
    debt_item = _rule_dimension(FrameworkKey.A, "debt_ratio", debt, 10)
    interest_debt = _number(metrics, "interest_bearing_to_total_debt")
    if debt is not None and 60 <= debt <= 75 and interest_debt is not None and interest_debt < 40:
        debt_item = DimensionScore("debt_ratio", 5, 10, "capital-intensive debt exception")
    gross_margin = _number(metrics, "gross_margin")
    stable = _boolean(metrics, "gross_margin_stable")
    margin_band = classify_framework_rule(FrameworkKey.A, "gross_margin", gross_margin).band
    if gross_margin is None or stable is None:
        margin_item = _missing("gross_margin_stability", 10)
    elif margin_band is RuleBand.EXCELLENT and stable:
        margin_item = DimensionScore("gross_margin_stability", 10, 10, "margin>30 and stable")
    elif stable:
        margin_item = DimensionScore("gross_margin_stability", 5, 10, "no material decline")
    else:
        margin_item = DimensionScore("gross_margin_stability", 0, 10, "margin declining")
    red = _common_red_flags(metrics)
    goodwill = _number(metrics, "goodwill_to_assets")
    pledge = _number(metrics, "controller_pledge_ratio")
    if goodwill is not None and goodwill > 30:
        red.append("goodwill_to_assets_above_30")
    if _boolean(metrics, "receivables_sustained_surge"):
        red.append("receivables_sustained_surge")
    if _boolean(metrics, "operating_cf_consecutive_negative"):
        red.append("operating_cf_consecutive_negative")
    if pledge is not None and pledge > 70:
        red.append("controller_pledge_above_70")
    return [
        _rule_dimension(FrameworkKey.A, "roe_3y_avg", metrics.get("roe_3y_avg"), 15),
        _rule_dimension(FrameworkKey.A, "net_profit_growth", metrics.get("net_profit_growth_3y"), 10),
        debt_item,
        margin_item,
        _subjective("moat", SubjectiveCategory.MOAT, assessments, 10),
        _subjective("industry_position", SubjectiveCategory.INDUSTRY_POSITION, assessments, 5),
    ], red, []


def _score_b(
    metrics: Metrics,
    assessments: Mapping[SubjectiveCategory, SubjectiveAssessment],
    cycle: CycleStage | None,
) -> ScorerResult:
    nim = _number(metrics, "nim")
    yoy = _number(metrics, "nim_yoy_decline_bp")
    two_year = _number(metrics, "nim_two_year_decline_bp")
    three_down = _boolean(metrics, "nim_three_periods_down")
    precise = _boolean(metrics, "bank_metrics_precise_latest")
    if nim is None or yoy is None or two_year is None or three_down is None or precise is None:
        nim_item = _missing("nim_trend", 10)
    elif nim <= 1.4 or yoy > 15 or two_year > 25:
        nim_item = DimensionScore("nim_trend", 0, 10, "NIM floor/trend red band")
    elif nim > 1.8 and yoy <= 5 and two_year <= 10 and not three_down:
        nim_item = DimensionScore("nim_trend", 10 if precise else 5, 10, "NIM excellent; precision cap applied" if not precise else "NIM excellent")
    else:
        nim_item = DimensionScore("nim_trend", 5, 10, "NIM pass band")
    npl = _lower("npl_ratio", _number(metrics, "npl_ratio"), 1.0, 1.5, 15)
    provision = _higher("provision_coverage", _number(metrics, "provision_coverage"), 300, 150, 10)
    if precise is False:
        npl = _scaled(npl, min(1.0, 7.5 / npl.score) if npl.score else 1.0, "imprecise-data cap")
        provision = _scaled(provision, min(1.0, 5 / provision.score) if provision.score else 1.0, "imprecise-data cap")
    red = _common_red_flags(metrics)
    capital = _number(metrics, "capital_adequacy_ratio")
    if capital is not None and capital < 10:
        red.append("capital_adequacy_below_10")
    if _boolean(metrics, "npl_two_years_rising") and (npl_value := _number(metrics, "npl_ratio")) is not None and npl_value > 2:
        red.append("npl_rising_above_2")
    if _boolean(metrics, "core_tier1_capital_declining"):
        red.append("core_tier1_capital_declining")
    gates = [] if cycle is not None else ["cycle_stage"]
    return [
        _rule_dimension(FrameworkKey.B, "roe_3y_avg", metrics.get("roe_weighted_annualized"), 15),
        nim_item,
        npl,
        provision,
        _subjective("moat", SubjectiveCategory.MOAT, assessments, 5),
        _subjective("industry_position", SubjectiveCategory.INDUSTRY_POSITION, assessments, 5),
    ], red, gates


def _score_c(
    metrics: Metrics,
    assessments: Mapping[SubjectiveCategory, SubjectiveAssessment],
    cycle: CycleStage | None,
) -> ScorerResult:
    dps = _number(metrics, "dps")
    eps = _number(metrics, "eps")
    same_basis = _boolean(metrics, "dps_eps_same_period_basis")
    payout_ratio = calculate_payout_ratio(dps, eps)
    payout_rule = get_framework_rule(FrameworkKey.C, "payout_ratio")
    growth = bool(
        same_basis
        and payout_ratio is not None
        and payout_rule.pass_threshold is not None
        and payout_ratio < payout_rule.pass_threshold
    )
    profit_rises = _boolean(metrics, "profit_rises_with_commodity")
    if eps is None or profit_rises is None:
        profit = _missing("net_profit_trend", 10)
    elif profit_rises:
        profit = DimensionScore("net_profit_trend", 10, 10, "rises with commodity")
    elif classify_framework_rule(FrameworkKey.C, "eps", eps).band is RuleBand.PASS:
        profit = DimensionScore("net_profit_trend", 5, 10, "not loss-making")
    else:
        profit = DimensionScore("net_profit_trend", 0, 10, "loss-making")
    yield_max = 5 if growth else 15
    stressed_yield = _number(metrics, "stressed_forward_dividend_yield")
    if stressed_yield is not None and cycle is CycleStage.DOWNTREND:
        stressed_yield *= 0.7
    yield_item = _higher("stressed_forward_dividend_yield", stressed_yield, 5, 3, yield_max)
    if cycle is CycleStage.DOWNTREND:
        profit = _scaled(profit, 0.5, "downtrend discount")
    dimensions = [
        _rule_dimension(FrameworkKey.C, "roe_3y_avg", metrics.get("roe_3y_avg"), 10),
        profit,
        _rule_dimension(FrameworkKey.C, "debt_ratio", metrics.get("debt_ratio"), 10),
        yield_item,
    ]
    if growth:
        production = _number(metrics, "production_cagr_3y")
        reserves_grew = _boolean(metrics, "equity_reserves_grew")
        if production is None or reserves_grew is None:
            dimensions.append(_missing("production_reserve_delivery", 10))
        elif production > 10 and reserves_grew:
            dimensions.append(DimensionScore("production_reserve_delivery", 10, 10, "production>10 and reserves grew"))
        elif production > 5 or reserves_grew:
            dimensions.append(DimensionScore("production_reserve_delivery", 5, 10, "production/reserves pass"))
        else:
            dimensions.append(DimensionScore("production_reserve_delivery", 0, 10, "growth not delivered"))
    reserve = _subjective("reserve_competitiveness", SubjectiveCategory.MOAT, assessments, 10)
    if _boolean(metrics, "commodity_long_term_downtrend"):
        reserve = _scaled(reserve, 0.5, "long-term commodity downtrend")
    dimensions.extend([
        reserve,
        _subjective("industry_position", SubjectiveCategory.INDUSTRY_POSITION, assessments, 5),
    ])
    red = _common_red_flags(metrics)
    debt = _number(metrics, "debt_ratio")
    reserve_life = _number(metrics, "reserve_life_years")
    if debt is not None and debt > 70:
        red.append("debt_ratio_above_70")
    if reserve_life is not None and reserve_life < 10:
        red.append("reserve_life_below_10")
    return dimensions, red, [] if cycle is not None else ["cycle_stage"]


def _score_d(
    metrics: Metrics,
    assessments: Mapping[SubjectiveCategory, SubjectiveAssessment],
    cycle: CycleStage | None,
) -> ScorerResult:
    business_growth = _number(metrics, "business_volume_growth")
    if business_growth is None:
        business = _missing("business_volume_growth", 10)
    elif business_growth > 5:
        business = DimensionScore("business_volume_growth", 10, 10, "growth>5")
    elif business_growth >= 0:
        business = DimensionScore("business_volume_growth", 5, 10, "non-negative growth")
    else:
        business = DimensionScore("business_volume_growth", 0, 10, "negative growth")
    yield_item = _higher("stressed_forward_dividend_yield", _number(metrics, "stressed_forward_dividend_yield"), 4, 2.5, 15)
    if _boolean(metrics, "policy_price_limit"):
        business = _scaled(business, 0.7, "policy-price discount")
        yield_item = _scaled(yield_item, 0.7, "policy-price discount")
    if cycle is CycleStage.DOWNTREND:
        yield_item = _scaled(yield_item, 0.7, "downtrend discount")
    if _boolean(metrics, "dividend_yield_declined_two_years"):
        yield_item = _scaled(yield_item, 0.5, "two-year dividend decline")
    roe_percentile = _number(metrics, "roe_industry_percentile")
    roe_item = _lower("roe_industry_percentile", roe_percentile, 30, 50, 10)
    red = _common_red_flags(metrics)
    debt = _number(metrics, "debt_ratio")
    current_yield = _number(metrics, "current_dividend_yield")
    if debt is not None and debt > 75:
        red.append("debt_ratio_above_75")
    if _boolean(metrics, "dividend_yield_declined_two_years") and current_yield is not None and current_yield < 2:
        red.append("dividend_decline_below_2")
    return [
        roe_item,
        business,
        _rule_dimension(FrameworkKey.D, "debt_ratio", debt, 10),
        yield_item,
        _subjective("franchise_scarcity", SubjectiveCategory.FRANCHISE_SCARCITY, assessments, 10),
        _subjective("industry_position", SubjectiveCategory.INDUSTRY_POSITION, assessments, 5),
    ], red, [] if cycle is not None else ["cycle_stage"]


def _score_e(
    metrics: Metrics,
    assessments: Mapping[SubjectiveCategory, SubjectiveAssessment],
    _cycle: CycleStage | None,
) -> ScorerResult:
    red = _common_red_flags(metrics)
    for key in ("inventory_accumulating_two_years", "channel_stuffing", "price_increase_resisted"):
        if _boolean(metrics, key):
            red.append(key)
    return [
        _rule_dimension(FrameworkKey.E, "roe_3y_avg", metrics.get("roe_3y_avg"), 15),
        _rule_dimension(FrameworkKey.E, "net_profit_growth", metrics.get("net_profit_growth_3y"), 10),
        _rule_dimension(FrameworkKey.E, "gross_margin", metrics.get("gross_margin"), 15),
        _lower("inventory_turnover_days", _number(metrics, "inventory_turnover_days"), 60, 120, 10),
        _subjective("brand_channel", SubjectiveCategory.BRAND_CHANNEL, assessments, 5),
        _subjective("industry_position", SubjectiveCategory.INDUSTRY_POSITION, assessments, 5),
    ], red, []


def _score_f(
    metrics: Metrics,
    assessments: Mapping[SubjectiveCategory, SubjectiveAssessment],
    _cycle: CycleStage | None,
) -> ScorerResult:
    margin = _number(metrics, "gross_margin")
    margin_stable = _boolean(metrics, "gross_margin_stable")
    margin_band = classify_framework_rule(FrameworkKey.F, "gross_margin", margin).band
    if margin is None or margin_stable is None:
        margin_item = _missing("gross_margin_trend", 15)
    elif margin_band is RuleBand.EXCELLENT and margin_stable:
        margin_item = DimensionScore("gross_margin_trend", 15, 15, "margin>50 and stable")
    elif margin_band is RuleBand.PASS and margin_stable:
        margin_item = DimensionScore("gross_margin_trend", 7.5, 15, "margin>30 and stable")
    else:
        margin_item = DimensionScore("gross_margin_trend", 0, 15, "margin/trend below threshold")
    fcf_positive = _boolean(metrics, "fcf_continuous_positive")
    fcf_gt_profit = _boolean(metrics, "fcf_above_net_profit")
    ocf_ratio = _number(metrics, "operating_cf_to_net_profit")
    if fcf_positive is None or fcf_gt_profit is None or ocf_ratio is None:
        cash_item = _missing("operating_cash_flow_quality", 5)
    elif fcf_positive and fcf_gt_profit:
        cash_item = DimensionScore("operating_cash_flow_quality", 5, 5, "FCF positive and above profit")
    elif classify_framework_rule(FrameworkKey.F, "operating_cf_to_net_profit", ocf_ratio).band is RuleBand.PASS:
        cash_item = DimensionScore("operating_cash_flow_quality", 2.5, 5, "OCF/profit>0.8")
    else:
        cash_item = DimensionScore("operating_cash_flow_quality", 0, 5, "cash conversion below threshold")
    red = _common_red_flags(metrics)
    for key in (
        "nonrecurring_profit_above_30",
        "top5_customers_above_50_without_contract",
        "core_technical_leader_left",
        "rd_spend_declined_two_years",
        "goodwill_assets_above_20",
    ):
        if _boolean(metrics, key):
            red.append(key)
    return [
        _rule_dimension(FrameworkKey.F, "revenue_growth_3y", metrics.get("revenue_growth_3y"), 15),
        margin_item,
        _higher("rd_to_revenue", _number(metrics, "rd_to_revenue"), 15, 8, 10),
        _subjective("order_visibility", SubjectiveCategory.MOAT, assessments, 10),
        cash_item,
        _subjective("industry_position", SubjectiveCategory.INDUSTRY_POSITION, assessments, 5),
    ], red, []
