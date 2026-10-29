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
