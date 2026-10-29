"""Historical peer reading on captured report 8; absence/metadata mutations are boundaries,
not new financial observations. No data providers or model calls.
"""
import copy
import json

import pytest

from a_share_research import reading
from test_continuation import BODY, render
from test_feedback_retry import web, browser_request


def captured():
    return copy.deepcopy(BODY[8])


def row(body, ref):
    return next(r for r in body['table'] if r['id'] == ref)


def panel(body):
    return render('_peers.html', body=body, peer_snapshots=reading.peer_snapshots(body),
                  financial_period=lambda value: value)


def test_captured_pair_preserves_values_negative_cash_and_exact_saved_ratios():
    body = captured()
    before = json.dumps(body, ensure_ascii=False)
    peer, = reading.peer_snapshots(body)
    assert (peer['subject_code'], peer['code'], peer['period']) == ('600519', '000858', '2026-06-30')
    assert [[c['value'] for c in r['cells']] for r in peer['rows']] == [
        ['445.17', '87.53'], ['706.91', '-21.54'], ['1.59', '-0.25']]
    assert [c['ref'] for c in peer['rows'][-1]['cells']] == ['C.1', 'C.3']
    assert json.dumps(body, ensure_ascii=False) == before
    html = panel(body)
    assert '供应商快照' in html and '程序计算' in html and '未建立逐值公告映射' in html
    assert '营业总收入' not in html and '营业收入' not in html
    assert '2026-10-23T15:15:49+08:00' in html and '2026-08-29' not in html
    assert '分母为归母净利润，范围不完全对应' in html


def test_no_peer_or_old_empty_report_does_not_add_an_empty_shell():
    for body in (BODY[37], {}, {'table': None}):
        assert reading.peer_snapshots(body) == []
        assert panel(body).strip() == ''


def test_same_period_alignment_never_pairs_each_company_latest_value():
    body = captured()
    for r in body['table']:
        if r['id'].startswith('P000858.T.'):
            r['values'] = {k: v for k, v in r['values'].items() if k == '2025-12-31'}
    peer, = reading.peer_snapshots(body)
    assert peer['period'] == '2025-12-31'
    assert peer['rows'][0]['cells'][0]['value'] == '823.20'
    assert peer['rows'][0]['cells'][1]['value'] == '89.54'
    assert peer['ratio_missing'] and len(peer['rows']) == 2
    for key in ('T.net_profit', 'T.op_cash'):
        row(body, key)['values'] = {'2026-06-30': row(body, key)['values']['2026-06-30']}
    peer, = reading.peer_snapshots(body)
    assert not peer['rows'] and '共同累计期间' in peer['reason']


def test_missing_current_value_is_not_zero_another_period_or_a_new_calculation():
    body = captured()
    row(body, 'P000858.T.op_cash')['values'].pop('2026-06-30')
    peer, = reading.peer_snapshots(body)
    assert peer['period'] == '2026-06-30'
    assert peer['rows'][1]['cells'][1]['value'] is None
    assert peer['rows'][2]['cells'][1]['value'] is None
    assert '—' in panel(body)


@pytest.mark.parametrize('change', ['unit', 'definition', 'source', 'company', 'duplicate', 'nonfinite', 'null-values'])
def test_uncertain_definition_identity_and_values_are_excluded(change):
    body = captured()
    target = row(body, 'P000858.T.net_profit')
    if change == 'unit': target['unit'] = row(body, 'C.3')['unit']
    if change == 'definition': target['label'] = row(body, 'P000858.T.revenue')['label']
    if change == 'source': target.pop('source')
    if change == 'company': target['source']['code'] = body['code']
    if change == 'duplicate': body['table'].append(copy.deepcopy(target))
    if change == 'nonfinite': target['values']['2026-06-30'] = 'NaN'
    if change == 'null-values': target['values'] = None
    peer, = reading.peer_snapshots(body)
    assert peer['rows'][0]['cells'][1]['value'] is None
    assert peer['rows'][-1]['cells'][1]['value'] is None


@pytest.mark.parametrize('location', ['row', 'source'])
@pytest.mark.parametrize('key,value', [('period_type', 'quarterly'), ('period_type', 'ttm'), ('report_type', '2')])
def test_explicit_period_type_conflicts_cannot_be_overridden_by_quarter_end(location, key, value):
    body = captured()
    target = row(body, 'P000858.T.net_profit')
    (target if location == 'row' else target['source'])[key] = value
    peer, = reading.peer_snapshots(body)
    assert peer['period'] == '2026-06-30'
    assert peer['rows'][0]['cells'][1]['value'] is None
    assert '显式期间或报表类型' in peer['rows'][0]['cells'][1]['reason']
    assert peer['rows'][-1]['cells'][1]['value'] is None


def test_known_explicit_metadata_and_missing_metadata_have_distinct_reading_notes():
    body = captured()
    target = row(body, 'P000858.T.net_profit')
    target['period_type'] = 'cumulative'
    target['source']['report_type'] = 1
    peer, = reading.peer_snapshots(body)
    subject, other = peer['rows'][0]['cells']
    assert other['value'] == '87.53' and '明确记录累计' in other['period_note']
    assert '原行未保存独立期间类型' in subject['period_note']
    assert '并非逐行重新证明' in panel(body)


def test_no_valid_pair_explains_the_gap_but_does_not_render_lone_company_values():
    body = captured()
    for key in ('P000858.T.net_profit', 'P000858.T.op_cash'):
        row(body, key).pop('source')
    peer, = reading.peer_snapshots(body)
    assert not peer['rows'] and '来源或公司标识' in peer['reason']
    assert '<table' not in panel(body) and '暂不能形成同口径对照' in panel(body)
    assert '该日期不等于期间口径已确认' in panel(body)
    assert all(value not in panel(body) for value in ('445.17', '87.53', '706.91', '-21.54'))


@pytest.mark.parametrize('change', ['period', 'ref', 'value', 'normalized', 'operation', 'duplicate', 'result'])
def test_calculation_requires_unambiguous_same_period_same_company_operands(change):
    body = captured()
    target = row(body, 'C.3')
    calculation = target['calculation']
    if change == 'period': calculation['inputs'][1]['period'] = '2025-06-30'
    if change == 'ref': calculation['inputs'][1]['ref'] = 'T.net_profit'
    if change == 'value': calculation['inputs'][1]['value'] = row(body, 'C.4')['calculation']['inputs'][1]['value']
    if change == 'normalized': calculation['normalized_inputs'] = row(body, 'C.4')['calculation']['inputs']
    if change == 'operation': calculation['operation'] = 'difference'
    if change == 'duplicate': body['table'].append(copy.deepcopy(target))
    if change == 'result': target['values'] = row(body, 'C.4')['values']
    peer, = reading.peer_snapshots(body)
    assert peer['rows'][-1]['cells'][0]['value'] == '1.59'
    assert peer['rows'][-1]['cells'][1]['value'] is None


def test_no_saved_matching_ratio_never_generates_one():
    body = captured()
    body['table'] = [r for r in body['table'] if r['id'] not in ('C.1', 'C.3')]
    peer, = reading.peer_snapshots(body)
    assert len(peer['rows']) == 2 and peer['ratio_missing']
    html = panel(body)
    assert '贵州茅台：未记录可确切映射' in html and '五粮液：未记录可确切映射' in html
    assert '-0.25' not in html


@pytest.mark.parametrize('location', ['row', 'calculation', 'operand', 'normalized'])
def test_calculation_period_metadata_cannot_contradict_its_mapped_operands(location):
    body = captured()
    target = row(body, 'C.3')
    calculation = target['calculation']
    if location == 'row': target['report_type'] = '2'
    if location == 'calculation': calculation['period_type'] = 'ttm'
    if location == 'operand': calculation['inputs'][0]['period_type'] = 'quarterly'
    if location == 'normalized':
        calculation['normalized_inputs'] = copy.deepcopy(calculation['inputs'])
        calculation['normalized_inputs'][0]['report_type'] = '2'
    peer, = reading.peer_snapshots(body)
    assert peer['rows'][-1]['cells'][1]['value'] is None


@pytest.mark.parametrize('profit,reason', [('-87.53', '为负'), ('0', '为零')])
def test_nonpositive_profit_keeps_amounts_but_hides_even_an_existing_double_negative_ratio(profit, reason):
    body = captured()
    # Sign/zero boundary variants only, not actual company observations. A matching
    # double-negative calculation would pass the old mapping-only rule.
    row(body, 'P000858.T.net_profit')['values']['2026-06-30'] = profit
    calculation = row(body, 'C.3')
    calculation['calculation']['inputs'][1]['value'] = profit
    calculation['values']['result'] = '0.25'
    peer, = reading.peer_snapshots(body)
    assert peer['rows'][0]['cells'][1]['value'] == profit
    assert peer['rows'][1]['cells'][1]['value'] == '-21.54'
    ratio = peer['rows'][-1]['cells'][1]
    assert ratio['value'] is None and reason in ratio['reason']
    assert reason in panel(body) and '>0.25<' not in panel(body)


def test_two_inapplicable_ratios_keep_each_company_reason_without_a_ratio_row():
    body = captured()
    row(body, 'T.net_profit')['values']['2026-06-30'] = '0'
    row(body, 'P000858.T.net_profit')['values']['2026-06-30'] = '-87.53'
    peer, = reading.peer_snapshots(body)
    assert len(peer['rows']) == 2 and peer['ratio_missing']
    html = panel(body)
    assert '贵州茅台：归母净利润为零，比率不适用' in html
    assert '五粮液：归母净利润为负，比率不适用' in html
    assert '未记录可确切映射' not in html


def test_restatement_reason_blocks_a_value_and_does_not_silently_select_an_older_period():
    from a_share_research.research import financial_table
    from test_financial_series import CAPTURES, frames
    body = captured()
    current = row(body, 'P000858.T.op_cash')
    protected = next(r for r in financial_table(frames(CAPTURES['000858']), {}) if r['id'] == 'T.op_cash')
    # Exercise the unavailable flag using its real captured reason, without inventing a replacement value.
    current['unavailable'] = {'2026-06-30': protected['unavailable']['2025-09-30']}
    peer, = reading.peer_snapshots(body)
    assert peer['period'] == '2026-06-30'
    assert peer['rows'][1]['cells'][1]['value'] is None
    assert '更正' in peer['rows'][1]['cells'][1]['reason']
    html = panel(body)
    assert '-21.54' not in html
    assert html.index('历史版本未经重新核对') < html.index('<table')
    assert 'href="?evidence=1#evidence"' in html


def test_text_gaps_are_not_parsed_as_structured_version_checks():
    from a_share_research.research import financial_table
    from test_financial_series import CAPTURES, frames
    body = captured()
    protected = next(r for r in financial_table(frames(CAPTURES['000858']), {}) if r['id'] == 'T.op_cash')
    body['gaps'] = list(protected['unavailable'].values())
    before = json.dumps(body, ensure_ascii=False)
    peer, = reading.peer_snapshots(body)
    assert peer['rows'][1]['cells'][1]['value'] == '-21.54'
    html = panel(body)
    assert '历史版本未经重新核对，不保证已覆盖后续更正' in html
    assert '逐期公告日与版本未在此行记录' in html
    assert 'href="?evidence=1#evidence"' in html
    assert json.dumps(body, ensure_ascii=False) == before


@pytest.mark.parametrize('reason', [None, ''])
def test_structured_unavailable_still_blocks_a_value_when_its_reason_is_missing(reason):
    body = captured()
    row(body, 'P000858.T.op_cash')['unavailable'] = {'2026-06-30': reason}
    peer, = reading.peer_snapshots(body)
    assert peer['rows'][1]['cells'][1]['value'] is None
    assert peer['rows'][-1]['cells'][1]['value'] is None
    assert '该期间已标记不可用，原因未记录' in panel(body)


def test_multiple_peer_pairs_keep_separate_common_periods_and_source_identities():
    body = captured()
    # Combine unchanged real company rows only to exercise pair layout. This is
    # not a saved peer relationship or an investment-comparability assertion.
    for original in BODY[37]['table']:
        if original['id'] in ('T.net_profit', 'T.op_cash'):
            added = copy.deepcopy(original)
            added['id'] = 'P603606.' + added['id']
            added['label'] = BODY[37]['name'] + '·' + added['label']
            added['values'] = {p: v for p, v in added['values'].items() if p == '2025-12-31'}
            body['table'].append(added)
    peers = reading.peer_snapshots(body)
    assert [(p['code'], p['period']) for p in peers] == [('000858', '2026-06-30'), ('603606', '2025-12-31')]
    html = panel(body)
    first, second = html.split('<h3>贵州茅台与东方电缆</h3>')
    assert '对照期：2026-06-30' in first and '对照期：2025-12-31' in second
    assert html.count('<table') == 2 and '各对公司可能不同期' in html
    for r in body['table']:
        if r['id'].startswith('P603606.'):
            r['source']['code'] = '000858'
    peers = reading.peer_snapshots(body)
    assert not peers[1]['rows'] and '来源或公司标识' in peers[1]['reason']


def test_missing_fetch_dates_and_untrusted_long_names_render_as_text():
    body = captured()
    for r in body['table']:
        if r['id'].startswith('P000858.T.'):
            r['source'].pop('retrieved_at')
            r['label'] = r['label'].replace('五粮液', '<script>未核验的长公司简称</script>')
    html = panel(body)
    assert '<script>' not in html and '&lt;script&gt;' in html and '抓取于 未记录' in html
    body.pop('question')
    assert 'href="#financial"' in panel(body) and 'href="#answer"' not in panel(body)


def test_report_route_is_read_only_and_failed_or_nonpeer_reports_hide_panel(web):
    request, _ = browser_request(web)
    before = web.conn.total_changes
    response = web.report_page(request, 8)
    assert response.status_code == 200 and len(response.context['peer_snapshots']) == 1
    html = response.body.decode()
    assert html.index('id="risks"') < html.index('id="peer-snapshots"')
    assert 'id="peer-snapshots"' not in web.report_page(request, 37).body.decode()
    assert web.conn.total_changes == before
    failed_body = captured() | {'facts': {}}
    web.conn.execute('UPDATE reports SET passed=0,body=? WHERE id=8', (json.dumps(failed_body),))
    web.conn.execute("UPDATE runs SET status='failed' WHERE id=14")
    assert 'id="peer-snapshots"' not in web.report_page(request, 8).body.decode()
