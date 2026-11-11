import sys
from pathlib import Path

import pytest

from policy_v0.helpers import set_valid_fundamentals
from a_share_research.policy.v0 import checklist
import dataclasses
from a_share_research.policy.v0 import framework_catalog, framework_metadata


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_checklist_cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
    yield str(db_file)


def _set_a_fundamentals(*, industry="银行", **overrides):
    data = {
        "roe_3y_avg": 18.2,
        "net_profit_growth": 12.0,
        "debt_ratio": 45.0,
        "gross_margin": 35.0,
    }
    data.update(overrides)
    set_valid_fundamentals("600036", "招商银行", industry, data, ttl=24)


def _set_c_fundamentals(**overrides):
    data = {
        "roe_3y_avg": 13.0,
        "eps": 1.2,
        "debt_ratio": 44.0,
    }
    data.update(overrides)
    set_valid_fundamentals("600900", "长江电力", "电力", data, ttl=24)


def _set_f_fundamentals(**overrides):
    data = {
        "revenue_growth_3y": 35.0,
        "gross_margin": 55.0,
        "operating_cf_per_share": 1.8,
        "eps": 2.0,
    }
    data.update(overrides)
    set_valid_fundamentals("688111", "科技公司", "软件服务", data, ttl=24)


def _set_d_fundamentals(**overrides):
    data = {
        "debt_ratio": 50.0,
    }
    data.update(overrides)
    set_valid_fundamentals("600025", "华能水电", "电力", data, ttl=24)


def _set_e_fundamentals(**overrides):
    data = {
        "roe_3y_avg": 22.0,
        "net_profit_growth": 16.0,
        "gross_margin": 55.0,
    }
    data.update(overrides)
    set_valid_fundamentals("600887", "消费公司", "食品饮料", data, ttl=24)


def _set_b_fundamentals(**overrides):
    data = {
        "roe_3y_avg": 14.0,
    }
    data.update(overrides)
    set_valid_fundamentals("601988", "中国银行", "银行", data, ttl=24)


def _item_by_key(items, key):
    return next(item for item in items if item.key == key)


def _metadata(key):
    return framework_metadata.FRAMEWORK_REGISTRY[key]


def _skipped(key):
    return [
        {"label": item.label, "reason": item.reason}
        for item in _metadata(key).skipped_items
    ]


def test_a_framework_roe_excellent():
    _set_a_fundamentals(roe_3y_avg=18.2)

    items = checklist.build_checklist("600036", "A")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == "达优"
    assert item.raw_value == 18.2
    assert item.data_status == "完整"


def test_a_framework_roe_pass():
    _set_a_fundamentals(roe_3y_avg=12.0)

    items = checklist.build_checklist("600036", "A")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == "达格"


def test_a_framework_roe_fail():
    _set_a_fundamentals(roe_3y_avg=9.9)

    items = checklist.build_checklist("600036", "A")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == "未达"


def test_a_framework_gross_margin_has_only_excellent_or_fail():
    _set_a_fundamentals(gross_margin=29.9)

    items = checklist.build_checklist("600036", "A")

    item = _item_by_key(items, "gross_margin")
    assert item.pass_threshold is None
    assert item.result == "未达"
    assert item.data_status == "简化判定（不判断趋势/连续性）"

    _set_a_fundamentals(gross_margin=30.0)
    items = checklist.build_checklist("600036", "A")
    item = _item_by_key(items, "gross_margin")
    assert item.result == "未达"
    assert item.data_status == "简化判定（不判断趋势/连续性）"


@pytest.mark.parametrize(
    "industry, debt_ratio, interest_debt, expected",
    [
        ("汽车整车", 60, 17.7, "达格"),
        ("整车", 70.7, 17.7, "达格"),
        ("重型设备", 75, 0, "达格"),
        ("汽车整车", 75.1, 17.7, "未达"),
        ("汽车整车", 70.7, 40, "未达"),
        ("汽车整车", 70.7, None, "未达"),
        ("汽车整车", 70.7, -1, "未达"),
        ("汽车整车", 70.7, True, "未达"),
    ],
)
def test_a_framework_capital_intensive_debt_exception(
    industry, debt_ratio, interest_debt, expected
):
    _set_a_fundamentals(
        industry=industry,
        debt_ratio=debt_ratio,
        interest_bearing_to_total_debt=interest_debt,
    )
    items = checklist.build_checklist("600036", "A")
    debt_item = _item_by_key(items, "debt_ratio")
    assert debt_item.result == expected
    assert ("资本密集型制造例外适用" in (debt_item.note or "")) == (expected == "达格")


@pytest.mark.parametrize(
    "industry", ["银行", "软件服务", "汽车零部件", "机械", "未知", ""]
)
def test_a_framework_debt_exception_requires_specific_industry(industry):
    _set_a_fundamentals(
        industry=industry, debt_ratio=70.7, interest_bearing_to_total_debt=17.7
    )
    items = checklist.build_checklist("600036", "A")
    debt_item = _item_by_key(items, "debt_ratio")
    assert debt_item.result == "未达"
    assert "需人工核验" in debt_item.note


def test_missing_field_marks_data_missing_without_error():
    _set_a_fundamentals(roe_3y_avg=None)

    items = checklist.build_checklist("600036", "A")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"
    assert item.raw_value is None


def test_nan_field_marks_data_missing_without_error():
    _set_a_fundamentals(roe_3y_avg=float("nan"))

    items = checklist.build_checklist("600036", "A")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"
    assert item.raw_value is None


def test_format_checklist_outputs_subjective_note():
    _set_a_fundamentals()
    items = checklist.build_checklist("600036", "A")

    report = checklist.format_checklist(
        items,
        framework="A",
        code="600036",
        subjective=_metadata("A").subjective_items,
    )

    assert "框架客观指标核对清单：A通用框架 600036" in report
    assert "ROE近3年均值: 18.2%" in report
    assert "需人工主观判断（不参与代码核对）：护城河、行业地位" in report


def test_format_checklist_outputs_item_note():
    _set_c_fundamentals()
    items = checklist.build_checklist("600900", "C")

    report = checklist.format_checklist(
        items,
        framework="C",
        code="600900",
        subjective=[],
    )

    assert (
        "备注:格档判定用EPS是否非负（即是否亏损），不用净利润增速字段（增速为负不代表亏损）；优档需结合商品价格走势人工判断"
        in report
    )


@pytest.mark.parametrize(
    ("roe_3y_avg", "expected"),
    [
        (12.0, "达格"),
        (11.9, "达格"),
        (8.0, "未达"),
        (8.1, "达格"),
        (7.9, "未达"),
    ],
)
def test_c_framework_roe_thresholds(roe_3y_avg, expected):
    _set_c_fundamentals(roe_3y_avg=roe_3y_avg)

    items = checklist.build_checklist("600900", "C")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == expected


@pytest.mark.parametrize(
    ("debt_ratio", "expected"),
    [
        (45.0, "达格"),
        (45.1, "达格"),
        (64.9, "达格"),
        (65.0, "未达"),
        (65.1, "未达"),
    ],
)
def test_c_framework_debt_ratio_lower_better_thresholds(debt_ratio, expected):
    _set_c_fundamentals(debt_ratio=debt_ratio)

    items = checklist.build_checklist("600900", "C")

    item = _item_by_key(items, "debt_ratio")
    assert item.result == expected


@pytest.mark.parametrize(
    ("eps", "expected"),
    [
        (1.0, "达格"),
        (0.0, "未达"),
        (-0.1, "未达"),
    ],
)
def test_c_framework_net_profit_trend_uses_eps_only_pass_or_fail(eps, expected):
    _set_c_fundamentals(eps=eps)

    items = checklist.build_checklist("600900", "C")

    item = _item_by_key(items, "eps")
    assert item.label == "净利润增长趋势"
    assert item.unit == "元"
    assert item.excellent_threshold is None
    assert item.pass_threshold == 0
    assert item.result == expected
    assert (
        item.note
        == "格档判定用EPS是否非负（即是否亏损），不用净利润增速字段（增速为负不代表亏损）；优档需结合商品价格走势人工判断"
    )


def test_c_framework_missing_field_marks_data_missing_with_note():
    _set_c_fundamentals(eps=None)

    items = checklist.build_checklist("600900", "C")

    item = _item_by_key(items, "eps")
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"
    assert item.raw_value is None
    assert (
        item.note
        == "格档判定用EPS是否非负（即是否亏损），不用净利润增速字段（增速为负不代表亏损）；优档需结合商品价格走势人工判断"
    )


def test_c_framework_payout_ratio_growth_branch_and_rendered_note():
    _set_c_fundamentals(dps=0.60, eps=1.95)

    items = checklist.build_checklist("600900", "C")
    item = _item_by_key(items, "payout_ratio")
    note = "派息率≤40%时C框架股息率主估值轴不适用（利润主要再投资而非分配）：须改用 frameworks/C.md「成长型资源股分支」——基本面前瞻股息率15分→5分并新增产量·储量成长兑现度10分，择时主估值轴改用PB历史分位；>40%时沿用成熟资源股原权重。eps≤0时派息率无经济意义，按成熟分支处理。判据要求 dps 与 eps 同为最近完整财年口径"
    assert item.raw_value == pytest.approx(30.7692307692)
    assert item.result == "未达"
    assert item.data_status == "完整"
    assert item.note == note

    report = checklist.format_checklist(
        items, framework="C", code="600900", subjective=[]
    )
    assert item.label in report
    assert note in report


@pytest.mark.parametrize(
    ("dps", "eps", "expected_value", "expected_result", "expected_status"),
    [
        (1.5, 2.0, 75.0, "达格", "完整"),
        (0.6, 0.0, None, "数据缺失", "缺失"),
        (0.6, -1.0, None, "数据缺失", "缺失"),
        # 负派息率来自 cache.py set 手工 JSON，须判数据缺失而非算出 -30% 后
        # 因 <40% 误路由进成长型分支
        (-0.6, 1.95, None, "数据缺失", "缺失"),
        (float("inf"), 1.95, None, "数据缺失", "缺失"),
        (0.6, float("inf"), None, "数据缺失", "缺失"),
        # 40% 分支阈值的上下边界：双向钉住这个数字本身，否则改成 50% 或 30%
        # 都不会有任何测试变红。取值经挑选保证浮点精确（0.8/2.0=40.0、0.78/2.0=39.0）
        (0.8, 2.0, 40.0, "未达", "完整"),  # owner 使用严格 > 边界
        (0.78, 2.0, 39.0, "未达", "完整"),  # 阈值下调到30则变达格
    ],
)
def test_c_framework_payout_ratio_threshold_and_eps_guardrail(
    dps,
    eps,
    expected_value,
    expected_result,
    expected_status,
):
    _set_c_fundamentals(dps=dps, eps=eps)

    item = _item_by_key(checklist.build_checklist("600900", "C"), "payout_ratio")

    assert item.raw_value == expected_value
    assert item.result == expected_result
    assert item.data_status == expected_status


def test_c_framework_payout_ratio_missing_dps_is_data_missing():
    _set_c_fundamentals()

    item = _item_by_key(checklist.build_checklist("600900", "C"), "payout_ratio")

    assert item.raw_value is None
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"


def test_format_checklist_outputs_c_skipped_items():
    _set_c_fundamentals()
    items = checklist.build_checklist("600900", "C")

    report = checklist.format_checklist(
        items,
        framework="C",
        code="600900",
        subjective=_metadata("C").subjective_items,
        skipped=_skipped("C"),
    )

    assert "框架客观指标核对清单：能源/资源框架 600900" in report
    assert "checklist工具无法核验（仍需按框架文档人工评分，权重不变）：" in report
    assert (
        "- 前瞻股息率（压力测试后）：数据缺口：分红压力测试所需历史派息率/利润情景假设数据未采集，checklist工具无法核验；"
        "C.md已定义人工压力测试流程，权重不变，仍需按框架文档人工评分"
    ) in report


def test_build_checklist_c_framework_is_case_insensitive():
    _set_c_fundamentals()

    items = checklist.build_checklist("600900", "c")

    assert _item_by_key(items, "roe_3y_avg").result == "达优"


@pytest.mark.parametrize(
    ("revenue_growth_3y", "expected"),
    [
        (30.0, "达格"),
        (15.0, "未达"),
        (14.9, "未达"),
    ],
)
def test_f_framework_revenue_growth_thresholds(revenue_growth_3y, expected):
    _set_f_fundamentals(revenue_growth_3y=revenue_growth_3y)

    items = checklist.build_checklist("688111", "F")

    item = _item_by_key(items, "revenue_growth_3y")
    assert item.result == expected
    assert item.data_status == "完整"


@pytest.mark.parametrize(
    ("gross_margin", "expected"),
    [
        (50.0, "达格"),
        (30.0, "未达"),
        (29.9, "未达"),
    ],
)
def test_f_framework_gross_margin_thresholds_are_simplified(gross_margin, expected):
    _set_f_fundamentals(gross_margin=gross_margin)

    items = checklist.build_checklist("688111", "F")

    item = _item_by_key(items, "gross_margin")
    assert item.label == "毛利率及趋势"
    assert item.result == expected
    assert item.data_status == "简化判定（不判断趋势/连续性）"
    assert (
        item.note
        == '达优/达格档要求的"不下滑"/"趋势平稳"代码不做验证，仅核对当期数值是否过线，趋势需人工结合历史数据复核'
    )


def test_f_framework_gross_margin_missing_is_not_simplified():
    _set_f_fundamentals(gross_margin=None)

    items = checklist.build_checklist("688111", "F")

    item = _item_by_key(items, "gross_margin")
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"


@pytest.mark.parametrize(
    (
        "operating_cf_per_share",
        "eps",
        "expected_value",
        "expected_result",
        "expected_status",
    ),
    [
        (1.8, 2.0, 0.9, "达格", "简化判定（不判断趋势/连续性）"),
        (1.5, 2.0, 0.75, "未达", "简化判定（不判断趋势/连续性）"),
        (1.6, 2.0, 0.8, "未达", "简化判定（不判断趋势/连续性）"),
        (1.8, 0.0, None, "数据缺失", "缺失"),
        (1.8, None, None, "数据缺失", "缺失"),
        # eps<0（净利润为负）：两个负数相除会反转符号，比值方法不适用，必须判数据缺失而不是误判达标
        (-1.0, -2.0, None, "数据缺失", "缺失"),
        (1.0, -2.0, None, "数据缺失", "缺失"),
    ],
)
def test_f_framework_operating_cf_quality_ratio(
    operating_cf_per_share,
    eps,
    expected_value,
    expected_result,
    expected_status,
):
    _set_f_fundamentals(operating_cf_per_share=operating_cf_per_share, eps=eps)

    items = checklist.build_checklist("688111", "F")

    item = _item_by_key(items, "operating_cf_to_net_profit")
    assert item.label == "经营现金流质量（经营CF/净利润）"
    assert item.unit == "倍"
    assert item.excellent_threshold is None
    assert item.pass_threshold == 0.8
    assert item.raw_value == expected_value
    assert item.result == expected_result
    assert item.data_status == expected_status
    assert item.note == (
        "优档需要FCF（经营现金流-资本支出）连续多年数据，cache未采集，代码仅用经营现金流/EPS近似核对格档（经营CF/净利润>0.8）；"
        "净利润为负或零时该比值方法不适用（两个负数相除会反转符号），直接判数据缺失"
    )


def test_f_framework_rd_intensity_is_skipped_and_not_built():
    _set_f_fundamentals()

    items = checklist.build_checklist("688111", "F")

    assert _skipped("F") == [
        {
            "label": "研发投入强度（R&D/收入）",
            "reason": "数据缺口：fetcher.py未采集研发投入字段，checklist工具无法核验；"
            "该字段公开财报可查，权重不变，仍需按框架文档人工评分",
        },
    ]
    assert "研发投入强度（R&D/收入）" not in [item.label for item in items]


def test_build_checklist_f_framework_is_case_insensitive():
    _set_f_fundamentals()

    items = checklist.build_checklist("688111", "f")

    assert _item_by_key(items, "revenue_growth_3y").result == "达优"
    assert _metadata("F").checklist_name == "科技/互联网框架"


@pytest.mark.parametrize(
    ("debt_ratio", "expected"),
    [
        (55.0, "达格"),
        (55.1, "达格"),
        (69.9, "达格"),
        (70.0, "未达"),
        (70.1, "未达"),
    ],
)
def test_d_framework_debt_ratio_thresholds(debt_ratio, expected):
    _set_d_fundamentals(debt_ratio=debt_ratio)

    items = checklist.build_checklist("600025", "D")

    item = _item_by_key(items, "debt_ratio")
    assert item.result == expected
    assert item.data_status == "完整"


def test_d_framework_missing_field_marks_data_missing():
    _set_d_fundamentals(debt_ratio=None)

    items = checklist.build_checklist("600025", "D")

    item = _item_by_key(items, "debt_ratio")
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"


def test_format_checklist_outputs_d_skipped_items():
    _set_d_fundamentals()
    items = checklist.build_checklist("600025", "D")

    report = checklist.format_checklist(
        items,
        framework="D",
        code="600025",
        subjective=_metadata("D").subjective_items,
        skipped=_skipped("D"),
    )

    assert "框架客观指标核对清单：水电/公用事业框架 600025" in report
    assert "checklist工具无法核验（仍需按框架文档人工评分，权重不变）：" in report
    assert "- ROE行业相对：" in report
    assert "- 业务量增长：" in report
    assert (
        "- 前瞻股息率（压力测试后）：数据缺口：分红压力测试所需历史派息率/利润情景假设数据未采集，checklist工具无法核验；"
        "D.md已定义人工压力测试流程，权重不变，仍需按框架文档人工评分"
    ) in report


def test_build_checklist_d_framework_is_case_insensitive():
    _set_d_fundamentals()

    items = checklist.build_checklist("600025", "d")

    assert _item_by_key(items, "debt_ratio").result is not None


@pytest.mark.parametrize(
    ("roe_3y_avg", "expected"),
    [
        (20.0, "达格"),
        (19.9, "达格"),
        (12.0, "未达"),
        (11.9, "未达"),
    ],
)
def test_e_framework_roe_thresholds(roe_3y_avg, expected):
    _set_e_fundamentals(roe_3y_avg=roe_3y_avg)

    items = checklist.build_checklist("600887", "E")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == expected
    assert item.data_status == "完整"


@pytest.mark.parametrize(
    ("net_profit_growth", "expected"),
    [
        (15.0, "达格"),
        (14.9, "达格"),
        (8.0, "未达"),
        (7.9, "未达"),
    ],
)
def test_e_framework_net_profit_growth_thresholds(net_profit_growth, expected):
    _set_e_fundamentals(net_profit_growth=net_profit_growth)

    items = checklist.build_checklist("600887", "E")

    item = _item_by_key(items, "net_profit_growth")
    assert item.result == expected


@pytest.mark.parametrize(
    ("gross_margin", "expected"),
    [
        (50.0, "达格"),
        (49.9, "达格"),
        (30.0, "未达"),
        (29.9, "未达"),
    ],
)
def test_e_framework_gross_margin_thresholds_are_not_simplified(gross_margin, expected):
    """E框架毛利率无趋势修饰语，data_status须为'完整'，不能套用A/F的trend_unverified模式。"""
    _set_e_fundamentals(gross_margin=gross_margin)

    items = checklist.build_checklist("600887", "E")

    item = _item_by_key(items, "gross_margin")
    assert item.result == expected
    assert item.data_status == "完整"


def test_e_framework_missing_field_marks_data_missing():
    _set_e_fundamentals(roe_3y_avg=None)

    items = checklist.build_checklist("600887", "E")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"


def test_format_checklist_outputs_e_skipped_items():
    _set_e_fundamentals()
    items = checklist.build_checklist("600887", "E")

    report = checklist.format_checklist(
        items,
        framework="E",
        code="600887",
        subjective=_metadata("E").subjective_items,
        skipped=_skipped("E"),
    )

    assert "框架客观指标核对清单：消费框架 600887" in report
    assert (
        "- 存货周转天数：数据缺口：fetcher.py未采集存货周转天数字段，checklist工具无法核验；"
        "该字段公开财报可查，权重不变，仍需按框架文档人工评分"
    ) in report


def test_build_checklist_e_framework_is_case_insensitive():
    _set_e_fundamentals()

    items = checklist.build_checklist("600887", "e")

    assert _item_by_key(items, "roe_3y_avg").result == "达优"


def test_framework_names_include_d_and_e():
    assert _metadata("D").checklist_name == "水电/公用事业框架"
    assert _metadata("E").checklist_name == "消费框架"


@pytest.mark.parametrize(
    ("roe_3y_avg", "expected"),
    [
        (13.0, "达格"),
        (12.9, "达格"),
        (9.0, "未达"),
        (8.9, "未达"),
    ],
)
def test_b_framework_roe_thresholds(roe_3y_avg, expected):
    _set_b_fundamentals(roe_3y_avg=roe_3y_avg)

    items = checklist.build_checklist("601988", "B")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == expected
    assert item.data_status == "完整"


def test_b_framework_missing_field_marks_data_missing():
    _set_b_fundamentals(roe_3y_avg=None)

    items = checklist.build_checklist("601988", "B")

    item = _item_by_key(items, "roe_3y_avg")
    assert item.result == "数据缺失"
    assert item.data_status == "缺失"


def test_format_checklist_outputs_b_skipped_items():
    _set_b_fundamentals()
    items = checklist.build_checklist("601988", "B")

    report = checklist.format_checklist(
        items,
        framework="B",
        code="601988",
        subjective=_metadata("B").subjective_items,
        skipped=_skipped("B"),
    )

    assert "框架客观指标核对清单：银行框架 601988" in report
    assert "checklist工具无法核验（仍需按框架文档人工评分，权重不变）：" in report
    assert "- 净息差趋势：" in report
    assert "- 不良贷款率：" in report
    assert "- 拨备覆盖率：" in report
    assert "核验对象与核验来源同源，不构成独立校验" in report


def test_build_checklist_b_framework_is_case_insensitive():
    _set_b_fundamentals()

    items = checklist.build_checklist("601988", "b")

    assert _item_by_key(items, "roe_3y_avg").result == "达优"


def test_framework_names_include_b():
    assert _metadata("B").checklist_name == "银行框架"


def test_framework_registry_has_all_six_frameworks_with_required_fields():
    assert set(framework_metadata.FRAMEWORK_REGISTRY.keys()) == {
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
    }
    for key, metadata in framework_metadata.FRAMEWORK_REGISTRY.items():
        assert metadata.key == key
        assert metadata.checklist_name
        assert metadata.subjective_items
        assert metadata.checklist_definitions


def test_build_checklist_and_registry_share_same_data_source(monkeypatch):
    """证明build_checklist()读的definitions跟framework_metadata.FRAMEWORK_REGISTRY
    是同一份数据，不是两份独立拼装——monkeypatch替换B框架的definitions后，
    build_checklist的判定结果必须跟着变。"""
    _set_b_fundamentals(roe_3y_avg=99.0)
    sentinel_metadata = dataclasses.replace(
        framework_metadata.FRAMEWORK_REGISTRY["B"],
        checklist_definitions=[
            checklist.ChecklistDefinition("roe_3y_avg", "SENTINEL指标", "%"),
        ],
    )
    monkeypatch.setitem(framework_metadata.FRAMEWORK_REGISTRY, "B", sentinel_metadata)

    items = checklist.build_checklist("601988", "B")

    assert items[0].label == "SENTINEL指标"


def test_checklist_routes_classification_and_ratios_through_public_owner_api(
    monkeypatch,
):
    real_classify = checklist.framework_scoring.classify_framework_rule
    real_payout = checklist.framework_scoring.calculate_payout_ratio
    real_cash_ratio = checklist.framework_scoring.calculate_operating_cf_to_net_profit
    calls = []

    def classify(framework, rule_id, value):
        calls.append(("classify", framework, rule_id, value))
        return real_classify(framework, rule_id, value)

    def payout(dps, eps):
        calls.append(("payout", dps, eps))
        return real_payout(dps, eps)

    def cash_ratio(operating_cf_per_share, eps):
        calls.append(("cash_ratio", operating_cf_per_share, eps))
        return real_cash_ratio(operating_cf_per_share, eps)

    monkeypatch.setattr(
        checklist.framework_scoring, "classify_framework_rule", classify
    )
    monkeypatch.setattr(checklist.framework_scoring, "calculate_payout_ratio", payout)
    monkeypatch.setattr(
        checklist.framework_scoring,
        "calculate_operating_cf_to_net_profit",
        cash_ratio,
    )
    _set_c_fundamentals(dps=0.8, eps=2.0)
    _set_f_fundamentals(operating_cf_per_share=1.6, eps=2.0)

    checklist.build_checklist("600900", "C")
    checklist.build_checklist("688111", "F")

    assert ("payout", 0.8, 2.0) in calls
    assert ("cash_ratio", 1.6, 2.0) in calls
    assert ("classify", "C", "payout_ratio", 40.0) in calls
    assert ("classify", "F", "operating_cf_to_net_profit", 0.8) in calls


def test_unsupported_framework_error_message_unchanged():
    _set_a_fundamentals()
    with pytest.raises(checklist.UnsupportedFrameworkError) as exc_info:
        checklist.build_checklist("600036", "Z")
    assert str(exc_info.value) == (
        "暂不支持框架 'Z' 的 checklist；当前仅支持 'A'/'B'/'C'/'D'/'E'/'F'"
    )


def test_framework_metadata_module_has_no_circular_import():
    """干净子进程里按这个顺序import三个模块，必须不触发循环import或半初始化对象。"""
    import subprocess

    script = (
        "from a_share_research.policy.v0 import framework_metadata; from a_share_research.policy.v0 import checklist; from a_share_research.policy.v0 import cache; "
        "assert len(framework_metadata.FRAMEWORK_REGISTRY) == 6"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_framework_routing_catalog_matches_legacy_data():
    routing = framework_catalog.FRAMEWORK_ROUTING

    assert set(routing) == {"A", "B", "C", "D", "E", "F"}
    assert routing["A"] == framework_catalog.FrameworkRouting("A", "A通用", (), None)
    assert routing["B"] == framework_catalog.FrameworkRouting(
        "B", "B银行", ("银行",), (0.88, 0.82)
    )
    assert routing["C"] == framework_catalog.FrameworkRouting(
        "C",
        "C资源",
        ("煤炭", "石油", "天然气", "有色金属", "铜", "钢铁", "采矿"),
        (0.82, 0.75),
    )
    assert routing["D"] == framework_catalog.FrameworkRouting(
        "D",
        "D公用",
        ("水电", "水力发电", "电网", "水务", "燃气", "高速", "公用事业"),
        (0.88, 0.82),
    )
    assert routing["E"] == framework_catalog.FrameworkRouting(
        "E", "E消费", ("白酒", "消费", "食品", "零售", "饮料"), None
    )
    assert routing["F"] == framework_catalog.FrameworkRouting(
        "F",
        "F科技",
        ("互联网", "软件", "科技", "半导体", "游戏", "通信"),
        (0.80, 0.72),
    )
