"""Render A1 from the captured report table and computed results; no model or provider calls."""
from copy import deepcopy

from a_share_research import valuation
from support.templates import render
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
