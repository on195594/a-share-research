"""Scheduled CLI delivery, including the real advisor supervisor and ashare subprocess boundary."""
from datetime import datetime
from io import StringIO
import json
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
def test_entry_points_preserve_body_after_failed_ack(client, tmp_path, job, ack_fails):
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
    client.reply('schedule_ack', {'run_id': '$run_id', 'status': 'acked'}, code=1 if ack_fails else 0,
                 stderr='private diagnostic')
    run = invoke(job)
    assert run.returncode == 0, run.stderr
    if ack_fails:
        assert run.stdout.startswith(expected) and '送达确认失败' in run.stdout[len(expected):]
        assert len(run.stdout[len(expected):].splitlines()) == 1
    else:
        assert run.stdout == expected
    assert 'private' not in run.stdout + run.stderr
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
    assert run.returncode == 0 and run.stdout.splitlines()[0] == '正确正文' and '确认失败' in run.stdout.splitlines()[1]


def test_flush_completes_before_ack(monkeypatch):
    events = []
    class Output(StringIO):
        def flush(self):
            events.append(('flush', self.getvalue()))
    def call(name, args, *, timeout):
        events.append((name, args))
        return dict(job='daily_brief', slot='2026-11-20', run_id=41,
                    status='ready' if name == 'schedule_run' else 'acked', text='正文')
    monkeypatch.setattr(schedule, 'tool', call)
    schedule.deliver('daily_brief', '2026-11-20', stdout=Output())
    assert events == [('schedule_run', dict(job='daily_brief', slot='2026-11-20')),
                      ('flush', '正文'), ('schedule_ack', {'run_id': 41})]


@pytest.mark.parametrize('recover', [False, True])
def test_exit_21_retries_same_slot_every_twenty_seconds_for_at_most_240(client, tmp_path, monkeypatch, capsys, recover):
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return cls.fromisoformat('2026-11-23T10:30:00+08:00')
    monkeypatch.setattr(schedule, 'datetime', Clock)
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


def test_ack_retries_within_the_run_deadline(monkeypatch):
    now = 0
    attempts = []
    def sleep(seconds):
        nonlocal now
        now += seconds
        assert now <= 240
    def call(name, args, *, timeout):
        attempts.append((now, name))
        if name == 'schedule_run' and now == 220:
            return dict(run_id=41, job='daily_brief', slot='2026-11-20', status='ready', text='日报')
        raise schedule.ToolsError('发布暂停', code='service_unavailable')
    monkeypatch.setattr(schedule, 'tool', call)
    monkeypatch.setattr(schedule, 'time', SimpleNamespace(sleep=sleep, monotonic=lambda: now))
    stream = StringIO()
    with pytest.raises(schedule.ToolsError, match='送达确认失败'):
        schedule.deliver('daily_brief', '2026-11-20', stdout=stream, deadline=240)
    assert attempts[-2:] == [(220, 'schedule_run'), (220, 'schedule_ack')]
    assert stream.getvalue() == '日报'


def test_each_call_gets_its_own_retry_window(monkeypatch):
    # A pause early in a long scan poll must not leave the later ack without retries.
    now = 0
    failures = {0, 300, 600}
    def sleep(seconds):
        nonlocal now
        now += seconds
    def call(name, args, *, timeout):
        if now in failures:
            failures.discard(now)
            raise schedule.ToolsError('暂不可用', code='service_unavailable')
        if name == 'schedule_ack':
            return dict(run_id=41, status='acked')
        status = 'ready' if now >= 600 else 'running'
        return dict(run_id=41, job='advisor_scan', slot='2026-11-20', status=status, result={})
    monkeypatch.setattr(schedule, 'tool', call)
    monkeypatch.setattr(schedule, 'time', SimpleNamespace(sleep=sleep, monotonic=lambda: now))
    stream = StringIO()
    schedule.deliver('advisor_scan', '2026-11-20', stdout=stream, render=lambda result: '扫描\n', deadline=900)
    assert stream.getvalue() == '扫描\n' and not failures


def test_untransient_unknown_outcome_is_not_retried(monkeypatch):
    calls = []
    def call(name, args, *, timeout):
        calls.append(name)
        raise schedule.ToolsError('回执超限', code='outcome_unknown')
    monkeypatch.setattr(schedule, 'tool', call)
    with pytest.raises(schedule.ToolsError, match='回执超限'):
        schedule.deliver('daily_brief', '2026-11-20', stdout=StringIO())
    assert calls == ['schedule_run']


def test_backlog_scan_is_delivered_before_today(monkeypatch):
    calls = []
    rows = {'2026-11-19': 'ready', '2026-11-20': 'ready'}
    def call(name, args, *, timeout):
        calls.append((name, args))
        if name == 'schedule_ack':
            return dict(run_id=args['run_id'], status='acked')
        slot = '2026-11-19' if len(calls) == 1 else args['slot']
        return dict(run_id=int(slot[-2:]), job='advisor_scan', slot=slot, status=rows[slot], result={'day': slot})
    monkeypatch.setattr(schedule, 'tool', call)
    stream = StringIO()
    schedule.deliver('advisor_scan', '2026-11-20', stdout=stream, render=lambda result: result['day'] + '\n')
    assert stream.getvalue() == '2026-11-19\n2026-11-20\n'
    assert calls == [('schedule_run', dict(job='advisor_scan', slot='2026-11-20')), ('schedule_ack', {'run_id': 19}),
                     ('schedule_run', dict(job='advisor_scan', slot='2026-11-20')), ('schedule_ack', {'run_id': 20})]


def test_only_scans_accept_an_earlier_slot(monkeypatch):
    monkeypatch.setattr(schedule, 'tool', lambda name, args, *, timeout: dict(
        run_id=1, job='daily_brief', slot='2026-11-19', status='ready', text='旧'))
    with pytest.raises(schedule.ToolsError, match='时段不符'):
        schedule.deliver('daily_brief', '2026-11-20', stdout=StringIO())


@pytest.mark.parametrize("status", ["failed", "expired", "acked"])
def test_terminal_backlog_delivers_disclosures_and_continues_today(monkeypatch, status):
    args = dict(job="advisor_scan", slot="2026-11-20")
    rows = iter([
        dict(run_id=40, job="advisor_scan", slot="2026-11-19", status=status,
             result={"scan": "昨日披露"}, error_code="scan_error", error="昨日部分失败", text="昨日部分失败"),
        dict(run_id=41, **args, status="ready", result={"scan": "今日披露"}),
        dict(run_id=41, status="acked"),
    ])
    calls = []
    def call(name, arguments, **kwargs):
        calls.append((name, arguments))
        return next(rows)
    monkeypatch.setattr(schedule, "tool", call)
    stream = StringIO()
    schedule.deliver(**args, stdout=stream, render=lambda result: result["scan"] + "\n")
    assert calls == [("schedule_run", args), ("schedule_run", args), ("schedule_ack", {"run_id": 41})]
    assert ("昨日披露" in stream.getvalue()) == (status != "acked")
    assert ("昨日部分失败" in stream.getvalue()) == (status != "acked")
    assert stream.getvalue().endswith("今日披露\n")


@pytest.mark.parametrize("job, pause, collect, limit", [("stop_alert", 0, 20, 29), ("daily_brief", 240, 29, 270)])
def test_policy_run_and_ack_share_remaining_subprocess_budget(monkeypatch, tmp_path, capsys, job, pause, collect, limit):
    import ashare_tools
    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return cls.fromisoformat("2026-11-23T10:34:30+08:00")
    monkeypatch.setattr(schedule, "datetime", Clock)
    monkeypatch.setattr(sys, "argv", ["a-share-stop-alert.py"])
    monkeypatch.setenv(f"ASHARE_{job.upper()}_STATE", str(tmp_path / "state.json"))
    elapsed = 0
    calls = []
    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds
    monkeypatch.setattr(schedule, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep))
    def run(argv, *, timeout, **kwargs):
        nonlocal elapsed
        name, _, payload = argv[-3:]
        calls.append((name, timeout))
        if name == "schedule_run":
            args = json.loads(payload)
            if elapsed < pause:
                return subprocess.CompletedProcess(argv, 21)
            assert args["slot"] == ("2026-11-23T10:30" if job == "stop_alert" else "2026-11-23")
            elapsed += collect
            row = dict(**args, run_id=1, status="ready", text="止损\n")
        else:
            elapsed += min(39, timeout)
            if timeout < 39:
                raise subprocess.TimeoutExpired(argv, timeout)
            row = dict(run_id=1, status="acked")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "result": {"data": row}}))
    monkeypatch.setattr(ashare_tools.subprocess, "run", run)
    assert schedule.policy_main(job) == 0
    assert elapsed == limit, calls
    assert calls[-2:] == [("schedule_run", limit - pause), ("schedule_ack", limit - pause - collect)]
    assert "确认失败" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["exit22", "timeout"])
@pytest.mark.parametrize("name", ["schedule_run", "schedule_ack"])
def test_transient_cli_retries_identical_arguments(monkeypatch, ashare_cli, failure, name):
    args = dict(job="daily_brief", slot="2026-11-20")
    ready = dict(**args, run_id=41, status="ready", text="日报")
    acked = dict(run_id=41, status="acked")
    ashare_cli.reply("schedule_run", ready)
    ashare_cli.reply("schedule_ack", acked)
    ashare_cli.reply(name, code=22 if failure == "exit22" else 0, delay=5 if failure == "timeout" else 0)
    # A short per-call budget drives a real subprocess timeout; advance replies at the retry boundary.
    def recover(seconds):
        assert seconds == .5
        ashare_cli.reply(name, ready if name == "schedule_run" else acked)
    monkeypatch.setattr(schedule, "time", SimpleNamespace(monotonic=lambda: 0, sleep=recover))
    stream = StringIO()
    schedule.deliver(**args, stdout=stream, deadline=.5)
    run_call = ("schedule_run", args)
    ack_call = ("schedule_ack", {"run_id": 41})
    assert ashare_cli.calls == [run_call, run_call if name == "schedule_run" else ack_call, ack_call]
    assert stream.getvalue() == "日报"


def test_advisor_preserves_backlog_after_supervisor_deadline(monkeypatch, tmp_path, capfd):
    advisor = load("a-share-advisor")
    args = dict(job="advisor_scan", slot="2026-11-20")
    body = "完整披露" * 70 + "末条风险"
    calls = []
    def call(name, arguments, **kwargs):
        calls.append(name)
        if len(calls) == 1:
            return dict(args, slot="2026-11-19", run_id=1, status="ready", result={"text": body})
        if name == "schedule_ack":
            return dict(run_id=1, status="acked")
        schedule.time.sleep(10)
    monkeypatch.setattr(sys, "argv", ["a-share-advisor.py", "scan"])
    monkeypatch.setenv("A_SHARE_ADVISOR_STATE", str(tmp_path / "advisor"))
    monkeypatch.setattr(advisor, "RUN_SECONDS", .5)
    monkeypatch.setattr(advisor, "current_slot", lambda job: args["slot"])
    monkeypatch.setattr(advisor, "scan_text", lambda result: result["text"])
    monkeypatch.setattr(schedule, "tool", call)
    code = advisor.main()
    output = capfd.readouterr()
    assert code == 0
    report, error = output.out.splitlines()
    assert report == body and "时限" in error


def test_ack_failure_with_silent_result_triggers_hermes_alarm(client):
    reply(client, "daily_brief", "ready")
    client.reply("schedule_ack", code=1)
    run = invoke("daily_brief")
    assert run.returncode == 1 and run.stdout == "" and "确认失败" in run.stderr


@pytest.mark.parametrize("old_status", ["failed", "expired"])
@pytest.mark.parametrize("today_status", ["failed", "acked"])
def test_advisor_only_backlog_failure_notice_keeps_alarm(monkeypatch, tmp_path, capfd, old_status, today_status):
    advisor = load("a-share-advisor")
    args = dict(job="advisor_scan", slot="2026-11-20")
    rows = iter([
        dict(args, slot="2026-11-19", run_id=1, status=old_status, result={},
             error_code="scan_error", error="旧扫描失败", text="旧扫描失败\n"),
        dict(args, run_id=2, status=today_status, result={}, error_code="scan_error", error="今日采集失败"),
    ])
    monkeypatch.setattr(sys, "argv", ["a-share-advisor.py", "scan"])
    monkeypatch.setenv("A_SHARE_ADVISOR_STATE", str(tmp_path / "advisor"))
    monkeypatch.setattr(advisor, "current_slot", lambda job: args["slot"])
    monkeypatch.setattr(schedule, "tool", lambda *args, **kwargs: next(rows))
    code = advisor.main()
    output = capfd.readouterr()
    expected_error = "今日采集失败" if today_status == "failed" else "旧扫描失败"
    assert code == 1 and expected_error in output.err
