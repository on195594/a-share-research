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
    # The first counter-evidence stays outside the fold, as visible as the thesis; the rest fold.
    first, fold = compact.index(escaped(card['risks'][0])), compact.index('<details class="card-fold">')
    assert first < fold and f"其余 {len(card['risks']) - 1} 条风险与反证" in compact
    assert 'claim=risks:1#claim-risks-1' in compact[fold:] and 'claim=risks:0#' not in compact[fold:]
    # The coverage predates the due report, so the card says so without claiming an announcement.
    assert '资料未覆盖 2026 年三季报' in compact


def test_card_buckets_match_the_home_filter_counts():
    rows = [RECORDS[39], dict(RECORDS[25], passed=0)]
    groups = charts.verdict_summary(rows)['buckets']
    assert {r['code']: reading.card(r, r['body'])['bucket'] for r in rows} == groups


def test_card_renders_standalone_with_report_dates_and_unicode_minus():
    report = RECORDS[39]
    card = reading.card(report, report['body'])
    # Standalone card has created_at and coverage populated from report
    assert card['created_at'] == report['created_at']
    assert card['coverage'] is not None
    # Render without manual dict merge
    summary_html = str(CARD.summary_card(card))
    full_html = str(CARD.full_card(card))
    assert '研究于' in summary_html
    assert '财务概览' in full_html and '累计 · 括号内为上年同期' in full_html

    # Test signed values with unicode minus sign − and ascii minus -
    mock_metric_ascii = {'label': '测试A', 'unit': '%', 'value': '-5.20', 'signed': True, 'unavailable': False, 'prior': '-3.10'}
    mock_metric_unicode = {'label': '测试U', 'unit': '%', 'value': '−5.20', 'signed': True, 'unavailable': False, 'prior': '−3.10'}
    mock_metric_pos = {'label': '测试P', 'unit': '%', 'value': '5.20', 'signed': True, 'unavailable': False, 'prior': '3.10'}
    assert str(CARD.value(mock_metric_ascii)) == '-5.20<small>%</small>'
    assert str(CARD.value(mock_metric_unicode)) == '−5.20<small>%</small>'
    assert str(CARD.value(mock_metric_pos)) == '+5.20<small>%</small>'

