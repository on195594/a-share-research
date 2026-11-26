"""Unit scope of a quoted figure, on captured report pages; no page text is ever altered.

Page subsets model what a researcher has read so far; they add no financial facts.
"""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from a_share_research.material import Material
from a_share_research.research_tools import ResearchTools, calculate, unit_hints, unit_scope
from a_share_research.sources import Document

DOCUMENTS = {d["key"]: d for d in json.loads((Path(__file__).parent / "data/unit-scope-pages.json").read_text())["documents"]}


def material(key, *read):
    sample = DOCUMENTS[key]
    pages = {str(n): sample["pages"][str(n)] for n in read}
    doc = Document(**sample["document"])
    return Material(doc.code, doc.name, doc, {}, {}, [], doc.announced,
                    {"sources": {"D1": {"document": doc.info(), "pages": pages, "raw_pages": {}}}})


def operand(page, quote, value, unit):
    return {"source": "D1", "page": page, "quote": quote, "value": value, "unit": unit}


def compute(held, side):
    return calculate(held.table, "原数复算", "sum", side, side, material=held)


# 东方电缆半年报第186页：万元表在上，元表在下。
PAY = operand(186, "关键管理人员报酬                                    509.55", "509.55", "万元")
PAYABLE = operand(186, "777,932.67         728,679.98", "777,932.67", "元")


def test_two_tables_on_one_page_keep_their_own_units():
    held = material("orient_half", 186)
    for side in (PAY, PAYABLE):
        assert compute(held, side)["unit"] == side["unit"]
    for side, wrong, declared in ((PAY, "元", "万元"), (PAYABLE, "万元", "元")):
        with pytest.raises(ValueError, match=rf"页面声明单位为 {declared}（D1第186页.*不能按 {wrong} 输入"):
            compute(held, side | {"unit": wrong})


# 宁德时代三季报：第5页声明千元，合并资产负债表续到第7页，无续表字样。
DEBT = operand(7, "短期借款                             39,897,579", "39,897,579", "千元")


def test_continuation_inherits_the_unit_only_through_read_pages():
    held = material("catl_q3", 5, 6, 7)
    before = deepcopy(held.extra["sources"])
    row = compute(held, DEBT)
    assert row["unit"] == "千元" and Decimal(row["exact_values"]["result"]) == Decimal(39897579 * 2)
    with pytest.raises(ValueError, match="页面声明单位为 千元（D1第5页"):
        compute(held, DEBT | {"unit": "元"})
    # Resolving a unit never writes into the stored evidence.
    assert held.extra["sources"] == before


@pytest.mark.parametrize("read,missing,count", [((7,), 6, 5), ((5, 7), 6, 5), ((6, 7), 5, 4)])
@pytest.mark.parametrize("unit", ["千元", "元"])
def test_unread_page_breaks_the_chain_with_the_pages_to_read(read, missing, count, unit):
    with pytest.raises(ValueError, match=rf"第7页引文上方未见单位声明，第{missing}页尚未读取.*"
                                         rf"read_disclosure\(source=D1, page=2, count={count}\)"):
        compute(material("catl_q3", *read), DEBT | {"unit": unit})


# 比亚迪半年报：第28页元表之后，第29页开启“六、投资状况分析”。
INVESTED = operand(29, "22,391,316,000.00       21,781,418,000.00", "22,391,316,000.00", "元")


def test_new_section_ends_the_previous_tables_unit():
    held = material("byd_half", 28, 29)
    pages = held.source_registry()["D1"]["pages"]
    scope = unit_scope(pages, 29, pages[29].index("22,391"))
    assert (scope["units"], scope["stop"], scope["page"]) == ([], "heading", 29)
    # The new table writes （元） in its column header; nothing else is inherited across the title.
    assert compute(held, INVESTED)["unit"] == "元"
    with pytest.raises(ValueError, match="未见单位 万元.*新表标题"):
        compute(held, INVESTED | {"unit": "万元"})


def test_quote_may_carry_its_own_declaration():
    held = material("catl_q3", 5)
    quoted = operand(5, "单位：千元……货币资金                                                369,813,786",
                     "369,813,786", "千元")
    assert compute(held, quoted)["unit"] == "千元"
    with pytest.raises(ValueError, match="页面声明单位为 千元"):
        compute(held, quoted | {"unit": "万元"})


def test_hints_report_own_and_carried_units_without_touching_pages():
    sample = DOCUMENTS["catl_q3"]["pages"]
    pages = {n: sample[str(n)] for n in (5, 6, 7)}
    assert unit_hints(pages, [5, 6, 7]) == {
        "5": {"declared": ["千元"]},
        "6": {"carried": ["千元"], "from_page": 5},
        "7": {"carried": ["千元"], "from_page": 5},
    }
    assert unit_hints({7: pages[7]}, [7]) == {}  # nothing is carried over an unread page
    assert pages == {n: sample[str(n)] for n in (5, 6, 7)}


def test_read_disclosure_returns_hints_beside_untouched_text():
    held = material("catl_q3", 5)
    doc = Document(**DOCUMENTS["catl_q3"]["document"])
    doc.pages = [DOCUMENTS["catl_q3"]["pages"].get(str(n), "") for n in range(1, 8)]
    doc.raw_pages = list(doc.pages)
    held.extra["catalog"] = {"D1": doc.info()}
    tools = ResearchTools(held, None)
    tools.documents["D1"] = doc  # the captured pages stand in for the PDF download
    result = tools.run("read_disclosure", {"source": "D1", "page": 6, "count": 2})
    assert result["unit_hints"]["7"] == {"carried": ["千元"], "from_page": 5}
    assert result["pages"] == {6: doc.pages[5], 7: doc.pages[6]}
    assert held.extra["sources"]["D1"]["pages"][7] == doc.pages[6]
    assert compute(held, DEBT)["unit"] == "千元"


@pytest.mark.parametrize("prefix", ["单位：万元 币种：人民币", "关键管理人员报酬 509.55"])
def test_elided_tables_bind_unit_to_selected_value(prefix):
    held = material("orient_half", 186)
    side = PAYABLE | {"quote": prefix + "……" + PAYABLE["quote"]}
    assert compute(held, side)["unit"] == "元"
    with pytest.raises(ValueError, match="页面声明单位为 元"):
        compute(held, side | {"unit": "万元"})


@pytest.mark.parametrize("unit", ["元", "万元"])
def test_new_table_with_declaration_removed_has_no_currency(unit):
    held = material("orient_half", 186)
    pages = held.extra["sources"]["D1"]["pages"]
    # Missing-header variant of the lower table on the captured page.
    pages["186"] = pages["186"].replace("单位：元 币种：人民币", "")
    with pytest.raises(ValueError, match="未见单位"):
        compute(held, PAYABLE | {"unit": unit})


def test_missing_column_unit_does_not_default_to_yuan():
    held = material("byd_half", 28, 29)
    pages = held.extra["sources"]["D1"]["pages"]
    # Remove just the column units from the new table of the capture.
    pages["29"] = pages["29"].replace("（元）", "")
    with pytest.raises(ValueError, match="未见单位 元"):
        compute(held, INVESTED)


def test_inline_unit_on_another_occurrence_cannot_override_local_table():
    held = material("orient_half", 186)
    pages = held.extra["sources"]["D1"]["pages"]
    # Same-number variant: make the upper table's unit explicit next to its value,
    # and repeat the number in the lower table, whose declaration is 元.
    pages["186"] = pages["186"].replace("509.55", "509.55万元").replace("777,932.67", "509.55")
    side = PAY | {"quote": "关键管理人员报酬 509.55万元……509.55 728,679.98"}
    with pytest.raises(ValueError, match="页面声明单位为 元"):
        compute(held, side)


def test_moutai_dividend_unit_on_the_next_line():
    held = material("moutai_annual", 9)
    side = operand(9, "2025 年度累计派发现金红利 650.33\n亿元", "650.33", "亿元")
    assert compute(held, side)["unit"] == "亿元"
    with pytest.raises(ValueError, match="紧邻单位为 亿元"):
        compute(held, side | {"unit": "万元"})
    # A blank line separates paragraphs. It cannot attach the unit to the preceding number.
    pages = held.extra["sources"]["D1"]["pages"]
    pages["9"] = pages["9"].replace("650.33\n亿元", "650.33\n\n亿元")
    with pytest.raises(ValueError, match="未见单位"):
        compute(held, side)


def test_moutai_personnel_table_stops_currency_but_keeps_its_own_percentage():
    held = material("moutai_annual", 12)
    for quote, value in [("公司研发人员的数量 806", "806"), ("学历结构人数……博士研究生 88", "88")]:
        with pytest.raises(ValueError, match="未见单位 元"):
            compute(held, operand(12, quote, value, "元"))
    ratio = operand(12, "研发人员数量占公司总人数的比例（%） 2.30", "2.30", "%")
    assert compute(held, ratio)["unit"] == "%"
    with pytest.raises(ValueError, match="单位"):
        compute(held, ratio | {"unit": "元"})


def test_percentages_belong_to_the_selected_column():
    byd = material("byd_half", 28, 29)
    with pytest.raises(ValueError, match="单位"):
        compute(byd, INVESTED | {"unit": "%"})
    change = operand(29, "21,781,418,000.00                2.80%", "2.80", "％")
    assert compute(byd, change)["unit"] == "%"
    moutai = material("moutai_annual", 9)
    side = operand(9, "销售费用 7,253,499,600.68 5,639,300,059.49 28.62", "28.62", "%")
    assert compute(moutai, side)["unit"] == "%"
    with pytest.raises(ValueError, match="单位"):
        compute(moutai, side | {"value": "5,639,300,059.49"})


@pytest.mark.parametrize("negative", ["-509.55", "−509.55", "（509.55）"])
def test_same_amount_with_opposite_signs_uses_its_own_table(negative):
    held = material("orient_half", 186)
    pages = held.extra["sources"]["D1"]["pages"]
    # Repeat the monetary magnitude with opposite sign in the lower 元 table.
    pages["186"] = pages["186"].replace("777,932.67", negative)
    side = PAY | {"quote": f"关键管理人员报酬 509.55……{negative} 728,679.98"}
    assert compute(held, side)["unit"] == "万元"
    assert compute(held, side | {"value": "-509.55", "unit": "元"})["unit"] == "元"
    with pytest.raises(ValueError, match="声明单位为 元"):
        compute(held, side | {"value": "-509.55"})


def test_multiple_units_on_one_header_line_need_a_matching_column():
    held = material("byd_half", 29)
    pages = held.extra["sources"]["D1"]["pages"]
    # Label the original change column explicitly; then remove the individual labels,
    # leaving only a table-level legend with both units.
    pages["29"] = pages["29"].replace("变动幅度", "变动幅度（单位：%）").replace("（元）", "（单位：元）")
    assert compute(held, INVESTED)["unit"] == "元"
    with pytest.raises(ValueError, match="所在行或列声明单位为 元"):
        compute(held, INVESTED | {"unit": "%"})
    header = next(line for line in pages["29"].splitlines() if "报告期投资额" in line)
    pages["29"] = pages["29"].replace(header, "单位：元 单位：%")
    for unit in ("元", "%"):
        with pytest.raises(ValueError, match="同一行声明多个单位"):
            compute(held, INVESTED | {"unit": unit})
