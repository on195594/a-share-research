"""Home-page cards built from captured production reports."""

from a_share_research import db, reading
from support.continuation import BODY, load


def test_report_cards_match_cards_and_link_last_passed_report(tmp_path):
    conn = db.connect(tmp_path / 'cards.db')
    try:
        load(conn)
        reports = reading.latest_reports(conn, include_checkpoint=False)
        passed = {r['code']: r for r in reading.latest_reports(conn, passed=True, include_checkpoint=False)}
        cards = reading.report_cards(conn, reports, passed)
        assert len(cards) == len(reports) > 0
        for item, report in zip(cards, reports):
            previous = None if report['passed'] else passed.get(report['code'])
            assert item == reading.card(report, BODY[report['id']], previous) | {'from_batch': report['from_batch']}
            assert item['last_passed'] == (dict(previous) if previous else None)
        assert reading.report_cards(conn, [], {}) == []
    finally:
        conn.close()
