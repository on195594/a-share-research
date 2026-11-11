from __future__ import annotations

from a_share_research.core.contracts import (
    FRAMEWORK_SUBJECTIVE_CATEGORY_MAP,
    CycleStage,
    CycleStageAssessment,
    EvidenceConfidence,
    FrameworkKey,
    RatingTier,
    SubjectiveAssessment,
    SubjectiveCategory,
    required_subjective_categories,
)


def test_framework_subjective_display_names_map_to_normalized_categories() -> None:
    assert (
        FRAMEWORK_SUBJECTIVE_CATEGORY_MAP[FrameworkKey.C]["储量竞争力"]
        is SubjectiveCategory.MOAT
    )
    assert (
        FRAMEWORK_SUBJECTIVE_CATEGORY_MAP[FrameworkKey.E]["品牌/渠道"]
        is SubjectiveCategory.BRAND_CHANNEL
    )
    assert (
        FRAMEWORK_SUBJECTIVE_CATEGORY_MAP[FrameworkKey.F]["订单能见度/客户留存"]
        is SubjectiveCategory.MOAT
    )
    for framework in FrameworkKey:
        assert len(required_subjective_categories(framework)) == 2


def test_contract_enums_and_dataclasses_construct() -> None:
    cycle = CycleStageAssessment(CycleStage.UPTREND, "订单改善")
    subjective = SubjectiveAssessment(
        SubjectiveCategory.MOAT,
        RatingTier.HIGH,
        ["高毛利稳定"],
        EvidenceConfidence.HIGH,
    )

    assert cycle.stage.value == "上行期"
    assert cycle.rationale == "订单改善"
    assert SubjectiveCategory.INDUSTRY_POSITION.value == "行业地位"
    assert SubjectiveCategory.FRANCHISE_SCARCITY.value == "特许经营稀缺性"
    assert SubjectiveCategory.BRAND_CHANNEL.value == "品牌渠道"
    assert subjective.category == SubjectiveCategory.MOAT
    assert subjective.rating.value == "优档"
    assert RatingTier.LOW.value == "格档"
    assert subjective.evidence == ["高毛利稳定"]
    assert subjective.confidence.value == "高"
    assert EvidenceConfidence.MEDIUM.value == "中"
    assert EvidenceConfidence.LOW.value == "低"
