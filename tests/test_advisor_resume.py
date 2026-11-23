"""Advisor polling stays within the invocation budget and reuses the server slot."""
from io import StringIO
from types import SimpleNamespace
import pytest

from support.hermes import load


def test_polls_same_request_and_renders_only_terminal_receipt(monkeypatch):
    advisor = load('a-share-advisor')
    import ashare_schedule as schedule
    now = 0
    request = dict(job='advisor_scan', slot='2026-11-20')
    row = dict(run_id=7, scan_id=8, **request)
    replies = iter([row | dict(status='running'), row | dict(status='ready', result={}), row | dict(status='acked')])
    calls = []
    def call(name, args, *, timeout):
        calls.append((now, name, args))
        return next(replies)
    def sleep(seconds):
        nonlocal now
        now += seconds
    monkeypatch.setattr(schedule, 'time', SimpleNamespace(sleep=sleep, monotonic=lambda: now))
    monkeypatch.setattr(schedule, 'tool', call)
    output = StringIO()
    advisor.deliver(**request, stdout=output, deadline=60, render=lambda result: '扫描完成\n')
    assert calls == [(0, 'schedule_run', request), (20, 'schedule_run', request), (20, 'schedule_ack', {'run_id': 7})]
    assert output.getvalue() == '扫描完成\n'


@pytest.mark.parametrize('deadline,called', [(0, False), (19, True)])
def test_scan_wait_ends_inside_budget_without_ack(monkeypatch, deadline, called):
    advisor = load('a-share-advisor')
    import ashare_schedule as schedule
    request = dict(job='advisor_scan', slot='2026-11-20')
    calls = []
    def call(name, args, *, timeout):
        calls.append(name)
        return dict(run_id=7, status='running', scan_id=8, **request)
    monkeypatch.setattr(schedule, 'time', SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(schedule, 'tool', call)
    with pytest.raises(advisor.ToolsError, match='时限已到'):
        advisor.deliver(**request, stdout=StringIO(), deadline=deadline, render=advisor.scan_text)
    assert calls == (['schedule_run'] if called else [])


def test_failed_monitor_keeps_scan_without_ack(monkeypatch, ashare_cli):
    advisor = load('a-share-advisor')
    result = dict(scan=dict(status='done', requested_at='2020-03-01T09:35:01+08:00', summary={}, notify=[dict(code='600519', reason='pending_review',
                                                                   path='/stock/600519')]),
                  held_codes=None, holding_note='请核对当前持仓', opportunities=[])
    args = dict(job='advisor_scan', slot='2026-11-20')
    ashare_cli.reply('schedule_run', dict(run_id=7, status='failed', result=result, error='监控采集失败',
                                         error_code='monitor_error', **args))
    stdout = StringIO()
    with pytest.raises(advisor.ToolsError, match='monitor_error：监控采集失败'):
        advisor.deliver(**args, stdout=stdout, render=advisor.scan_text)
    text = stdout.getvalue()
    assert '【研究扫描补回】原扫描时间 2020-03-01T09:35:01+08:00' in text
    assert '持仓状态未知' in text and '【持仓核对】' in text and 'https://stock.test/stock/600519' in text
    assert ashare_cli.calls == [('schedule_run', args)]
