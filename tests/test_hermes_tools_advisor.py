"""Scheduled-client transport and retry tests; research prompts use a captured real card."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import fcntl
import json
from zoneinfo import ZoneInfo

import pytest

from a_share_research.policy.v0 import monitor_research
from a_share_research import disclosure_checks
from test_ops_hermes import ROOT, load
from test_scans import conn, disclosure_replay  # noqa: F401  (fixtures)


@pytest.fixture
def advisor(tmp_path, monkeypatch, ashare_cli):
    module = load('a-share-advisor')
    monkeypatch.setattr(module, 'OPPORTUNITY_STATE', tmp_path / 'opportunities.json')
    monkeypatch.setattr(module, 'SCAN_STATE', tmp_path / 'scan.json')
    return module


@pytest.fixture
def prompts():
    card = json.loads((ROOT / 'tests/policy_v0/fixtures/monitor/research-card-stance.json').read_text())['card']
    _, prompts, _ = monitor_research.summarize({'status': 'ok', 'cards': [card], 'missing': []}, [], date(2026, 11, 4))
    return [p for p in prompts if p['kind'] == 'research_opportunity']


def monitor_receipt(prompts, **changes):
    return dict(mode='service_monitor_capture', request_id='$request_id', activation_id='$expected_activation_id',
                capture_context={'capture_finished_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()},
                result={'review_prompts': prompts}) | changes


def test_real_prompt_dedup_migrates_legacy_keys_and_allows_lapsed_return(advisor, ashare_cli, prompts):
    assert len(prompts) == 1
    advisor.OPPORTUNITY_STATE.write_text(json.dumps([advisor.opportunity_key(prompts[0])]))
    ashare_cli.reply('policy_current', dict(installed=True, activation={'id': 7}))
    ashare_cli.reply('policy_monitor', monitor_receipt(prompts))
    assert advisor.opportunities() == '' and advisor.read_opportunity_state()['pending'] is None
    ashare_cli.reply('policy_monitor', monitor_receipt([]))
    assert advisor.opportunities() == ''
    ashare_cli.reply('policy_monitor', monitor_receipt(prompts))
    assert '【建仓机会】' in advisor.opportunities()
    assert advisor.opportunities() == ''
    requests = [args['request_id'] for name, args in ashare_cli.calls if name == 'policy_monitor']
    assert len(set(requests)) == 4


@pytest.mark.parametrize('code', [21, 22])
def test_original_monitor_request_recovered_without_new_policy_lookup(advisor, ashare_cli, prompts, code):
    ashare_cli.reply('policy_current', dict(installed=True, activation={'id': 7}))
    ashare_cli.reply('policy_monitor', code=code)
    assert '检查未完成' in advisor.opportunities()
    pending = advisor.read_opportunity_state()['pending']
    ashare_cli.reply('policy_monitor', monitor_receipt(prompts))
    assert '【建仓机会】' in advisor.opportunities()
    assert ashare_cli.calls == [('policy_current', {}), ('policy_monitor', pending), ('policy_monitor', pending)]


@pytest.mark.parametrize('mutation', [dict(mode='supplied_input_replay'), dict(request_id='another'),
                                      dict(activation_id=8), dict(result={})])
def test_incompatible_receipt_preserves_keys_and_pending(advisor, ashare_cli, prompts, mutation):
    advisor.OPPORTUNITY_STATE.write_text('["previous-key"]')
    ashare_cli.reply('policy_current', dict(installed=True, activation={'id': 7}))
    ashare_cli.reply('policy_monitor', monitor_receipt(prompts, **mutation))
    assert '检查未完成' in advisor.opportunities()
    assert advisor.read_opportunity_state()['seen'] == ['previous-key']
    assert advisor.read_opportunity_state()['pending'] is not None


def test_recovered_old_receipt_is_not_presented_as_today(advisor, ashare_cli, prompts):
    advisor.OPPORTUNITY_STATE.write_text('["previous-key"]')
    ashare_cli.reply('policy_current', dict(installed=True, activation={'id': 7}))
    old = datetime.now(ZoneInfo('Asia/Shanghai')) - timedelta(days=1)
    ashare_cli.reply('policy_monitor', monitor_receipt(prompts, capture_context={'capture_finished_at': old.isoformat()}))
    assert '未作为今日机会推送' in advisor.opportunities()
    assert advisor.read_opportunity_state() == dict(schema_version=1, seen=['previous-key'], pending=None)


def test_concurrent_and_corrupt_state_do_not_execute_tools(advisor, ashare_cli):
    with advisor.OPPORTUNITY_STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert '已有检查' in advisor.opportunities()
    advisor.OPPORTUNITY_STATE.write_text('{')
    assert '检查未完成' in advisor.opportunities()
    assert advisor.OPPORTUNITY_STATE.read_text() == '{' and ashare_cli.calls == []


@pytest.mark.parametrize(('reason', 'label'), [
    ('checkpoint_deviated', '披露偏离预测区间'), ('add_trigger_missed', '加仓条件未达到'),
])
@pytest.mark.parametrize('held', [True, False])
def test_disclosure_notice_shows_detail_and_review_action(advisor, reason, label, held):
    capture = json.loads((ROOT / 'tests/data/disclosure-checks.json').read_text())
    observation, = disclosure_checks.normalize_forecast(capture['tushare']['forecast']['603960.SH'], '603960')
    bounds = observation['values']['T.net_profit']
    # Rendering-only envelope around the captured disclosure, not a company judgment.
    detail = (f"归母净利润 {observation['period']}：披露区间 {bounds['low']}–{bounds['high']} 亿元；"
              f"业绩预告 {observation['announced']}")
    item = dict(code='603960', reason=reason, detail=detail, kind='disclosure_check')
    text = advisor.notice_text(item, {'603960'} if held else set())
    assert f'【{label}】' in text
    assert f'发生了什么：{detail}' in text
    if reason == 'add_trigger_missed' and held:
        assert '对你：你仍持仓：加仓条件未达到，复核是否减仓。' in text
    if not held:
        assert '当前未持仓：无需处理持仓' in text
        assert '复核是否减仓' not in text
    assert '%' not in text and '卖出比例' not in text


def test_opportunity_rebind_only_after_certain_policy_change(advisor, ashare_cli, prompts):
    original = dict(request_id='advisor-before-release', expected_activation_id=3)
    advisor.OPPORTUNITY_STATE.write_text(json.dumps(dict(schema_version=1, seen=['old-key'], pending=original,
        binding=advisor.client_identity()[0])))
    ashare_cli.reply('policy_current', dict(installed=True, activation={'id': 4}))
    ashare_cli.reply('policy_monitor', code=20)
    assert '政策异常' in advisor.opportunities()
    saved = advisor.read_opportunity_state()
    replacement = saved['pending']
    assert saved['seen'] == ['old-key'] and replacement['request_id'] != original['request_id']
    assert replacement['expected_activation_id'] == 4
    assert ashare_cli.calls == [('policy_monitor', original), ('policy_current', {}), ('policy_monitor', replacement)]
    ashare_cli.reply('policy_monitor', monitor_receipt(prompts))
    assert '【建仓机会】' in advisor.opportunities()
    assert ashare_cli.calls[-1] == ('policy_monitor', replacement)
    assert advisor.read_opportunity_state()['pending'] is None


@pytest.mark.parametrize('account', [None, {'codes': [], 'source': {}}, {'codes': [], 'source': {'mode': 'ledger'}}])
def test_holdings_read_only_at_delivery_and_opportunity_failure_keeps_scan(advisor, ashare_cli, account):
    from test_advisor_resume import RECEIPT, response

    original = dict(request_id='opportunity-pending', expected_activation_id=7)
    advisor.OPPORTUNITY_STATE.write_text(json.dumps(dict(schema_version=1, seen=['keep-seen'], pending=original,
        binding=advisor.client_identity()[0])))
    ashare_cli.reply('research_scan_request', RECEIPT)
    ashare_cli.reply('research_scan_read', response(notify=[dict(
        code='600519', reason='pending_review', path='/stock/600519')]))
    ashare_cli.reply('account_current', account, code=21 if account is None else 0)
    ashare_cli.reply('policy_monitor', code=21)
    output = advisor.scan()
    assert [name for name, _ in ashare_cli.calls] == [
        'research_scan_request', 'research_scan_read', 'account_current', 'policy_monitor']
    assert 'https://stock.test/stock/600519' in output and '检查未完成' in output and '发布暂停' in output
    assert '订阅' not in output
    if account and account['source'].get('mode') == 'ledger':
        assert '当前未持仓' in output
    else:
        assert '持仓状态未知' in output and '当前未持仓' not in output
    assert advisor.read_scan_state()['pending'] is None
    state = advisor.read_opportunity_state()
    assert state['pending'] == original and state['seen'] == ['keep-seen']


def test_scan_and_weekly_show_check_failures_gaps_and_open_events(advisor, conn, disclosure_replay):  # noqa: F811
    from a_share_research import db, scans, service
    from test_scans import AT, CapturedSources, run_scan
    code, _, source = disclosure_replay

    class Failure(CapturedSources):
        def valuation(self, requested):
            return super().valuation('603606') | {'ts_code': requested}  # Identity variant, as in test_scans.

        def disclosures(self, code, periods):
            raise RuntimeError('披露取数暂不可用')
    first = run_scan(conn, Failure(source.items))
    run_id, = first['summary']['queued_runs']
    conn.execute("UPDATE scans SET started_at='2026-09-20T18:00:00+08:00' WHERE id=?", (first['id'],))  # Outage variant.
    db.update_run(conn, run_id, status='failed', error='变体：研究失败', finished_at='2026-10-30T19:00:00+08:00')
    late = run_scan(conn, Failure(source.items))
    text = advisor.render_scan(late, set())
    assert '披露核对未完成' in text and f'{code} disclosure_checks：披露取数暂不可用' in text
    assert f'{code} 2026-09-20 至 2026-09-30' in text and '不能视为连续覆盖' in text
    weekly = advisor.render_weekly(json.loads(json.dumps(scans.weekly(conn, at=AT), default=str)))
    assert f'最后成功扫描：{late["started_at"][:16]}' in weekly
    assert '未覆盖区间' in weekly and f'其中披露核对失败 2 次（{code}）' in weekly
    assert '已纳入研究不等于风险排除' in weekly
    assert f'研究失败，可重试；重试任务 #{run_id}' in weekly
    service.retry_run(conn, run_id)
    assert '研究进行中' in advisor.render_weekly(json.loads(json.dumps(scans.weekly(conn, at=AT), default=str)))
    # Explicit association-missing variant: a projection failure has no report ID.
    unlinked = deepcopy(late)
    for failure in unlinked['summary']['check_failures']:
        failure['report_id'] = None
    assert '报告 #pending' in advisor.render_scan(unlinked, set())
    unlinked_week = scans.weekly(conn, at=AT)
    for failure in unlinked_week['failures']:
        if failure.get('stage') == 'disclosure_checks':
            failure['report_id'] = None
    assert '报告 #pending' in advisor.render_weekly(unlinked_week)


def test_weekly_includes_opportunity_cost_and_survives_tool_failure(advisor, conn, ashare_cli):  # noqa: F811
    from a_share_research import scans
    from test_scans import AT

    cards_payload = json.loads((ROOT / 'tests/policy_v0/fixtures/monitor/research-cards.json').read_text())['response']
    for item in cards_payload['cards']:
        if value := item['current']['valuation']:
            value['neutral_return_without_dividends'] = value['annual_returns']['中性']
    _, prompts, _ = monitor_research.summarize(
        {'status': 'ok', 'cards': cards_payload['cards'], 'missing': []}, ['600900'], date(2026, 11, 2))
    opportunity_prompts = [p for p in prompts if p['kind'] == 'opportunity_cost']
    assert len(opportunity_prompts) == 1

    weekly_payload = json.loads(json.dumps(scans.weekly(conn, at=AT), default=str))
    ashare_cli.reply('research_scan_weekly', weekly_payload)
    ashare_cli.reply('policy_current', dict(installed=True, activation={'id': 7}))
    ashare_cli.reply('policy_monitor', monitor_receipt(prompts))
    output = advisor.weekly()
    assert '换仓机会成本复核 1 项：' in output
    assert opportunity_prompts[0]['detail'] in output
    assert '测算透支区' not in output.split('换仓机会成本复核')[1]
    assert not advisor.OPPORTUNITY_STATE.exists()
    weekly_state = advisor.OPPORTUNITY_STATE.with_name(advisor.WEEKLY_OPPORTUNITY_STATE.name)
    assert json.loads(weekly_state.read_text())['pending'] is None

    assert ashare_cli.calls[0] == ('research_scan_weekly', {'compact': False})
    ashare_cli.reply('policy_monitor', code=21)
    degraded = advisor.weekly()
    assert '【研究周报】' in degraded and '【换仓复核】检查未完成' in degraded and '发布暂停' in degraded
    assert json.loads(weekly_state.read_text())['pending'] is not None


@pytest.mark.wide_scan_scope
def test_weekly_counts_scan_only_companies_without_listing_them(advisor, conn):  # noqa: F811
    from a_share_research import scans
    scans.replace_subscriptions(conn, ['603606'], catalog={'603606': {}})
    week = json.loads(json.dumps(scans.weekly(conn, at='2026-10-30T18:00:00+08:00'), default=str))
    text = advisor.render_weekly(week)
    # Captured reports give 600519 and 000651 cards: scanned, counted, not listed card by card.
    assert '披露扫描另覆盖 2 家' in text and '(600519)' not in text and '(603606)' in text
    assert '披露扫描另覆盖' not in advisor.render_weekly({k: v for k, v in week.items() if k != 'scan_scope'})
