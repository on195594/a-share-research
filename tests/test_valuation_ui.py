"""Render A1 from the captured report table and computed results; no model or provider calls."""
from copy import deepcopy

from a_share_research import reading, valuation
from support.templates import ENV, render
from support.valuation import BODY, RECORD, ROWS, assumptions


def page(body, *, passed=True):
    return render('report.html', report=RECORD | {'passed': passed}, body=body,
                  rows={r['id']: r for r in body['table']}, fin=[], market=[],
                  comparison={'old': None}, retry={'label': '重试原研究问题', 'note': ''})


def calculated():
    body = deepcopy(BODY)
    body['valuation'] = assumptions()
    body['valuation_calc'] = valuation.compute(
        body['valuation'], body['table'], body['market_date'],
        consensus=valuation.consensus(ROWS, [2026, 2027, 2028], body['market_date']))
    return body


def test_scenario_report_renders_computed_rates():
    body = calculated()
    html = page(body)
    calc = body['valuation_calc']
    for name, scenario in calc['scenarios'].items():
        assert name in html and f'{scenario["annual_return"] * 100:.2f}%' in html
    assert f'{calc["implied"]["profit"]:.2f} 亿元' in html
    assert '所采集卖方预测，不作测算输入' in html


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
    from support.market_replay import CALENDAR
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
    comparison.update(status='ok', coverage={'trading_days': 2, 'covered_days': 2, 'truncated_days': []})
    assert '覆盖 2/2 个交易日' not in page(body)  # full coverage is the normal case and says nothing


def test_v2_shadow_and_research_stance_render_with_program_price():
    from a_share_research import research
    body = calculated()
    calc = body['valuation_calc']
    body['stance'] = {'action': '等价格', 'text': '期望回报尚未达到门槛。',
                      'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}]}
    body['stance_calc'] = research.stance_calc(body['stance'], calc, body['market_date'])
    html = page(body)
    assert '等价格' in html
    assert f'{calc["v2"]["support_below"]:.2f}' in html
    assert body['stance_calc']['valid_until'] in html
    old = calculated()
    old['valuation_calc'].pop('v2')
    assert '研究立场' not in page(old)


def test_add_trigger_is_optional_reference_and_old_wait_still_renders():
    from support.stance import ADD, EVIDENCE
    body = calculated()
    body['stance'] = ADD
    html = page(body)
    assert '加仓检查点（参考，不阻止首批）' in html
    assert '达到才加后续批次；未达到则复核是否减仓' in html
    body['stance'] = EVIDENCE
    html = page(body)
    assert '研究立场：<strong>等证据</strong>' in html
    assert '加仓检查点' not in html


def test_stance_decision_handles_zero_negative_and_missing_prices():
    # Zero, negative or non-finite values must not throw zero division or display negative hurdle
    assert reading.stance_decision(None, {}, {}) is None
    assert reading.stance_decision({}, {}, {}) is None

    # Invalid price combinations
    for close, hurdle in [(0, 10), (10, None)]:
        sd = reading.stance_decision(
            {"action": "等价格"},
            {"price": hurdle},
            {"close": close, "v2": {"support_below": hurdle}}
        )
        assert sd["has_prices"] is False
        assert sd["diff_pct"] is None

    # Valid waiting: close > hurdle
    sd_wait = reading.stance_decision(
        {"action": "等价格"},
        {"price": 27.77, "valid_until": "2027-05-04"},
        {"close": 29.25, "v2": {"support_below": 27.77, "annual_return": 0.0772, "zone": "中性区"}}
    )
    assert sd_wait["has_prices"] is True
    assert sd_wait["close"] == 29.25 and sd_wait["hurdle"] == 27.77
    assert sd_wait["diff_pct"] == 5.1
    assert sd_wait["valid_until"] == "2027-05-04"
    assert sd_wait["annual_return"] == 7.7

    # Valid batch: close <= hurdle
    sd_batch = reading.stance_decision(
        {"action": "现在分批"},
        {"price": 9.64},
        {"close": 8.83, "v2": {"support_below": 9.64, "annual_return": 0.139, "zone": "测算支持区"}}
    )
    assert sd_batch["has_prices"] is True
    assert sd_batch["close"] == 8.83 and sd_batch["hurdle"] == 9.64
    assert sd_batch["diff_pct"] == 8.4
    assert sd_batch["zone"] == "测算支持区"


def test_page_renders_without_500_on_adversarial_stance_data():
    body = calculated()
    # Injected dirty values that previously triggered 500 or display bugs
    for close, hurdle in [(0, 0), (0, -3.2), (-1, 0), (29.25, -5.0)]:
        body["stance"] = {"action": "等价格", "text": "等价格"}
        body["stance_calc"] = {"price": hurdle, "valid_until": "2027-01-01"}
        body["valuation_calc"]["close"] = close
        body["valuation_calc"]["v2"]["support_below"] = hurdle
        html = page(body)
        assert "研究立场：<strong>等价格</strong>" in html
        # Should never render price comparison snapshot when prices are zero or negative
        assert "stance-snapshot" not in html


def test_valuation_versions_and_company_stance_keep_saved_values_distinct():
    from a_share_research import research
    body = calculated()
    body['stance'] = {'action': '等价格', 'text': '期望回报尚未达到门槛。', 'cites': []}
    body['stance_calc'] = research.stance_calc(body['stance'], body['valuation_calc'], body['market_date'])
    html = page(body)
    assert '保守／中性情景边界（v1）' in html and '概率加权期望回报（v2）' in html
    assert '同为“中性区”不表示门槛价相同' in html
    card = reading.card(RECORD, body)
    full = ENV.get_template('_card.html').module.full_card(card)
    assert '报告时点研究立场：等价格' in full and '未按现价更新' in full
    assert body['market_date'] in full and f"{body['stance_calc']['price']:.2f}" in full
    failed = reading.card(RECORD | {'passed': False}, body)
    assert failed['stance'] is None
    assert '报告时点研究立场' not in ENV.get_template('_card.html').module.full_card(failed)


def test_report_market_snapshot_dedupes_close_merges_pe_bands_and_skips_empty_grid():
    from a_share_research import research
    body = calculated()
    body['summary'] = f"{RECORD['verdict']}：利润与现金流匹配。"
    body['stance'] = {'action': '等价格', 'text': '等估值回到门槛以下。', 'cites': []}
    body['stance_calc'] = research.stance_calc(body['stance'], body['valuation_calc'], body['market_date'])
    market = [r for r in body['table'] if r['id'].startswith('M.')]
    if not any(r['id'].startswith('M.pe_ttm_5y_p') for r in market):
        market += [
            {'id': f'M.pe_ttm_5y_p{p}', 'label': f'近 5 年 PE TTM {p}% 分位', 'unit': '倍',
             'values': {body['market_date']: v}}
            for p, v in ((30, '12.50'), (50, '18.20'), (70, '24.80'))
        ]
    html = render('report.html', report=RECORD | {'passed': True}, body=body,
                  rows={r['id']: r for r in body['table']}, fin=[], market=market,
                  comparison={'old': None}, retry={'label': '重试原研究问题', 'note': ''})
    assert '<dt>收盘价</dt>' not in html  # already shown in stance-snapshot
    assert '近 5 年 PE TTM 30/50/70 分位' in html and 'class="pe-bands"' in html
    assert f'>{RECORD["verdict"]}：' not in html and '利润与现金流匹配。' in html

    # When only M.close is present and stance already shows it, no empty snapshot box renders.
    only_close = [r for r in market if r['id'] == 'M.close']
    empty_html = render('report.html', report=RECORD | {'passed': True}, body=body,
                        rows={r['id']: r for r in body['table']}, fin=[], market=only_close,
                        comparison={'old': None}, retry={'label': '重试原研究问题', 'note': ''})
    assert 'aria-label="估值快照' not in empty_html

    # Missing valid_until on 等价格 never leaves empty parentheses or dangling label.
    body['stance_calc'] = {'price': body['stance_calc']['price']}
    no_valid = page(body)
    assert '有效至' not in no_valid and '（）' not in no_valid


def test_scenario_table_compact_strips_trailing_zeros_and_negative_zero():
    body = calculated()
    body['valuation']['scenarios']['保守']['profit'] = [-0.001, 12.50, 100.00]
    body['valuation']['scenarios']['保守']['exit_pe'] = 15.0
    html = page(body)
    assert 'class="scenario-table"' in html
    assert '>-0<' not in html
    assert '>0<' in html and '>12.5<' in html and '>100<' in html and '>15<' in html
