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
    latest = max(p for k, r in table.items() if k.startswith('T.') for p in r['values'])
    prior = f"{int(latest[:4]) - 1}{latest[4:]}"
    # Every check-up value is the saved string for its period; the prior column is the year-earlier saved string.
    labels = dict(m for _, group in reading.CARD_DIMENSIONS for m in group) | dict(reading.CARD_MARKET)
    by_label = {v: k for k, v in labels.items()}
    for dim in card['dims']:
        for m in dim['metrics']:
            row = table[by_label[m['label']]]
            if dim['label'] == '估值':
                assert m['value'] == row['values'][body['market_date']] and m['prior'] is None
            else:
                assert m['value'] == row['values'].get(latest) and m['prior'] == row['values'].get(prior)
    assert [d['label'] for d in card['dims']][-1] == '估值'
    assert [m['label'] for m in card['figures']] == ['营收同比', '扣非同比', '市盈率', '股息率'][:len(card['figures'])]
    assert card['market_date'] == '2026-10-26'


def test_failed_card_shows_no_judgement_only_the_dated_last_pass():
    report = dict(RECORDS[39], passed=0)
    last = {'id': 37, 'verdict': '需要谨慎', 'created_at': RECORDS[37]['created_at']}
    card = reading.card(report, report['body'], last)
    assert card['verdict'] is None and card['bucket'] == '复核未通过'
    assert card['summary'] == '' and card['risks'] == [] and card['checkpoints'] == []
    assert card['dims'] == [] and card['figures'] == []
    html = str(CARD.summary_card(card | {'coverage': '2026-06-30', 'created_at': report['created_at']}))
    assert '最新一次研究复核未通过' in html and '2026-10-26 的报告' in html
    assert report['body']['summary'][5:20] not in html
    assert 'card-keys' not in html and 'card-fold' not in html


def test_card_text_is_never_truncated():
    report = RECORDS[39]
    card = reading.card(report, report['body']) | {'coverage': '2026-06-30', 'created_at': report['created_at']}
    compact = str(CARD.summary_card(card, ('2026-09-30', '2026 年三季报')))
    full = str(CARD.full_card(card))
    escaped = lambda t: str(ENV.from_string('{{ t }}').render(t=t))  # noqa: E731
    # The summary card folds the lists but keeps every item whole; the full card shows them open.
    for html in (compact, full):
        assert all(escaped(t) in html for t in card['risks'] + card['checkpoints'])
    assert f"{len(card['risks'])} 条风险与反证" in compact and '<details class="card-fold">' in compact
    # The coverage predates the due report, so the card says so without claiming an announcement.
    assert '资料未覆盖 2026 年三季报' in compact


def test_card_buckets_match_the_home_verdict_strip():
    rows = [RECORDS[39], dict(RECORDS[25], passed=0)]
    strip = charts.verdict_summary(rows)['buckets']
    assert {r['code']: reading.card(r, r['body'])['bucket'] for r in rows} == strip
