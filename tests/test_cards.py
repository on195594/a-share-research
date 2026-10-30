"""Investment cards read only what a captured report saved; a failed review shows none of it."""
import json
from pathlib import Path

from support.templates import ENV
from a_share_research import charts, reading

ROOT = Path(__file__).resolve().parents[1]
RECORDS = {r['report']['id']: r['report'] for r in
           json.loads((ROOT / 'tests/data/continuation-records.json').read_text())['records']}
CARD = ENV.get_template('_card.html').module


def test_card_carries_saved_verdict_risks_checkpoints_and_closing_values():
    report = RECORDS[39]
    body = report['body']
    card = reading.card(report, body)
    assert card['verdict'] == '需要谨慎' and card['bucket'] == '需要谨慎' and card['rank'] == 1
    # The verdict prefix is dropped only because the scale beside it already says it.
    assert body['summary'].startswith('需要谨慎：') and body['summary'].endswith(card['summary'])
    assert not card['summary'].startswith('需要谨慎')
    assert card['risks'] == [r['text'] for r in body['risks']]
    assert card['checkpoints'] == [c['text'] for c in body['checkpoints']]
    table = {r['id']: r for r in body['table']}
    assert [m['value'] for m in card['market']] == [
        table[k]['values'][body['market_date']] for k, _ in reading.CARD_MARKET if k in table]
    assert card['market_date'] == '2026-10-26'


def test_failed_card_shows_no_judgement_only_the_dated_last_pass():
    report = dict(RECORDS[39], passed=0)
    last = {'id': 37, 'verdict': '需要谨慎', 'created_at': RECORDS[37]['created_at']}
    card = reading.card(report, report['body'], last)
    assert card['verdict'] is None and card['bucket'] == '复核未通过'
    assert card['summary'] == '' and card['risks'] == [] and card['checkpoints'] == []
    assert card['market'] == [] and card['pulse'] == []
    html = str(CARD.invest_card(card | {'coverage': '2026-06-30', 'created_at': report['created_at']}))
    assert '最新一次研究复核未通过' in html and '2026-10-26 的报告' in html
    assert report['body']['summary'][5:20] not in html
    assert 'card-body' not in html


def test_card_text_is_never_truncated():
    report = RECORDS[39]
    card = reading.card(report, report['body']) | {'coverage': '2026-06-30', 'created_at': report['created_at']}
    compact = str(CARD.invest_card(card, ('2026-09-30', '2026 年三季报')))
    full = str(CARD.invest_card(card, full=True))
    escaped = lambda t: str(ENV.from_string('{{ t }}').render(t=t))  # noqa: E731
    assert escaped(card['risks'][0]) in compact and escaped(card['checkpoints'][0]) in compact
    assert f"共 {len(card['risks'])} 条" in compact
    assert all(escaped(t) in full for t in card['risks'] + card['checkpoints'])
    # The coverage predates the due report, so the card says so without claiming an announcement.
    assert '资料未覆盖 2026 年三季报' in compact


def test_card_buckets_match_the_home_verdict_strip():
    rows = [RECORDS[39], dict(RECORDS[25], passed=0)]
    strip = charts.verdict_summary(rows)['buckets']
    assert {r['code']: reading.card(r, r['body'])['bucket'] for r in rows} == strip
