#!/usr/bin/env python3
"""框架客观指标核对清单。

本模块只展示客观指标达标情况，不计算也不输出加总总分。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from a_share_research.core import framework_scoring

from a_share_research.policy.v0 import framework_metadata
from a_share_research.policy.v0 import store
from a_share_research.policy.v0.framework_metadata import (
    FrameworkMetadata,
    SkippedChecklistItem,
)


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    label: str
    raw_value: float | None
    unit: str
    direction: str  # 'higher_better' 或 'lower_better'
    excellent_threshold: float | None
    pass_threshold: float | None
    result: str  # '达优'/'达格'/'未达'/'数据缺失'
    data_status: str  # '完整'/'简化判定（不判断趋势/连续性）'/'缺失'
    note: str | None = None


@dataclass(frozen=True)
class ChecklistDefinition:
    key: str
    label: str
    unit: str
    note: str | None = None
    trend_unverified: bool = False


class ChecklistError(Exception):
    """Checklist 构建失败的基类异常。"""


class UnsupportedFrameworkError(ChecklistError, ValueError):
    """请求了当前 checklist 工具尚未支持的框架。"""


class FundamentalsCacheMissingError(ChecklistError, LookupError):
    """指定股票没有可用的基本面缓存。"""


_RESULT_LABELS = {
    "excellent": "达优",
    "pass": "达格",
    "fail": "未达",
    "missing": "数据缺失",
}


def _build_item(
    framework: str,
    definition: ChecklistDefinition,
    value: object,
    fundamentals: dict | None = None,
) -> ChecklistItem:
    classification = framework_scoring.classify_framework_rule(
        framework,
        definition.key,
        value,
    )
    rule = classification.rule
    data_status = (
        "缺失"
        if classification.value is None
        else "简化判定（不判断趋势/连续性）"
        if definition.trend_unverified
        else "完整"
    )
    result = _RESULT_LABELS[classification.band.value]
    note = definition.note

    # A 通用框架重工/整车资本密集型制造负债率例外条款：
    # 负债率在 60%—75% 之间且有息负债率 < 40% 时视同格档
    if (
        framework == "A"
        and definition.key == "debt_ratio"
        and classification.value is not None
        and 60 <= classification.value <= 75
        and fundamentals is not None
    ):
        interest_debt = fundamentals.get("interest_bearing_to_total_debt")
        # 只自动识别规则明确列出的类别，宽泛行业仍需人工核验。
        eligible_industry = fundamentals.get("_cache_meta", {}).get("industry") in (
            "汽车整车",
            "整车",
            "重型设备",
        )
        if (
            eligible_industry
            and isinstance(interest_debt, (int, float))
            and not isinstance(interest_debt, bool)
            and isfinite(interest_debt)
            and 0 <= interest_debt < 40
        ):
            result = "达格"
            exception_note = f"资本密集型制造例外适用：负债率≤75%且有息负债率（{interest_debt:.1f}%）<40%"
            note = f"{note}；{exception_note}" if note else exception_note
        elif not eligible_industry:
            note = "未确认属于整车/重型设备等资本密集型制造，例外适用性需人工核验"

    return ChecklistItem(
        key=definition.key,
        label=definition.label,
        raw_value=classification.value,
        unit=definition.unit,
        direction=rule.direction.value,
        excellent_threshold=rule.excellent_threshold,
        pass_threshold=rule.pass_threshold,
        result=result,
        data_status=data_status,
        note=note,
    )


def _build_f_operating_cf_quality_item(fundamentals: dict) -> ChecklistItem:
    definition = ChecklistDefinition(
        key="operating_cf_to_net_profit",
        label="经营现金流质量（经营CF/净利润）",
        unit="倍",
        note="优档需要FCF（经营现金流-资本支出）连续多年数据，cache未采集，代码仅用经营现金流/EPS近似核对格档（经营CF/净利润>0.8）；净利润为负或零时该比值方法不适用（两个负数相除会反转符号），直接判数据缺失",
        trend_unverified=True,
    )
    ratio = framework_scoring.calculate_operating_cf_to_net_profit(
        fundamentals.get("operating_cf_per_share"),
        fundamentals.get("eps"),
    )
    return _build_item("F", definition, ratio)


def _build_c_payout_ratio_item(fundamentals: dict) -> ChecklistItem:
    definition = ChecklistDefinition(
        key="payout_ratio",
        label="派息率（>40%=成熟分支 / ≤40%=成长分支）",
        unit="%",
        note="派息率≤40%时C框架股息率主估值轴不适用（利润主要再投资而非分配）：须改用 frameworks/C.md「成长型资源股分支」——基本面前瞻股息率15分→5分并新增产量·储量成长兑现度10分，择时主估值轴改用PB历史分位；>40%时沿用成熟资源股原权重。eps≤0时派息率无经济意义，按成熟分支处理。判据要求 dps 与 eps 同为最近完整财年口径",
    )
    payout_ratio = framework_scoring.calculate_payout_ratio(
        fundamentals.get("dps"),
        fundamentals.get("eps"),
    )
    return _build_item("C", definition, payout_ratio)


def _register(metadata: FrameworkMetadata) -> None:
    framework_metadata.FRAMEWORK_REGISTRY[metadata.key] = metadata


_register(
    FrameworkMetadata(
        key="A",
        checklist_name="A通用框架",
        subjective_items=["护城河", "行业地位"],
        skipped_items=[],
        checklist_definitions=[
            ChecklistDefinition("roe_3y_avg", "ROE近3年均值", "%"),
            ChecklistDefinition("net_profit_growth", "净利润增速近3年", "%"),
            ChecklistDefinition("debt_ratio", "资产负债率", "%"),
            ChecklistDefinition(
                "gross_margin",
                "毛利率",
                "%",
                note='达优档要求的"稳定"/格档"无明显下滑"代码不做验证，仅核对当期数值是否过线，趋势需人工结合历史数据复核；'
                '格档本身无数值线（"无明显下滑"是质性判断），"未达"只代表未过30%优档线，不代表完全不及格',
                trend_unverified=True,
            ),
        ],
    )
)

_register(
    FrameworkMetadata(
        key="B",
        checklist_name="银行框架",
        subjective_items=["护城河", "行业地位"],
        skipped_items=[
            SkippedChecklistItem(
                label="净息差趋势",
                reason="数据缺口：该字段无AKShare API，需人工检索公开来源后手动写入缓存"
                "（fetcher.py FIELDS注册表标注来源为web），核验对象与核验来源同源，不构成独立校验；"
                "权重不变，仍需结合公开来源人工评分",
            ),
            SkippedChecklistItem(
                label="不良贷款率",
                reason="数据缺口：该字段无AKShare API，需人工检索公开来源后手动写入缓存"
                "（fetcher.py FIELDS注册表标注来源为web），核验对象与核验来源同源，不构成独立校验；"
                "权重不变，仍需结合公开来源人工评分",
            ),
            SkippedChecklistItem(
                label="拨备覆盖率",
                reason="数据缺口：该字段无AKShare API，需人工检索公开来源后手动写入缓存"
                "（fetcher.py FIELDS注册表标注来源为web），核验对象与核验来源同源，不构成独立校验；"
                "权重不变，仍需结合公开来源人工评分",
            ),
        ],
        checklist_definitions=[
            ChecklistDefinition("roe_3y_avg", "ROE加权年化", "%"),
        ],
    )
)

_register(
    FrameworkMetadata(
        key="C",
        checklist_name="能源/资源框架",
        subjective_items=["储量竞争力", "行业地位"],
        skipped_items=[
            SkippedChecklistItem(
                label="前瞻股息率（压力测试后）",
                reason="数据缺口：分红压力测试所需历史派息率/利润情景假设数据未采集，checklist工具无法核验；"
                "C.md已定义人工压力测试流程，权重不变，仍需按框架文档人工评分",
            ),
        ],
        checklist_definitions=[
            ChecklistDefinition("roe_3y_avg", "ROE近3年均值", "%"),
            ChecklistDefinition(
                "eps",
                "净利润增长趋势",
                "元",
                note="格档判定用EPS是否非负（即是否亏损），不用净利润增速字段（增速为负不代表亏损）；优档需结合商品价格走势人工判断",
            ),
            ChecklistDefinition("debt_ratio", "资产负债率", "%"),
        ],
        custom_builder=_build_c_payout_ratio_item,
    )
)

_register(
    FrameworkMetadata(
        key="D",
        checklist_name="水电/公用事业框架",
        subjective_items=["特许经营稀缺性", "行业地位"],
        skipped_items=[
            SkippedChecklistItem(
                label="ROE行业相对",
                reason="数据缺口：fetcher.py仅采集ROE绝对值（roe_3y_avg），未做同行业分位排名，"
                "checklist工具无法核验是否处于行业前30%/50%分位；权重不变，仍需结合同业对比人工评分",
            ),
            SkippedChecklistItem(
                label="业务量增长",
                reason="数据缺口：业务量（如发电量/售电量）由实物量驱动，fetcher.py仅采集财务报表字段，"
                "未采集业务量数据，checklist工具无法核验；权重不变，仍需按框架文档人工评分",
            ),
            SkippedChecklistItem(
                label="前瞻股息率（压力测试后）",
                reason="数据缺口：分红压力测试所需历史派息率/利润情景假设数据未采集，checklist工具无法核验；"
                "D.md已定义人工压力测试流程，权重不变，仍需按框架文档人工评分",
            ),
        ],
        checklist_definitions=[
            ChecklistDefinition("debt_ratio", "资产负债率", "%"),
        ],
    )
)

_register(
    FrameworkMetadata(
        key="E",
        checklist_name="消费框架",
        subjective_items=["品牌/渠道", "行业地位"],
        skipped_items=[
            SkippedChecklistItem(
                label="存货周转天数",
                reason="数据缺口：fetcher.py未采集存货周转天数字段，checklist工具无法核验；"
                "该字段公开财报可查，权重不变，仍需按框架文档人工评分",
            ),
        ],
        checklist_definitions=[
            ChecklistDefinition("roe_3y_avg", "ROE近3年均值", "%"),
            ChecklistDefinition("net_profit_growth", "净利润增速", "%"),
            ChecklistDefinition("gross_margin", "毛利率", "%"),
        ],
    )
)

_register(
    FrameworkMetadata(
        key="F",
        checklist_name="科技/互联网框架",
        subjective_items=["订单能见度/客户留存", "行业地位"],
        skipped_items=[
            SkippedChecklistItem(
                label="研发投入强度（R&D/收入）",
                reason="数据缺口：fetcher.py未采集研发投入字段，checklist工具无法核验；"
                "该字段公开财报可查，权重不变，仍需按框架文档人工评分",
            ),
        ],
        checklist_definitions=[
            ChecklistDefinition("revenue_growth_3y", "收入增速近3年均值", "%"),
            ChecklistDefinition(
                "gross_margin",
                "毛利率及趋势",
                "%",
                note='达优/达格档要求的"不下滑"/"趋势平稳"代码不做验证，仅核对当期数值是否过线，趋势需人工结合历史数据复核',
                trend_unverified=True,
            ),
        ],
        custom_builder=_build_f_operating_cf_quality_item,
    )
)


def build_checklist(code: str, framework: str) -> list[ChecklistItem]:
    """基于缓存数据构建框架客观指标核对清单。"""
    normalized_framework = framework.upper()
    metadata = framework_metadata.FRAMEWORK_REGISTRY.get(normalized_framework)
    if metadata is None:
        raise UnsupportedFrameworkError(
            f"暂不支持框架 {framework!r} 的 checklist；当前仅支持 'A'/'B'/'C'/'D'/'E'/'F'"
        )

    fundamentals = store.get_fundamentals(code)
    if fundamentals is None:
        raise FundamentalsCacheMissingError(f"未找到 {code} 的有效基本面缓存")

    items = [
        _build_item(
            normalized_framework,
            definition,
            fundamentals.get(definition.key),
            fundamentals=fundamentals,
        )
        for definition in metadata.checklist_definitions
    ]
    if metadata.custom_builder is not None:
        items.append(metadata.custom_builder(fundamentals))
    return items


def format_checklist(
    items: list[ChecklistItem],
    framework: str,
    code: str,
    subjective: list[str],
    skipped: list[dict] | None = None,
) -> str:
    """生成人类可读的 checklist 文本报告。"""
    normalized_framework = framework.upper()
    metadata = framework_metadata.FRAMEWORK_REGISTRY.get(normalized_framework)
    framework_name = (
        metadata.checklist_name if metadata else f"{normalized_framework}框架"
    )

    lines = [
        f"框架客观指标核对清单：{framework_name} {code}",
        "────────────────────────────────",
    ]
    for item in items:
        line = (
            f"{item.label}: {_format_value(item.raw_value, item.unit)} | "
            f"{_format_thresholds(item)} | "
            f"结果:{item.result} | 数据:{item.data_status}"
        )
        if item.note:
            line += f" | 备注:{item.note}"
        lines.append(line)

    if subjective:
        lines.extend(
            [
                "",
                f"需人工主观判断（不参与代码核对）：{'、'.join(subjective)}",
            ]
        )

    if skipped:
        lines.extend(
            [
                "",
                "checklist工具无法核验（仍需按框架文档人工评分，权重不变）：",
            ]
        )
        for skipped_item in skipped:
            lines.append(f"- {skipped_item['label']}：{skipped_item['reason']}")

    return "\n".join(lines)


def _format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "数据缺失"
    return f"{value:.1f}{unit}"


def _format_thresholds(item: ChecklistItem) -> str:
    excellent = _format_threshold(
        "优线", item.direction, item.excellent_threshold, item.unit
    )
    if item.pass_threshold is None:
        return excellent
    passing = _format_threshold("格线", item.direction, item.pass_threshold, item.unit)
    return f"{excellent} {passing}"


def _format_threshold(
    label: str, direction: str, threshold: float | None, unit: str
) -> str:
    if threshold is None:
        return f"{label}未设"
    op = ">" if direction == "higher_better" else "<"
    return f"{label}{op}{_format_number(threshold)}{unit}"


def _format_number(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)
