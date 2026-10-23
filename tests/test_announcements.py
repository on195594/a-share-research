"""Parsing of real cninfo flash-report, major-event and contract announcements."""

import json
from pathlib import Path

from a_share_research.sources import announcement_document


def test_official_event_date_is_not_a_financial_period():
    captured = json.loads((Path(__file__).parent / "data/announcements.json").read_text())
    for item in captured["items"]:
        doc = announcement_document(item["raw"], item["code"])
        assert doc.code.startswith(item["code"])
        assert doc.title and doc.announced.startswith("2026-")
        assert doc.period == ""
        assert doc.url.startswith("https://static.cninfo.com.cn/finalpage/")
        assert announcement_document(item["raw"], "000001") is None
