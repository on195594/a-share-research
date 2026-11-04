"""Render A1 from the captured report table and computed results; no model or provider calls."""
from copy import deepcopy

from a_share_research import reading, valuation
from support.templates import ENV, render
from support.valuation import BODY, RECORD, ROWS, assumptions


def page(body, *, passed=True):
    return render('report.html', report=RECORD | {'passed': passed}, body=body,
                  rows={r['id']: r for r in body['table']}, fin=[], market=[],
                  comparison={'old': None})


def calculated():
    body = deepcopy(BODY)
    body['valuation'] = assumptions()
    body['valuation_calc'] = valuation.compute(
        body['valuation'], body['table'], body['market_date'],
        consensus=valuation.consensus(ROWS, [2026, 2027, 2028], body['market_date']))
    return body


def test_scenario_report_renders_computed_rates_and_accessible_comparisons():
    body = calculated()
    html = page(body)
    calc = body['valuation_calc']
    assert 'href="#valuation"' in html and 'id="valuation" tabindex="-1"' in html
    for name, entry in calc['scenarios'].items():
        assert f'<th scope="row">{name}</th>' in html
        assert f'{entry["annual_return"] * 100:.2f}%' in html
    for field in ['support_below', 'stretched_above']:
        assert f'{calc["zones"][field]:.2f}' in html
    assert f'{calc["implied"]["profit"]:.2f} 亿元' in html
    assert '敏感性矩阵' in html and '价格桥' in html
    assert '所采集卖方预测，不作测算输入' in html
    for stats in calc['consensus']['years'].values():
        assert f'{stats["median"]:.2f}' in html
    assert 'aria-label="利润与退出市盈率的年化回报敏感性矩阵"' in html
    assert '终点年分红在退出后' in html


def test_old_and_unreviewed_reports_do_not_show_scenario_results():
    assert 'id="valuation"' not in page(BODY)
    body = calculated()
    body['facts'] = {}
    html = page(body, passed=False)
    assert 'id="valuation"' not in html and 'href="#valuation"' not in html


def test_unpriced_and_calculation_gap_keep_reason_and_escape_external_text():
    body = deepcopy(BODY)
    reason = deepcopy(body['risks'][0])
    body['valuation'] = {'not_priced': reason, 'next_evidence': body['gaps'][0]}
    body['valuation_calc'] = valuation.compute(body['valuation'], body['table'], body['market_date'])
    html = page(body)
    assert reason['text'] in html and body['valuation']['next_evidence'] in html
    assert '本次不足以定价' in html and '三种情景' not in html
    body['valuation'] = assumptions()
    # Transport/error state mutation, not an invented financial observation.
    body['valuation_calc'] = {'rule': 'valuation-v1', 'error': '<script>missing M.total_mv</script>'}
    html = page(body)
    assert '&lt;script&gt;missing M.total_mv&lt;/script&gt;' in html
    assert '本次未生成参考区间' in html and '三种情景' not in html


def test_partial_coverage_keeps_statistics_and_appears_on_cards():
    body = calculated()
    from test_advisor_sources import CALENDAR
    days = [d for d in CALENDAR if '20260728' <= d <= '20261026']
    covered = sorted({r['report_date'].replace('-', '') for r in ROWS} & set(days))
    coverage = {'trading_days': len(days), 'covered_days': len(covered), 'truncated_days': covered[:1]}
    # Coverage is derived from captured dates; the truncation flag is an explicit boundary mutation.
    comparison = valuation.consensus(ROWS, [2026, 2027, 2028], body['market_date'], coverage=coverage)
    body['valuation_calc'] = valuation.compute(body['valuation'], body['table'], body['market_date'], consensus=comparison)
    html = page(body)
    note = f'覆盖 {len(covered)}/{len(days)} 个交易日'
    assert note in html and '部分覆盖' in html and '可能截断' in html
    for stats in comparison['years'].values():
        assert f'{stats["median"]:.2f}' in html
    card = reading.card(RECORD | {'passed': True}, body)
    template = ENV.get_template('_card.html').module
    for rendered in (template.summary_card(card), template.full_card(card)):
        assert note in rendered and '部分覆盖' in rendered and '可能截断' in rendered


def test_unavailable_and_partial_without_samples_show_coverage():
    body = calculated()
    comparison = body['valuation_calc']['consensus']
    comparison.update(status='unavailable', years={}, coverage={'trading_days': 1, 'covered_days': 0, 'truncated_days': []})
    html = page(body)
    assert '覆盖 0/1 个交易日' in html and '卖方预测暂不可用' in html
    comparison.update(status='partial', coverage={'trading_days': 2, 'covered_days': 1, 'truncated_days': []})
    html = page(body)
    assert '部分覆盖' in html and '覆盖 1/2 个交易日' in html and '未采集到符合日期' in html


def test_v2_shadow_and_research_stance_render_with_program_price():
    from a_share_research import research
    body = calculated()
    calc = body['valuation_calc']
    body['stance'] = {'action': '等价格', 'text': '期望回报尚未达到门槛。',
                      'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}]}
    body['stance_calc'] = research.stance_calc(body['stance'], calc, body['market_date'])
    html = page(body)
    assert '研究立场：<strong>等价格</strong>' in html
    assert f'{calc["v2"]["support_below"]:.2f}</span> 元，有效至 {body["stance_calc"]["valid_until"]}' in html
    assert f'期望年化 <span class="num">{calc["v2"]["annual_return"] * 100:.2f}%' in html
    assert '<th scope="col" class="num">概率</th>' in html and '<td class="num">50.00%</td>' in html
    body['stance'] = {'action': '等证据', 'text': '先看现金流。', 'cites': [],
                      'trigger': {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>=', 'value': 0}}
    html = page(body)
    label = next(r['label'] for r in body['table'] if r['id'] == 'T.op_cash')
    assert f'（2026-09-30 {label} ≥ <span class="num">0</span>' in html
    # Pre-v2 reports keep the v1 page unchanged: no probability column or stance block.
    old = calculated()
    old['valuation_calc'].pop('v2')
    html = page(old)
    assert '<th scope="col" class="num">概率</th>' not in html and '研究立场' not in html
