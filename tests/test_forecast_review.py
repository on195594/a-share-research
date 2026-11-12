"""Read-only review diagnostics on captured report 103; boundary variants are tests, not performance evidence."""
import copy
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from a_share_research import validation

CAPTURE = json.loads(Path(__file__).with_name('data').joinpath('k7a-forecast-report.json').read_text())['report']


def database(path):
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE runs (id INTEGER, kind TEXT, replay_of INTEGER)')
        conn.execute('CREATE TABLE reports (id INTEGER, run_id INTEGER, code TEXT, name TEXT, created_at TEXT, body TEXT, passed INTEGER)')
        conn.execute("INSERT INTO runs VALUES (1, 'research', NULL)")
        conn.execute('INSERT INTO reports VALUES (?,1,?,?,?,?,1)',
                     (CAPTURE['id'], CAPTURE['code'], CAPTURE['body']['name'], CAPTURE['created_at'], json.dumps(CAPTURE['body'])))
    return str(path)


def frozen(path):
    return validation.frozen_forecasts(validation.load_forecasts(database(path)), date(2026, 11, 12))


def test_captured_forecasts_keep_width_units_and_pending_outcomes(tmp_path):
    path = tmp_path / 'capture.db'
    items = frozen(path)
    before = path.read_bytes()
    review = validation.forecast_review(items)
    assert path.read_bytes() == before
    assert review['summary']['scored'] == 0
    assert review['summary']['pending'] == 2
    assert [i['diagnostics']['width'] for i in review['items']] == [30, 60]
    for i in review['items']:
        d = i['diagnostics']
        assert d['unit'] == d['difference_unit'] == '亿元'
        assert d['width_to_baseline'] == pytest.approx(d['width'] / abs(d['baseline']))
        assert d['midpoint_absolute_error'] is None and d['width_to_actual'] is None
    group, = review['model_groups']
    assert group['author'] == '未记录 / 未记录 / 未记录'
    assert group['reviewer'] == group['author']
    assert (group['reports'], group['companies'], group['periods'], group['total']) == (1, 1, 1, 2)
    assert group['beat_baseline'] is None and group['compared'] == 0
    # An ended period without lookup must not be declared still pending or unavailable.
    ended = validation.frozen_forecasts(items, date(2027, 1, 1))
    assert validation.forecast_review(ended)['summary']['unchecked'] == 2


def test_model_metadata_does_not_create_new_sample_slots(tmp_path):
    items = frozen(tmp_path / 'capture.db')
    original = copy.deepcopy(items)
    # Metadata-only variants of the captured forecasts exercise changed model routes.
    roles = validation.model_roles({'judgment': {'author': {'provider': 'openai-codex', 'model': 'gpt-6.1-sol', 'reasoning': 'high'},
                                                'reviewer': {'provider': 'google', 'model': 'gemini-3.8-flash'}}})
    changed = [dict(i, report=104, created='2026-11-06T09:13:05+08:00', models=roles) for i in items]
    review = validation.forecast_review(validation.mark_first(items + changed))
    assert len(review['model_groups']) == 1  # A rerun under another model does not earn a new score.
    assert review['summary']['total'] == 2 and review['summary']['updates'] == 2
    assert len(review['items']) == 4
    assert items == original  # No diagnostics or altered score written back to frozen source records.
    # A later contract selection still precedes first-sample marking, preserving the existing contract semantics.
    selected = validation.mark_first(validation.filter_contract([dict(i, contract=1) for i in items] +
                                                                 [dict(i, contract=2) for i in changed], 2))
    group, = validation.forecast_review(selected)['model_groups']
    assert group['contract'] == 2 and group['author'].startswith('openai-codex')
    assert group['reviewer'].endswith('未记录')  # Never infer missing reasoning from current settings.


def test_width_zero_missing_and_negative_denominators_are_explicit(tmp_path):
    item = frozen(tmp_path / 'capture.db')[0]
    # Minimal boundary variants, deliberately not production forecasts or financial observations.
    missing = dict(item, written=dict(item['written'], exact_values={}, values={}))
    assert validation.forecast_diagnostics(missing)['width_to_baseline'] is None
    written = dict(item['written'], unit='%', exact_values={item['base']: '0'})
    zero = dict(item, written=written, result={'status': 'scored', 'actual': 0})
    result = validation.forecast_diagnostics(zero)
    assert result['width_to_baseline'] is None and result['width_to_actual'] is None
    assert result['midpoint_absolute_error'] == 230
    negative = dict(zero, written=dict(written, exact_values={item['base']: '-100'}),
                    forecast=dict(item['forecast'], low=-110, high=-90), result={'status': 'scored', 'actual': -100})
    d = validation.forecast_diagnostics(negative)
    assert d['width_to_baseline'] == .2 and d['width_to_actual'] == .2
    assert d['midpoint_absolute_error'] == d['baseline_absolute_error'] == 0
    assert d['unit'] == '%' and d['difference_unit'] == '百分点'


def test_scored_diagnostics_use_real_disclosed_quarter_without_changing_score():
    from a_share_research import forecasts
    rows = {r['id']: r for r in json.loads(Path(__file__).with_name('data').joinpath('validation_v0.json').read_text())['forecast_table']}
    # Same retrospective deterministic test as test_validation; not prospective model success evidence.
    f = {'metric': 'T.revenue', 'period': '2026-09-30', 'low': 95, 'high': 105, 'confidence': 75}
    score = forecasts.score(f, '2026-06-30', rows['T.revenue'], rows['T.revenue'])
    original = copy.deepcopy(score)
    d = validation.forecast_diagnostics({'forecast': f, 'base': '2026-06-30', 'written': rows['T.revenue'], 'result': score})
    assert d['width'] == 10
    assert d['midpoint_absolute_error'] == pytest.approx(abs(100 - score['actual']))
    assert d['baseline_absolute_error'] == pytest.approx(abs(score['baseline'] - score['actual']))
    assert score == original


def test_html_snapshot_is_escaped_readable_and_has_no_automatic_actions(tmp_path):
    items = frozen(tmp_path / 'capture.db')
    items[0]['name'] = '<script>alert(1)</script>'  # Explicit malicious-text variant.
    review = validation.forecast_review(items)
    path = tmp_path / 'review.html'
    validation.write_forecast_html(review, str(path))
    html = path.read_text()
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'viewport' in html and 'grid-template-columns' in html
    assert '真实账户' in html and '模拟收益' in html and '未查询兑现' in html
    assert '<form' not in html and 'http-equiv="refresh"' not in html
    assert '尚无已记分结果' in html
    assert '未记录 / 未记录 / 未记录' in html
    assert '<details>' in html


def test_cli_frozen_has_no_provider_calls_and_emits_additive_json(tmp_path, monkeypatch):
    import sys
    path = database(tmp_path / 'capture.db')
    before = Path(path).read_bytes()
    def unexpected(*args, **kwargs):
        raise AssertionError('frozen review must not query financial or model providers')
    monkeypatch.setattr(validation, 'score_forecasts', unexpected)
    monkeypatch.setattr(validation.market, 'client', unexpected)
    output, page = tmp_path / 'review.json', tmp_path / 'review.html'
    monkeypatch.setattr(sys, 'argv', ['validation', '--database', path, '--forecasts', '--frozen',
                                    '--json', str(output), '--html', str(page)])
    validation.main()
    result = json.loads(output.read_text())
    assert result['summary']['scored'] == 0
    assert result['model_groups'][0]['total'] == 2
    assert result['forecasts'][0]['diagnostics']['width'] == 30
    assert Path(path).read_bytes() == before and page.exists()
    monkeypatch.setattr(sys, 'argv', ['validation', '--database', path, '--frozen'])
    with pytest.raises(SystemExit):
        validation.main()


def test_percentage_differences_render_as_points_in_html_and_cli(tmp_path, monkeypatch, caplog):
    import logging
    import sys
    item = frozen(tmp_path / 'capture.db')[0]
    # Explicit percentage-unit boundary variant; not investment performance evidence.
    item = dict(item, written=dict(item['written'], unit='%', exact_values={item['base']: '20'}),
                forecast=dict(item['forecast'], low=20, high=30),
                result={'status': 'scored', 'actual': 24, 'baseline': 20, 'hit': True,
                        'beat_baseline': True, 'base_restated': False})
    review = validation.forecast_review([item])
    page = tmp_path / 'points.html'
    validation.write_forecast_html(review, str(page))
    html = page.read_text()
    assert '区间 20–30 %' in html and '实际 24 %' in html
    assert '冻结朴素值 20.0 %' in html
    assert '绝对宽度 10.0 百分点' in html
    assert '区间中点误差 1.0 百分点；朴素误差 4.0 百分点' in html
    monkeypatch.setattr(validation, 'load_forecasts', lambda *args: [item])
    monkeypatch.setattr(validation, 'score_forecasts', lambda items: items)
    monkeypatch.setattr(sys, 'argv', ['validation', '--database', str(tmp_path / 'capture.db'), '--forecasts'])
    with caplog.at_level(logging.INFO):
        validation.main()
    assert '区间宽度 10百分点' in caplog.text
    assert '中点绝对误差 1百分点，朴素绝对误差 4.0百分点' in caplog.text


def test_width_groups_never_mix_confidence_metric_or_unit(tmp_path):
    item = frozen(tmp_path / 'capture.db')[0]
    # Explicit cohort-boundary variants, not additional observed research outcomes.
    rows = [dict(item, forecast=dict(item['forecast'], confidence=c, metric=m),
                 written=dict(item['written'], unit=u))
            for c, m, u in [(60, 'T.profit', '亿元'), (75, 'T.profit', '亿元'),
                            (90, 'T.profit', '亿元'), (75, 'T.cash', '亿元'), (75, 'T.profit', '%')]]
    rows.append(dict(rows[0], first=False))
    group, = validation.forecast_review(rows)['model_groups']
    assert 'median_width_to_baseline' not in group and 'width_compared' not in group
    assert len(group['width_groups']) == 5
    for stratum in group['width_groups']:
        assert stratum['total'] == stratum['width_compared'] == 1
        assert stratum['scored'] == stratum['compared'] == 0
        assert stratum['pending'] == 1
