"""Advisor polling stays within the invocation budget and reuses the server slot."""
from io import StringIO
import sys
from types import SimpleNamespace
import pytest

from support.hermes import SOURCE

sys.path.insert(0, str(SOURCE))
import ashare_schedule as schedule


def test_polls_same_request_and_delivers_only_terminal_receipt(monkeypatch):
    now = 0
    request = dict(job='advisor_scan', slot='2026-11-20')
    row = dict(run_id=7, scan_id=8, **request)
    replies = iter([row | dict(status='running'), row | dict(status='ready', text='扫描完成\n'), row | dict(status='acked')])
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
    schedule.deliver(**request, stdout=output, deadline=60)
    assert calls == [(0, 'schedule_run', request), (20, 'schedule_run', request), (20, 'schedule_ack', {'run_id': 7})]
    assert output.getvalue() == '扫描完成\n'


@pytest.mark.parametrize('deadline,called', [(0, False), (19, True)])
def test_scan_wait_ends_inside_budget_without_ack(monkeypatch, deadline, called):
    request = dict(job='advisor_scan', slot='2026-11-20')
    calls = []
    def call(name, args, *, timeout):
        calls.append(name)
        return dict(run_id=7, status='running', scan_id=8, **request)
    monkeypatch.setattr(schedule, 'time', SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(schedule, 'tool', call)
    with pytest.raises(schedule.ToolsError, match='时限已到'):
        schedule.deliver(**request, stdout=StringIO(), deadline=deadline)
    assert calls == (['schedule_run'] if called else [])


def test_failed_monitor_keeps_scan_without_ack(ashare_cli):
    partial = '【研究扫描补回】部分披露\n【持仓核对】请核对当前持仓\n'
    args = dict(job='advisor_scan', slot='2026-11-20')
    ashare_cli.reply('schedule_run', dict(run_id=7, status='failed', text=partial, error='监控采集失败',
                                         error_code='monitor_error', **args))
    stdout = StringIO()
    schedule.deliver(**args, stdout=stdout)
    assert stdout.getvalue() == partial + '【定时任务失败】monitor_error：监控采集失败\n'
    assert ashare_cli.calls == [('schedule_run', args)]
