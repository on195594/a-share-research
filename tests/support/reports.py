"""Real 贵州茅台2025年报 excerpts and a report builder for citation checks."""
from copy import deepcopy

from a_share_research.research import Material
from a_share_research.sources import Document

MATERIAL = Material(
    code="600519.SH", name="贵州茅台",
    doc=Document("600519.SH", "贵州茅台", "贵州茅台2025年年度报告", "2025-12-31", "2026-04-17", ""),
    pages={
        9: "销售费用 7,253,499,600.68 5,639,300,059.49 28.62\n销售费用变动原因说明：主要是本期广告宣传及市场费用增加。",
        10: "经营活动产生的现金流量净额变动原因说明：主要是本期公司控股子公司贵州茅台集团财务有限\n"
            "公司吸收集团公司成员单位存款减少及不可随时支取的同业存款增加。",
    },
    raw_pages={9: "销售费用 7,253,499,600.68 5,639,300,059.49 28.62"},
    table=[
        {"id": "T.revenue", "label": "营业总收入", "unit": "亿元",
         "values": {"2024-12-31": "1741.44", "2025-12-31": "1720.54"}},
        {"id": "T.op_cash", "label": "经营活动现金流净额", "unit": "亿元",
         "values": {"2024-12-31": "924.64", "2025-12-31": "615.22"}},
    ],
    market_date="2026-10-20",
)
SALES = {"page": 9, "quote": "销售费用变动原因说明：主要是本期广告宣传及市场费用增加。"}
SALES_ROW = {"page": 9, "quote": "销售费用 7,253,499,600.68 5,639,300,059.49 28.62"}
CASH = {"page": 10, "quote": "主要是本期公司控股子公司贵州茅台集团财务有限\n公司吸收集团公司成员单位存款减少"}


def report(**changes):
    base = {
        "verdict": "需要谨慎",
        "summary": "需要谨慎：2025年营业总收入1720.54亿元，低于上年。",
        "risks": [{"text": "经营现金流从924.64亿元降至615.22亿元。", "cites": [{"ref": "T.op_cash"}, CASH]}],
        "business": [{"text": "销售费用增加主要因为广告宣传。", "cites": [SALES]}],
        "financial": [{"text": "销售费用同比增长28.62%，约72.53亿元。", "cites": [SALES_ROW]}],
        "checkpoints": [{"text": "下一期经营现金流若仍低于615.22亿元，说明现金转化尚未恢复。",
                         "cites": [{"ref": "T.op_cash", "period": "2025-12-31"}]}],
    }
    return deepcopy(base | changes)

