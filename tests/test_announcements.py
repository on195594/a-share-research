"""Parsing of real cninfo periodic reports, flash reports and event announcements."""

import json
from pathlib import Path

import pytest

from a_share_research.sources import announcement_document


@pytest.mark.parametrize("item", json.loads(
    (Path(__file__).parent / "data/periodic-announcements.json").read_text()
)["items"], ids=lambda item: item["raw"]["announcementTitle"])
def test_periodic_report_titles_with_and_without_ordinal_marker(item):
    doc = announcement_document(item["raw"], item["code"])
    assert doc is not None and doc.period == item["period"]
    assert doc.title == item["raw"]["announcementTitle"]
    assert doc.url == "https://static.cninfo.com.cn/" + item["raw"]["adjunctUrl"]


def test_official_event_date_is_not_a_financial_period():
    captured = json.loads((Path(__file__).parent / "data/announcements.json").read_text())
    for item in captured["items"]:
        doc = announcement_document(item["raw"], item["code"])
        assert doc.code.startswith(item["code"])
        assert doc.title and doc.announced.startswith("2026-")
        assert doc.period == ""
        assert doc.url.startswith("https://static.cninfo.com.cn/finalpage/")
        assert announcement_document(item["raw"], "000001") is None


@pytest.mark.parametrize("truncated", [True, False])
def test_announcement_page_cap_reaches_the_query_and_the_gap_text(monkeypatch, truncated):
    """A real captured row; only the cninfo transport is replaced, keeping the suite offline."""
    from copy import deepcopy

    from a_share_research import sources
    from a_share_research.research_tools import ResearchTools
    from support.reports import MATERIAL

    item = json.loads((Path(__file__).parent / "data/announcements.json").read_text())["items"][0]
    pages = []

    def query(code, days, text, category, max_pages):
        pages.append(max_pages)
        return [item["raw"]], {"since": "2026-05-10", "until": "2026-11-06", "query": text,
                               "truncated": truncated, "scanned": 1}

    monkeypatch.setattr(sources, "_query_announcements", query)
    # Not the production value, so a hard-coded page count or row count in the gap fails here.
    monkeypatch.setattr(sources, "ANNOUNCEMENT_MAX_PAGES", 2)
    material = deepcopy(MATERIAL)
    material.extra = {"catalog": {}, "gaps": []}
    result = ResearchTools(material, None).announcements(item["code"])
    assert pages == [2]
    assert result["documents"] and result["coverage"]["truncated"] is truncated
    assert material.extra["gaps"] == (
        [f"{item['code']} 公告检索超过60条，仅返回部分目录；请缩小关键词或时间范围。"] if truncated else [])
    assert material.extra["announcement_searches"][-1] == {"code": item["code"], **result["coverage"]}
