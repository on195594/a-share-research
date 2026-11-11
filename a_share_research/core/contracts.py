"""Typed contracts for LLM-output to code-consumption boundaries.

These types formalize framework routing, cycle-stage judgment, and
subjective-quality ratings that downstream consumers historically handled
with free-text and regex heuristics.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FrameworkKey(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"


class CycleStage(str, Enum):
    UPTREND = "上行期"
    PEAK = "顶部区"
    DOWNTREND = "下行期"
    TROUGH = "底部区"


@dataclass(frozen=True)
class CycleStageAssessment:
    stage: CycleStage
    rationale: str


class SubjectiveCategory(str, Enum):
    MOAT = "护城河"
    INDUSTRY_POSITION = "行业地位"
    FRANCHISE_SCARCITY = "特许经营稀缺性"
    BRAND_CHANNEL = "品牌渠道"


# Framework prose uses different business labels for the same normalized
# quality dimension.  Downstream validators must compare the parsed enum set,
# not the display strings emitted by checklist/framework documents.
FRAMEWORK_SUBJECTIVE_CATEGORY_MAP: dict[FrameworkKey, dict[str, SubjectiveCategory]] = {
    FrameworkKey.A: {
        "护城河": SubjectiveCategory.MOAT,
        "行业地位": SubjectiveCategory.INDUSTRY_POSITION,
    },
    FrameworkKey.B: {
        "护城河": SubjectiveCategory.MOAT,
        "行业地位": SubjectiveCategory.INDUSTRY_POSITION,
    },
    FrameworkKey.C: {
        "储量竞争力": SubjectiveCategory.MOAT,
        "行业地位": SubjectiveCategory.INDUSTRY_POSITION,
    },
    FrameworkKey.D: {
        "特许经营稀缺性": SubjectiveCategory.FRANCHISE_SCARCITY,
        "行业地位": SubjectiveCategory.INDUSTRY_POSITION,
    },
    FrameworkKey.E: {
        "品牌/渠道": SubjectiveCategory.BRAND_CHANNEL,
        "行业地位": SubjectiveCategory.INDUSTRY_POSITION,
    },
    FrameworkKey.F: {
        "订单能见度/客户留存": SubjectiveCategory.MOAT,
        "行业地位": SubjectiveCategory.INDUSTRY_POSITION,
    },
}


def required_subjective_categories(framework: FrameworkKey | str) -> frozenset[SubjectiveCategory]:
    """Return normalized subjective categories required by a framework."""
    key = framework if isinstance(framework, FrameworkKey) else FrameworkKey(framework.upper())
    return frozenset(FRAMEWORK_SUBJECTIVE_CATEGORY_MAP[key].values())


class RatingTier(str, Enum):
    HIGH = "优档"
    LOW = "格档"


class EvidenceConfidence(str, Enum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


@dataclass(frozen=True)
class SubjectiveAssessment:
    category: SubjectiveCategory
    rating: RatingTier
    evidence: list[str]
    # Captured for future use, not currently consumed by any caller.
    confidence: EvidenceConfidence
