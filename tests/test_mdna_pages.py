"""Section bounds on real annual reports, trimmed to page headers, footers and 第X节 lines."""

import json
from pathlib import Path

import pytest

from a_share_research.material import DASHED_PAGE_NUMBER, _page_offset, mdna_pages
from a_share_research.sources import Document


REPORTS = json.loads((Path(__file__).parent / "data/annual-report-sections.json").read_text())


def _span(code: str) -> tuple[int, int]:
    pages = mdna_pages(Document("", "", "", "", "", "", pages=REPORTS[code]["pages"]))
    return min(pages), max(pages)


def test_designed_report_without_text_headings_uses_the_table_of_contents():
    # 申通快递 2025: body headings are images; TOC 014–062, printed page = PDF page - 1.
    assert _span("002468") == (15, 63)


def test_wrapped_cross_reference_is_not_a_section_heading():
    # 恒力石化 2025: page 33 cites 第四节“经营情况讨论与分析”之… from an older report.
    assert _span("600346") == (15, 43)


def test_two_equally_common_page_numberings_give_no_offset():
    # 申通 pages 2–127 (printed = PDF page) followed by 恒力 pages 1–126 (printed = PDF page - 126):
    # each numbering covers half the document, so the MD&A could sit in either.
    pages = REPORTS["002468"]["pages"][1:127] + REPORTS["600346"]["pages"][:126]
    assert _page_offset(Document("", "", "", "", "", "", pages=pages)) is None


def test_designed_report_with_number_first_contents_and_footers():
    # 紫金矿业 2025: contents list "34   管理层讨论与分析" in two columns, left pages print "3   Annual Report 2025",
    # and the unnumbered financial statements make up most of the PDF.
    assert _span("601899") == (34, 62)


def test_red_chip_report_in_overseas_format_spans_business_and_financial_overview():
    # 中国移动 2025: no 第X节 and no 管理层讨论与分析 section; the contents list title-first dotted entries
    # 业务概览 ... 15, 财务概览 ... 17, 可持续发展报告 ... 22, and printed page = PDF page - 1.
    assert _span("600941") == (16, 23)


def test_cnooc_report_with_dash_wrapped_page_numbers():
    # 中国海油 2025: no 第X节 headings; TOC says 85–92 and footers print "- 85 -".
    report = json.loads((Path(__file__).parent / "data/cnooc-annual-report-sections.json").read_text())
    doc = Document("", "", "", "", "", "", pages=report["pages"])
    assert _page_offset(doc) == 0
    pages = mdna_pages(doc)
    assert (min(pages), max(pages)) == (85, 92)
    assert "管理层讨论与分析" in pages[85]


@pytest.mark.parametrize("footer", ["-85-", "- 85 -"])
def test_dash_wrapped_page_number_whitespace(footer):
    assert DASHED_PAGE_NUMBER.fullmatch(footer)[1] == "85"


@pytest.mark.parametrize("footer", ["-85", "85-", "-- 85 --", "- 85 - trailing", "prefix - 85 -"])
def test_malformed_variants_of_cnooc_footer_are_not_page_numbers(footer):
    assert DASHED_PAGE_NUMBER.fullmatch(footer) is None
