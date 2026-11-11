from __future__ import annotations

import pytest
import a_share_research.core.framework_scoring as framework_scoring

from a_share_research.core.contracts import (
    CycleStage,
    EvidenceConfidence,
    FrameworkKey,
    RatingTier,
    SubjectiveAssessment,
    SubjectiveCategory,
)
from a_share_research.core.framework_scoring import (
    RuleBand,
    RuleDirection,
    calculate_operating_cf_to_net_profit,
    calculate_payout_ratio,
    classify_framework_rule,
    get_framework_rule,
    score_fundamentals,
)


# Pinned rule identity. Intentional rule changes require a RULE_VERSION bump
# and updated hashes. Scoring behavior is covered by explicit assertions below.
_PINNED_RULE_VERSION = "2026-08-23.v1"
_PINNED_RULE_HASHES = {
    "A": "6a35e5390401e775bd9b75597f47824b212b8f6645ddbc21036611a6dcfd6408",
    "B": "7c782c1d01e8664987ace21cef826f206452a5f17f83fa2c3d73e0f7d33f4c33",
    "C": "328c0640b4f5b7d5b184f5c36a60be403996dde04f53c3ea791197fe0f509fce",
    "D": "a4558d1214c3c61bf80724e9ca88e644e60c9eff249a51ab61dd3993ba0622d3",
    "E": "2a36d441187273e6d71d30b8581e7442ae02193d6130f4feb27e4bdaaeb372ab",
    "F": "db994c0cd38248c7c83d637560046c8562d125f6d0c63db7419395567643dccd",
}


def test_rule_identity_is_pinned() -> None:
    assert framework_scoring.RULE_VERSION == _PINNED_RULE_VERSION
    assert {key.value: framework_scoring.framework_rule_hash(key) for key in FrameworkKey} == _PINNED_RULE_HASHES


def test_missing_subjective_inputs_have_stable_order() -> None:
    result = score_fundamentals("D", {}, [])
    subjective = [item for item in result.missing_inputs if item.startswith("subjective:")]
    assert subjective == sorted(subjective)


def test_rule_hash_ignores_source_formatting_and_accepts_lowercase() -> None:
    assert framework_scoring.framework_rule_hash("a") == framework_scoring.framework_rule_hash(FrameworkKey.A)
    assert len(set(_PINNED_RULE_HASHES.values())) == len(FrameworkKey)


def test_rule_hash_changes_with_rule_version(monkeypatch) -> None:
    before = framework_scoring.framework_rule_hash("A")
    monkeypatch.setattr(framework_scoring, "RULE_VERSION", "test.v2")
    framework_scoring._framework_rule_hash.cache_clear()
    try:
        assert framework_scoring.framework_rule_hash("A") != before
    finally:
        monkeypatch.undo()
        framework_scoring._framework_rule_hash.cache_clear()


def _assessment(category: SubjectiveCategory, rating: RatingTier = RatingTier.HIGH) -> SubjectiveAssessment:
    return SubjectiveAssessment(category, rating, ["verified evidence"], EvidenceConfidence.HIGH)


def _subjective(*categories: SubjectiveCategory) -> list[SubjectiveAssessment]:
    return [_assessment(category) for category in categories]


_BOUNDARY_CASES = (
    ("A", "roe_3y_avg", 15.0, 10.0, 15.0, True),
    ("B", "npl_ratio", 1.0, 1.5, 15.0, False),
    ("C", "stressed_forward_dividend_yield", 5.0, 3.0, 15.0, True),
    ("D", "debt_ratio", 55.0, 70.0, 10.0, False),
    ("E", "inventory_turnover_days", 60.0, 120.0, 10.0, False),
    ("F", "revenue_growth_3y", 30.0, 15.0, 15.0, True),
)
_EPSILON = 0.01


def _boundary_points(
    excellent: float,
    passed: float,
    maximum: float,
    higher_is_better: bool,
) -> tuple[tuple[float, str, float, str], ...]:
    if higher_is_better:
        return (
            (excellent - _EPSILON, "pass", maximum / 2, f"{excellent - _EPSILON:g}>{passed:g}"),
            (excellent, "pass", maximum / 2, f"{excellent:g}>{passed:g}"),
            (excellent + _EPSILON, "excellent", maximum, f"{excellent + _EPSILON:g}>{excellent:g}"),
            (passed - _EPSILON, "fail", 0.0, f"{passed - _EPSILON:g}<=pass threshold"),
            (passed, "fail", 0.0, f"{passed:g}<=pass threshold"),
            (passed + _EPSILON, "pass", maximum / 2, f"{passed + _EPSILON:g}>{passed:g}"),
        )
    return (
        (excellent - _EPSILON, "excellent", maximum, f"{excellent - _EPSILON:g}<{excellent:g}"),
        (excellent, "pass", maximum / 2, f"{excellent:g}<{passed:g}"),
        (excellent + _EPSILON, "pass", maximum / 2, f"{excellent + _EPSILON:g}<{passed:g}"),
        (passed - _EPSILON, "pass", maximum / 2, f"{passed - _EPSILON:g}<{passed:g}"),
        (passed, "fail", 0.0, f"{passed:g}>=pass threshold"),
        (passed + _EPSILON, "fail", 0.0, f"{passed + _EPSILON:g}>=pass threshold"),
    )


@pytest.mark.parametrize(
    ("framework", "metric", "excellent", "passed", "maximum", "higher_is_better"),
    _BOUNDARY_CASES,
)
def test_exact_boundaries_follow_documented_strict_comparisons(
    framework: str,
    metric: str,
    excellent: float,
    passed: float,
    maximum: float,
    higher_is_better: bool,
) -> None:
    expected_hash = framework_scoring.framework_rule_hash(framework)

    for value, expected_band, expected_score, expected_reason in _boundary_points(
        excellent, passed, maximum, higher_is_better
    ):
        result = score_fundamentals(framework, {metric: value}, [])
        dimension = next(item for item in result.dimensions if item.key == metric)
        actual_band = (
            "excellent"
            if dimension.score == maximum
            else "pass"
            if dimension.score == maximum / 2
            else "fail"
        )

        assert actual_band == expected_band
        assert dimension.score == expected_score
        assert dimension.reason == expected_reason
        assert result.rule_hash == expected_hash


@pytest.mark.parametrize(
    ("framework", "metrics", "subjective", "cycle"),
    [
        (
            "A",
            {
                "roe_3y_avg": 16,
                "net_profit_growth_3y": 16,
                "debt_ratio": 39,
                "gross_margin": 31,
                "gross_margin_stable": True,
            },
            _subjective(SubjectiveCategory.MOAT, SubjectiveCategory.INDUSTRY_POSITION),
            None,
        ),
        (
            "B",
            {
                "roe_weighted_annualized": 14,
                "nim": 1.9,
                "nim_yoy_decline_bp": 5,
                "nim_two_year_decline_bp": 10,
                "nim_three_periods_down": False,
                "bank_metrics_precise_latest": True,
                "npl_ratio": 0.9,
                "provision_coverage": 301,
                "capital_adequacy_ratio": 12,
            },
            _subjective(SubjectiveCategory.MOAT, SubjectiveCategory.INDUSTRY_POSITION),
            CycleStage.UPTREND,
        ),
        (
            "C",
            {
                "roe_3y_avg": 13,
                "eps": 2,
                "dps": 1,
                "dps_eps_same_period_basis": True,
                "profit_rises_with_commodity": True,
                "debt_ratio": 44,
                "stressed_forward_dividend_yield": 5.1,
                "reserve_life_years": 20,
            },
            _subjective(SubjectiveCategory.MOAT, SubjectiveCategory.INDUSTRY_POSITION),
            CycleStage.UPTREND,
        ),
        (
            "D",
            {
                "roe_industry_percentile": 29,
                "business_volume_growth": 6,
                "debt_ratio": 54,
                "stressed_forward_dividend_yield": 4.1,
                "current_dividend_yield": 4.5,
                "policy_price_limit": False,
                "dividend_yield_declined_two_years": False,
            },
            _subjective(SubjectiveCategory.FRANCHISE_SCARCITY, SubjectiveCategory.INDUSTRY_POSITION),
            CycleStage.UPTREND,
        ),
        (
            "E",
            {
                "roe_3y_avg": 21,
                "net_profit_growth_3y": 16,
                "gross_margin": 51,
                "inventory_turnover_days": 59,
            },
            _subjective(SubjectiveCategory.BRAND_CHANNEL, SubjectiveCategory.INDUSTRY_POSITION),
            None,
        ),
        (
            "F",
            {
                "revenue_growth_3y": 31,
                "gross_margin": 51,
                "gross_margin_stable": True,
                "rd_to_revenue": 16,
                "fcf_continuous_positive": True,
                "fcf_above_net_profit": True,
                "operating_cf_to_net_profit": 1.1,
            },
            _subjective(SubjectiveCategory.MOAT, SubjectiveCategory.INDUSTRY_POSITION),
            None,
        ),
    ],
)
def test_all_six_frameworks_close_at_sixty(
    framework: str,
    metrics: dict[str, object],
    subjective: list[SubjectiveAssessment],
    cycle: CycleStage | None,
) -> None:
    result = score_fundamentals(framework, metrics, subjective, cycle_stage=cycle)

    assert sum(item.max_score for item in result.dimensions) == 60
    assert result.subtotal == 60
    assert result.complete
    assert not result.blocked
    assert result.rule_version == "2026-08-23.v1"
    assert len(result.rule_hash) == 64


def test_c_growth_branch_keeps_sixty_points_and_applies_downtrend_discounts() -> None:
    result = score_fundamentals(
        FrameworkKey.C,
        {
            "roe_3y_avg": 13,
            "eps": 2,
            "dps": 0.5,
            "dps_eps_same_period_basis": True,
            "profit_rises_with_commodity": True,
            "debt_ratio": 44,
            "stressed_forward_dividend_yield": 8,
            "production_cagr_3y": 11,
            "equity_reserves_grew": True,
            "reserve_life_years": 20,
        },
        _subjective(SubjectiveCategory.MOAT, SubjectiveCategory.INDUSTRY_POSITION),
        cycle_stage=CycleStage.DOWNTREND,
    )

    scores = {item.key: item.score for item in result.dimensions}
    assert sum(item.max_score for item in result.dimensions) == 60
    assert scores["net_profit_trend"] == 5
    assert scores["stressed_forward_dividend_yield"] == 5
    assert result.complete


def test_missing_input_never_becomes_complete_investment_score() -> None:
    result = score_fundamentals(
        "E",
        {"roe_3y_avg": float("nan")},
        _subjective(SubjectiveCategory.BRAND_CHANNEL),
    )

    assert not result.complete
    assert "roe_3y_avg" in result.missing_inputs
    assert "subjective:行业地位" in result.missing_inputs


def test_red_line_blocks_but_preserves_mechanical_score() -> None:
    result = score_fundamentals(
        "D",
        {
            "roe_industry_percentile": 29,
            "business_volume_growth": 6,
            "debt_ratio": 76,
            "stressed_forward_dividend_yield": 4.1,
            "current_dividend_yield": 1.9,
            "policy_price_limit": False,
            "dividend_yield_declined_two_years": True,
        },
        _subjective(SubjectiveCategory.FRANCHISE_SCARCITY, SubjectiveCategory.INDUSTRY_POSITION),
        cycle_stage=CycleStage.UPTREND,
    )

    assert result.blocked
    assert "debt_ratio_above_75" in result.red_flags
    assert "dividend_decline_below_2" in result.red_flags
    assert result.subtotal > 0


@pytest.mark.parametrize(
    ("framework", "rule_id", "direction", "excellent", "passed"),
    [
        ("A", "roe_3y_avg", RuleDirection.HIGHER_BETTER, 15, 10),
        ("A", "net_profit_growth", RuleDirection.HIGHER_BETTER, 15, 8),
        ("A", "debt_ratio", RuleDirection.LOWER_BETTER, 40, 60),
        ("A", "gross_margin", RuleDirection.HIGHER_BETTER, 30, None),
        ("B", "roe_3y_avg", RuleDirection.HIGHER_BETTER, 13, 9),
        ("C", "roe_3y_avg", RuleDirection.HIGHER_BETTER, 12, 8),
        ("C", "eps", RuleDirection.HIGHER_BETTER, None, 0),
        ("C", "debt_ratio", RuleDirection.LOWER_BETTER, 45, 65),
        ("C", "payout_ratio", RuleDirection.HIGHER_BETTER, None, 40),
        ("D", "debt_ratio", RuleDirection.LOWER_BETTER, 55, 70),
        ("E", "roe_3y_avg", RuleDirection.HIGHER_BETTER, 20, 12),
        ("E", "net_profit_growth", RuleDirection.HIGHER_BETTER, 15, 8),
        ("E", "gross_margin", RuleDirection.HIGHER_BETTER, 50, 30),
        ("F", "revenue_growth_3y", RuleDirection.HIGHER_BETTER, 30, 15),
        ("F", "gross_margin", RuleDirection.HIGHER_BETTER, 50, 30),
        ("F", "operating_cf_to_net_profit", RuleDirection.HIGHER_BETTER, None, 0.8),
    ],
)
def test_public_owner_rules_expose_consumer_threshold_metadata(
    framework: str,
    rule_id: str,
    direction: RuleDirection,
    excellent: float | None,
    passed: float | None,
) -> None:
    rule = get_framework_rule(framework, rule_id)

    assert rule.framework is FrameworkKey(framework)
    assert rule.rule_id == rule_id
    assert rule.direction is direction
    assert rule.excellent_threshold == excellent
    assert rule.pass_threshold == passed


@pytest.mark.parametrize(
    ("framework", "rule_id", "value", "expected"),
    [
        ("A", "roe_3y_avg", 15, RuleBand.PASS),
        ("A", "roe_3y_avg", 10, RuleBand.FAIL),
        ("A", "debt_ratio", 40, RuleBand.PASS),
        ("A", "debt_ratio", 60, RuleBand.FAIL),
        ("A", "gross_margin", 30, RuleBand.FAIL),
        ("C", "eps", 0, RuleBand.FAIL),
        ("C", "payout_ratio", 40, RuleBand.FAIL),
        ("F", "operating_cf_to_net_profit", 0.8, RuleBand.FAIL),
    ],
)
def test_public_owner_classification_uses_strict_boundaries(
    framework: str, rule_id: str, value: object, expected: RuleBand
) -> None:
    result = classify_framework_rule(framework, rule_id, value)

    assert result.rule == get_framework_rule(framework, rule_id)
    assert result.value == value
    assert result.band is expected


@pytest.mark.parametrize(
    "value", [None, True, "15", float("nan"), float("inf"), 10**400]
)
def test_public_owner_classification_treats_invalid_values_as_missing(value: object) -> None:
    result = classify_framework_rule("A", "roe_3y_avg", value)

    assert result.value is None
    assert result.band is RuleBand.MISSING


def test_c_payout_boundary_keeps_existing_mature_branch_semantics() -> None:
    score = score_fundamentals(
        FrameworkKey.C,
        {
            "roe_3y_avg": 13,
            "eps": 1,
            "dps": 0.4,
            "dps_eps_same_period_basis": True,
            "profit_rises_with_commodity": True,
            "debt_ratio": 44,
            "stressed_forward_dividend_yield": 5.1,
            "reserve_life_years": 20,
        },
        _subjective(SubjectiveCategory.MOAT, SubjectiveCategory.INDUSTRY_POSITION),
        cycle_stage=CycleStage.UPTREND,
    )

    dimensions = {item.key: item for item in score.dimensions}
    assert dimensions["stressed_forward_dividend_yield"].max_score == 15
    assert "production_reserve_delivery" not in dimensions


def test_public_owner_arithmetic_helpers_fail_closed() -> None:
    assert calculate_payout_ratio(2, 4) == 50
    assert calculate_payout_ratio(-1, 4) is None
    assert calculate_payout_ratio(1, 0) is None
    assert calculate_operating_cf_to_net_profit(2, 4) == 0.5
    assert calculate_operating_cf_to_net_profit(1, 0) is None
    assert calculate_operating_cf_to_net_profit(float("inf"), 1) is None
    assert calculate_payout_ratio(1e308, 1e-308) is None
    assert calculate_operating_cf_to_net_profit(1e308, 1e-308) is None


def test_rule_hash_changes_with_public_owner_rule(monkeypatch) -> None:
    before = framework_scoring.framework_rule_hash("A")
    rule = get_framework_rule("A", "roe_3y_avg")
    monkeypatch.setitem(
        framework_scoring.FRAMEWORK_THRESHOLD_RULES,
        (FrameworkKey.A, "roe_3y_avg"),
        framework_scoring.FrameworkThresholdRule(
            rule.framework,
            rule.rule_id,
            rule.metric_key,
            rule.direction,
            16,
            rule.pass_threshold,
        ),
    )
    changed_score = score_fundamentals("A", {"roe_3y_avg": 16}, [])
    roe = next(item for item in changed_score.dimensions if item.key == "roe_3y_avg")

    assert roe.score == 7.5
    framework_scoring._framework_rule_hash.cache_clear()
    try:
        assert framework_scoring.framework_rule_hash("A") != before
    finally:
        monkeypatch.undo()
        framework_scoring._framework_rule_hash.cache_clear()


@pytest.mark.parametrize(
    ("framework", "metrics", "expected_flag", "should_trigger"),
    [
        # Framework A: goodwill > 30, controller_pledge > 70, booleans
        ("A", {"goodwill_to_assets": 30.0}, "goodwill_to_assets_above_30", False),
        ("A", {"goodwill_to_assets": 30.01}, "goodwill_to_assets_above_30", True),
        ("A", {"controller_pledge_ratio": 70.0}, "controller_pledge_above_70", False),
        ("A", {"controller_pledge_ratio": 70.01}, "controller_pledge_above_70", True),
        ("A", {"receivables_sustained_surge": True}, "receivables_sustained_surge", True),
        ("A", {"receivables_sustained_surge": False}, "receivables_sustained_surge", False),
        ("A", {"operating_cf_consecutive_negative": True}, "operating_cf_consecutive_negative", True),
        ("A", {"operating_cf_consecutive_negative": False}, "operating_cf_consecutive_negative", False),
        # Framework B: capital_adequacy < 10, npl_rising > 2, booleans
        ("B", {"capital_adequacy_ratio": 10.0}, "capital_adequacy_below_10", False),
        ("B", {"capital_adequacy_ratio": 9.99}, "capital_adequacy_below_10", True),
        ("B", {"npl_two_years_rising": True, "npl_ratio": 2.0}, "npl_rising_above_2", False),
        ("B", {"npl_two_years_rising": True, "npl_ratio": 2.01}, "npl_rising_above_2", True),
        ("B", {"npl_two_years_rising": False, "npl_ratio": 2.5}, "npl_rising_above_2", False),
        ("B", {"core_tier1_capital_declining": True}, "core_tier1_capital_declining", True),
        # Framework C: debt_ratio > 70, reserve_life < 10
        ("C", {"debt_ratio": 70.0}, "debt_ratio_above_70", False),
        ("C", {"debt_ratio": 70.01}, "debt_ratio_above_70", True),
        ("C", {"reserve_life_years": 10.0}, "reserve_life_below_10", False),
        ("C", {"reserve_life_years": 9.99}, "reserve_life_below_10", True),
        # Framework D: debt_ratio > 75, dividend_yield_declined with yield < 2
        ("D", {"debt_ratio": 75.0}, "debt_ratio_above_75", False),
        ("D", {"debt_ratio": 75.01}, "debt_ratio_above_75", True),
        (
            "D",
            {"dividend_yield_declined_two_years": True, "current_dividend_yield": 2.0},
            "dividend_decline_below_2",
            False,
        ),
        (
            "D",
            {"dividend_yield_declined_two_years": True, "current_dividend_yield": 1.99},
            "dividend_decline_below_2",
            True,
        ),
        (
            "D",
            {"dividend_yield_declined_two_years": False, "current_dividend_yield": 1.5},
            "dividend_decline_below_2",
            False,
        ),
        # Framework E booleans
        ("E", {"inventory_accumulating_two_years": True}, "inventory_accumulating_two_years", True),
        ("E", {"channel_stuffing": True}, "channel_stuffing", True),
        ("E", {"price_increase_resisted": True}, "price_increase_resisted", True),
        # Framework F booleans
        ("F", {"nonrecurring_profit_above_30": True}, "nonrecurring_profit_above_30", True),
        ("F", {"top5_customers_above_50_without_contract": True}, "top5_customers_above_50_without_contract", True),
        ("F", {"core_technical_leader_left": True}, "core_technical_leader_left", True),
        ("F", {"rd_spend_declined_two_years": True}, "rd_spend_declined_two_years", True),
        ("F", {"goodwill_assets_above_20": True}, "goodwill_assets_above_20", True),
        # Common custom red flags and malformed input fallback
        ("A", {"red_flags": ["custom_flag"]}, "custom_flag", True),
        ("A", {"red_flags": "not-a-list"}, "invalid_red_flags", True),
    ],
)
def test_red_flags_strict_boundaries_and_triggers(
    framework: str,
    metrics: dict,
    expected_flag: str,
    should_trigger: bool,
) -> None:
    score = score_fundamentals(framework, metrics, [])
    if should_trigger:
        assert expected_flag in score.red_flags
        assert score.blocked is True
    else:
        assert expected_flag not in score.red_flags
