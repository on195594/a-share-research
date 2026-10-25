"""Section bounds on real annual reports, trimmed to page headers, footers and 第X节 lines."""

import json
from pathlib import Path

from a_share_research.research import mdna_pages
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
