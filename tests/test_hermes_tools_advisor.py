"""Service-rendered advisor notifications from captured disclosures, cards and evaluations."""
from copy import deepcopy
from datetime import date
import json

import pytest

from a_share_research.policy.v0 import monitor_research
from a_share_research import disclosure_checks
from a_share_research.tools import advisor_text
from support.hermes import ROOT


@pytest.fixture
def prompts():
    card = json.loads((ROOT / 'tests/policy_v0/fixtures/monitor/research-card-stance.json').read_text())['card']
    _, prompts, _ = monitor_research.summarize({'status': 'ok', 'cards': [card], 'missing': []}, [], date(2026, 11, 4))
    return [p for p in prompts if p['kind'] == 'research_opportunity']


def test_real_filtered_opportunity_links_and_risk_note(prompts):
    text = advisor_text.render_opportunities(prompts, 'https://stock.test')
    assert prompts[0]['detail'] in text and 'https://stock.test/stock/' + prompts[0]['code'] in text
    assert advisor_text.OPPORTUNITY_NOTE in text
    assert advisor_text.render_opportunities([]) == ''


@pytest.mark.parametrize(('reason', 'label'), [
    ('checkpoint_deviated', '披露偏离预测区间'), ('add_trigger_missed', '加仓条件未达到'),
])
@pytest.mark.parametrize('held', [True, False])
def test_disclosure_notice_shows_detail_and_review_action(reason, label, held):
    capture = json.loads((ROOT / 'tests/data/disclosure-checks.json').read_text())
    observation, = disclosure_checks.normalize_forecast(capture['tushare']['forecast']['603960.SH'], '603960')
    bounds = observation['values']['T.net_profit']
    # Rendering-only envelope around the captured disclosure, not a company judgment.
    detail = (f"归母净利润 {observation['period']}：披露区间 {bounds['low']}–{bounds['high']} 亿元；"
              f"业绩预告 {observation['announced']}")
    item = dict(code='603960', reason=reason, detail=detail, kind='disclosure_check')
    text = advisor_text.notice_text(item, {'603960'} if held else set())
    assert f'【{label}】' in text
    assert f'发生了什么：{detail}' in text
    if reason == 'add_trigger_missed' and held:
        assert '对你：你仍持仓：加仓条件未达到，复核是否减仓。' in text
    if not held:
        assert '当前未持仓：无需处理持仓' in text
        assert '复核是否减仓' not in text
    assert '%' not in text and '卖出比例' not in text


def test_scan_and_weekly_show_check_failures_gaps_and_open_events(conn, disclosure_replay):  # noqa: F811
    from support.scans import AT, CapturedSources, run_scan

    from a_share_research import db, scans, service
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
    text = advisor_text.render_scan(late, set())
    assert '披露核对未完成' in text and f'{code} disclosure_checks：披露取数暂不可用' in text
    assert f'{code} 2026-09-20 至 2026-09-30' in text and '不能视为连续覆盖' in text
    weekly = advisor_text.render_weekly(json.loads(json.dumps(scans.weekly(conn, at=AT), default=str)))
    assert f'最后成功扫描：{late["started_at"][:16]}' in weekly
    assert '未覆盖区间' in weekly and f'其中披露核对失败 2 次（{code}）' in weekly
    assert '已纳入研究不等于风险排除' in weekly
    assert f'研究失败，可重试；重试任务 #{run_id}' in weekly
    service.retry_run(conn, run_id)
    assert '研究进行中' in advisor_text.render_weekly(json.loads(json.dumps(scans.weekly(conn, at=AT), default=str)))
    # Explicit association-missing variant: a projection failure has no report ID.
    unlinked = deepcopy(late)
    for failure in unlinked['summary']['check_failures']:
        failure['report_id'] = None
    assert '报告 #pending' in advisor_text.render_scan(unlinked, set())
    unlinked_week = scans.weekly(conn, at=AT)
    for failure in unlinked_week['failures']:
        if failure.get('stage') == 'disclosure_checks':
            failure['report_id'] = None
    assert '报告 #pending' in advisor_text.render_weekly(unlinked_week)


def test_weekly_includes_opportunity_cost(conn):  # noqa: F811
    from support.scans import AT

    from a_share_research import scans

    cards_payload = json.loads((ROOT / 'tests/policy_v0/fixtures/monitor/research-cards.json').read_text())['response']
    for item in cards_payload['cards']:
        if value := item['current']['valuation']:
            value['neutral_return_without_dividends'] = value['annual_returns']['中性']
    _, prompts, _ = monitor_research.summarize(
        {'status': 'ok', 'cards': cards_payload['cards'], 'missing': []}, ['600900'], date(2026, 11, 2))
    opportunity_prompts = [p for p in prompts if p['kind'] == 'opportunity_cost']
    assert len(opportunity_prompts) == 1

    weekly_payload = json.loads(json.dumps(scans.weekly(conn, at=AT), default=str))
    output = advisor_text.render_weekly(weekly_payload, opportunity_prompts)
    assert '换仓机会成本复核 1 项：' in output
    assert opportunity_prompts[0]['detail'] in output
    assert '测算透支区' not in output.split('换仓机会成本复核')[1]


@pytest.mark.wide_scan_scope
def test_weekly_counts_scan_only_companies_without_listing_them(conn):  # noqa: F811
    from a_share_research import scans
    scans.replace_subscriptions(conn, ['603606'], catalog={'603606': {}})
    week = json.loads(json.dumps(scans.weekly(conn, at='2026-10-30T18:00:00+08:00'), default=str))
    text = advisor_text.render_weekly(week)
    # Captured reports give 600519 and 000651 cards: scanned, counted, not listed card by card.
    assert '披露扫描另覆盖 2 家' in text and '(600519)' not in text and '(603606)' in text
    assert '披露扫描另覆盖' not in advisor_text.render_weekly({k: v for k, v in week.items() if k != 'scan_scope'})


@pytest.mark.parametrize('job,slot', [('advisor_scan', '2026-11-04'), ('advisor_weekly', '2026-W45')])
def test_schedule_text_matches_captured_hermes_output(policy_setup, monkeypatch, job, slot):
    from a_share_research import db, scans
    from a_share_research.tools.registry import Context, invoke
    from support.scans import replay_disclosure

    database, conn, principals, _, _, bundle, _ = policy_setup
    stamp = '2026-11-04T16:37:00+08:00'
    monkeypatch.setattr(db, 'now', lambda: stamp)
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', 'https://stock.test/mcp')
    _, _, source = replay_disclosure(conn)
    ctx = Context(conn, database, principals['assistant'], True)
    card = json.loads((ROOT / 'tests/policy_v0/fixtures/monitor/research-card-stance.json').read_text())['card']
    bundle.update(now=stamp, research_result={'cards': [card], 'status': 'ok', 'missing': []})

    def lookup(slot):
        return invoke(ctx, 'schedule_run', {'job': job, 'slot': slot})['data']

    if job == 'advisor_scan':
        scans.execute(conn, lookup(slot)['scan_id'], source=source, at=stamp)
    row = lookup(slot)
    assert row['status'] == 'ready', row['error']
    # Golden text from the 9e77a07 Hermes renderer on these same service receipts.
    expected = (ROOT / 'tests/data' / ('scheduled-' + job.replace('_', '-') + '.txt')).read_text()
    assert row['text'] == expected
    assert conn.execute('SELECT text FROM scheduled_runs WHERE id=?', (row['run_id'],)).fetchone()[0] == expected

    # Segment 2 already has ready rows with structured results and empty text.
    conn.execute("UPDATE scheduled_runs SET text='' WHERE id=?", (row['run_id'],))
    stamp = '2026-11-05T16:37:00+08:00'
    if job == 'advisor_scan':
        expected = '【研究扫描补回】原扫描时间 2026-11-04T16:37:00+08:00；以下为历史回执，不代表今日观察。\n\n' + expected
        slot = stamp[:10]
    assert lookup(slot)['text'] == expected
    assert lookup(slot)['text'] == expected


def test_failed_scan_delivers_disclosures_before_the_catchup_error(policy_setup, monkeypatch):
    from a_share_research import db, scans
    from a_share_research.policy import account_view, registry
    from a_share_research.tools.registry import Context, invoke
    from support.scans import replay_disclosure

    database, conn, principals, *_ = policy_setup
    ctx = Context(conn, database, principals['assistant'], True)
    stamp = '2026-11-04T16:37:00+08:00'
    monkeypatch.setattr(db, 'now', lambda: stamp)
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', 'http://127.0.0.1:8621/mcp')
    _, _, source = replay_disclosure(conn)

    def lookup():
        return invoke(ctx, 'schedule_run', dict(job='advisor_scan', slot=stamp[:10]))['data']

    def unavailable(*args):
        raise registry.PolicyError('账户核对暂不可用', 503)

    scan_id = lookup()['scan_id']
    scans.execute(conn, scan_id, source=source, at=stamp)
    monkeypatch.setattr(account_view, 'current', unavailable)
    failed = lookup()
    assert failed['status'] == 'failed'
    assert '【披露偏离预测区间】' in failed['text'] and '持仓状态未知' in failed['text']
    assert '查看研究：' not in failed['text']
    assert failed['text'].endswith('【持仓核对】账户核对暂不可用\n')

    stamp = '2026-11-05T16:37:00+08:00'
    catchup = lookup()
    assert catchup['status'] == 'expired' and catchup['result']['has_body']
    assert catchup['text'] == ('【研究扫描补回】原扫描时间 2026-11-04T16:37:00+08:00；以下为历史回执，不代表今日观察。\n\n'
                               + failed['text'] + '2026-11-04 的扫描补回失败：unavailable，账户核对暂不可用\n')

    # Segment 2 saved the expiry reason alone, with the partial scan in result.
    conn.execute("UPDATE scheduled_runs SET result=json_remove(result,'$.has_body'),text=? WHERE id=?",
                 ('2026-11-04 的扫描补回失败：unavailable，账户核对暂不可用\n', catchup['run_id']))
    legacy = invoke(ctx, 'schedule_run', dict(job='advisor_scan', slot=catchup['slot']))['data']
    assert legacy == catchup
