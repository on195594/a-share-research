"""Scheduled CLI delivery, including the real advisor supervisor and ashare subprocess boundary."""
from datetime import datetime
from io import StringIO
import subprocess
import sys
from types import SimpleNamespace

import pytest

from support.hermes import SOURCE, load

sys.path.insert(0, str(SOURCE))
import ashare_schedule as schedule


@pytest.fixture
def client(monkeypatch, tmp_path, ashare_cli):
    # All five cron entry points and the supervisor run under this isolated HOME.
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    return ashare_cli


SCRIPTS = {
    'stop_alert': ['a-share-stop-alert.py'],
    'daily_brief': ['a-share-daily-brief.py'],
    'benchmark': ['a-share-benchmark.py'],
    'advisor_scan': ['a-share-advisor.py', 'scan'],
    'advisor_weekly': ['a-share-advisor.py', 'weekly'],
}


def invoke(job):
    script, *args = SCRIPTS[job]
    return subprocess.run([sys.executable, str(SOURCE / script), *args], text=True, capture_output=True,
                          timeout=10)


def reply(client, job, status, *, text='', result=None, **fields):
    client.reply('schedule_run', dict(job=job, slot='$slot', status=status, run_id=41,
                                     text=text, result=result, **fields))


@pytest.mark.parametrize('job', SCRIPTS)
@pytest.mark.parametrize('ack_fails', [False, True])
def test_entry_points_print_before_ack_even_on_failure(client, tmp_path, job, ack_fails):
    expected = '时段正文\n'
    data = {'stderr': '价格参考\n'} if job == 'benchmark' else {}
    if job == 'advisor_scan':
        data = dict(scan=dict(id=7, status='done', summary={}, notify=[]),
                    held_codes=[], holding_note='请核对账户来源', opportunities=[])
        expected = '【持仓核对】请核对账户来源\n'
    elif job == 'advisor_weekly':
        data = dict(weekly=dict(as_of='2026-11-22T18:07:00+08:00', status_distribution={}, zone_distribution={},
                                subscriptions=[], events=[], backlog=[], failures=[], review_failures=[],
                                checkpoint_count=0, forecast_count=0), opportunity_costs=[])
        expected = load('a-share-advisor').render_weekly(data['weekly']) + '\n'
    reply(client, job, 'ready', text=expected if not job.startswith('advisor') else '', result=data)
    client.reply('schedule_ack', {'run_id': '$run_id', 'status': 'acked'}, code=22 if ack_fails else 0,
                 stderr='private diagnostic')
    run = invoke(job)
    assert run.stdout == expected
    assert run.returncode == int(ack_fails), run.stderr
    assert ('送达确认失败' in run.stderr) is ack_fails and 'private' not in run.stderr
    assert ('价格参考' in run.stderr) == (job == 'benchmark')
    (tool, args), ack = client.calls
    assert tool == 'schedule_run' and args['job'] == job and set(args) == {'job', 'slot'}
    assert ack == ('schedule_ack', {'run_id': 41})
    assert [p.suffix for p in (tmp_path/'home').rglob('*') if p.is_file()] == ['.lock']


@pytest.mark.parametrize('job,slot', [
    ('stop_alert', '2027-01-01T00:00'), ('daily_brief', '2027-01-01'), ('advisor_scan', '2027-01-01'),
    ('advisor_weekly', '2026-W53'), ('benchmark', '2027-01'),
])
def test_slot_uses_shanghai_and_iso_week(monkeypatch, job, slot):
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return cls.fromisoformat('2026-12-31T16:04:59+00:00').astimezone(tz)
    monkeypatch.setattr(schedule, 'datetime', Clock)
    assert schedule.current_slot(job) == slot


@pytest.mark.parametrize('status,output,code', [('acked', '', 0), ('expired', '时段已过期\n', 0), ('failed', '', 1)])
def test_finished_receipts_only_ready_can_ack(client, status, output, code):
    reply(client, 'daily_brief', status, text='时段已过期\n', error_code='policy', error='采集失败')
    run = invoke('daily_brief')
    assert (run.returncode, run.stdout) == (code, output)
    assert ('policy：采集失败' in run.stderr) == (status == 'failed')
    assert len(client.calls) == 1


def test_reject_wrong_slot_and_wrong_ack(client):
    client.reply('schedule_run', dict(status='ready', job='stop_alert', slot='2026-01', text='wrong', run_id=41))
    run = invoke('benchmark')
    assert run.returncode == 1 and not run.stdout and '时段不符' in run.stderr and len(client.calls) == 1
    reply(client, 'benchmark', 'ready', text='正确正文')
    client.reply('schedule_ack', dict(run_id=42, status='acked'))
    run = invoke('benchmark')
    assert run.returncode == 1 and run.stdout == '正确正文' and '确认失败' in run.stderr


def test_flush_completes_before_ack(monkeypatch):
    events = []
    class Output(StringIO):
        def flush(self):
            events.append(('flush', self.getvalue()))
    def call(name, args):
        events.append((name, args))
        return dict(job='daily_brief', slot='2026-11-20', run_id=41,
                    status='ready' if name == 'schedule_run' else 'acked', text='正文')
    monkeypatch.setattr(schedule, 'tool', call)
    schedule.deliver('daily_brief', '2026-11-20', stdout=Output())
    assert events == [('schedule_run', dict(job='daily_brief', slot='2026-11-20')),
                      ('flush', '正文'), ('schedule_ack', {'run_id': 41})]


@pytest.mark.parametrize('recover', [False, True])
def test_exit_21_retries_same_slot_every_twenty_seconds_for_at_most_240(client, tmp_path, monkeypatch, capsys, recover):
    job = load('a-share-stop-alert')
    monkeypatch.setattr(sys, 'argv', ['a-share-stop-alert.py'])
    now = 0
    waits = []
    def sleep(seconds):
        nonlocal now
        now += seconds
        waits.append(seconds)
        if now >= 260:
            pytest.fail('retry exceeded 240 seconds')
        if recover and now == 240:
            reply(client, 'stop_alert', 'ready', text='恢复\n')
    monkeypatch.setattr(schedule, 'time', SimpleNamespace(sleep=sleep, monotonic=lambda: now))
    client.reply('schedule_run', code=21)
    client.reply('schedule_ack', dict(run_id=41, status='acked'))
    status = job.main()
    out = capsys.readouterr()
    assert waits == [20] * 12
    assert len([1 for name, _ in client.calls if name == 'schedule_run']) == 13
    assert len({args['slot'] for name, args in client.calls if name == 'schedule_run'}) == 1
    assert status == int(not recover) and out.out == ('恢复\n' if recover else '')
    assert ('重试时限' in out.err) != recover
    assert [p.suffix for p in (tmp_path/'home').rglob('*') if p.is_file()] == ['.lock']


@pytest.mark.parametrize('job', SCRIPTS)
def test_held_task_lock_is_silent(client, job):
    with schedule.task_lock(schedule.lock_path(job)):
        run = invoke(job)
    assert (run.returncode, run.stdout, run.stderr, client.calls) == (0, '', '', [])


def test_run_and_ack_share_the_release_retry_window(monkeypatch):
    now = 0
    attempts = []
    def sleep(seconds):
        nonlocal now
        now += seconds
        assert now <= 240
    def call(name, args):
        attempts.append((now, name))
        if name == 'schedule_run' and now == 220:
            return dict(run_id=41, job='daily_brief', slot='2026-11-20', status='ready', text='日报')
        raise schedule.ToolsError('发布暂停', code='service_unavailable')
    monkeypatch.setattr(schedule, 'tool', call)
    monkeypatch.setattr(schedule, 'time', SimpleNamespace(sleep=sleep, monotonic=lambda: now))
    stream = StringIO()
    with pytest.raises(schedule.ToolsError, match='送达确认失败'):
        schedule.deliver('daily_brief', '2026-11-20', stdout=stream)
    assert attempts[-2:] == [(220, 'schedule_ack'), (240, 'schedule_ack')]
    assert stream.getvalue() == '日报'
