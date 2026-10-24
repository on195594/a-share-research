"""Display checks using dates observed on real reports, without provider calls."""

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
REPORTS = json.loads((ROOT / "tests/data/report-date-observations.json").read_text())["reports"]
TEMPLATES = Environment(
    loader=FileSystemLoader(ROOT / "a_share_research/templates"),
    autoescape=select_autoescape(),
)


def test_maotai_discloses_distinct_snapshot_dates_without_calling_price_expired():
    report = REPORTS[0]
    notice = str(TEMPLATES.get_template("_macros.html").module.snapshot_date_notice(
        report["market_date"], report["price"],
    ))
    assert 'role="status"' in notice
    assert 'href="#price"' in notice
    assert "估值快照依据 2026-10-23 收盘" in notice
    assert "依据 2026-10-20 收盘，有效至 2026-10-27" in notice
    assert "有效期不表示行情已更新" in notice
    assert "过期" not in notice


def test_industrial_fulian_same_date_has_no_mismatch_notice():
    report = REPORTS[1]
    notice = str(TEMPLATES.get_template("_macros.html").module.snapshot_date_notice(
        report["market_date"], report["price"],
    ))
    assert not notice.strip()
