"""Hermes delivery of service-owned scheduled runs."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
import stat
import sys
import time
from zoneinfo import ZoneInfo

from ashare_tools import ToolsError, tool


def policy_state_path(job):
    """The installer checks pending records left by the previous scripts."""
    directory = 'ashare-' + job.replace('_', '-')
    return Path(os.environ.get(f'ASHARE_{job.upper()}_STATE', Path.home() / '.local/state' / directory / 'state.json'))


def advisor_directory():
    return Path(os.environ.get('A_SHARE_ADVISOR_STATE', Path.home() / '.local/state/a-share-advisor'))


def lock_path(job):
    if job.startswith('advisor_'):
        return advisor_directory() / (job.removeprefix('advisor_') + '.lock')
    return policy_state_path(job).with_suffix('.lock')


@contextmanager
def task_lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a'):
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ToolsError('任务锁无效，请核对属主与文件类型')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def current_slot(job, now=None):
    if now is None:
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if job == 'stop_alert':
        return now.replace(minute=now.minute // 5 * 5).strftime('%Y-%m-%dT%H:%M')
    if job in ('benchmark', 'monthly_submit'):
        return now.strftime('%Y-%m')
    if job == 'monthly_status':
        return now.strftime('%Y-%m-%dT%H')
    if job == 'advisor_weekly':
        return now.strftime('%G-W%V')
    return now.date().isoformat()


ADVISOR_SECONDS = 540
POLICY_SECONDS = 270
# One schedule_run call is capped at 40s; retry only when a full call still fits.
RETRY_MIN_SECONDS = 45


def failure_line(error):
    return '【定时任务失败】' + ' '.join(str(error).split())[:160] + '\n'


def _call(name, arguments, deadline):
    retry_until = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolsError('本轮执行时限已到，请按原时段查回')
        try:
            return tool(name, arguments, timeout=min(40, remaining))
        except ToolsError as exc:
            # schedule_run and schedule_ack are idempotent, so a lost reply is looked up again.
            if not exc.retryable:
                raise
            now = time.monotonic()
            if retry_until is None:
                retry_until = min(now + 240, deadline)
            if now >= retry_until:
                raise ToolsError(f'{name} 服务仍不可用，本轮重试时限已到') from None
            time.sleep(min(20, retry_until - now))


def deliver(job, slot, *, stdout, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + (ADVISOR_SECONDS if job.startswith('advisor_') else POLICY_SECONDS)
    last_char = ''

    def write(text, *, body=True):
        nonlocal last_char
        if not isinstance(text, str):
            raise ToolsError('定时运行正文无效')
        if text:
            stdout.write(text)
            stdout.flush()
            if body or last_char:
                last_char = text[-1]

    try:
        args = dict(job=job, slot=slot)
        backlog_error = None
        retried = False
        while True:
            row = _call('schedule_run', args, deadline)
            run_id = row.get('run_id')
            # The service returns an earlier scan still awaiting its result before today's slot.
            backlog = job == 'advisor_scan' and isinstance(row.get('slot'), str) and row['slot'] < slot
            if (row.get('job') != job or (row.get('slot') != slot and not backlog)
                    or type(run_id) is not int or run_id < 1):
                raise ToolsError('定时运行回执与本次时段不符')
            status = row.get('status')
            if status == 'running':
                if job != 'advisor_scan':
                    raise ToolsError('该时段正在处理，请稍后用原时段查回')
                if deadline - time.monotonic() <= 20:
                    raise ToolsError(f'本轮轮询时限已到，请查回运行 #{run_id}')
                time.sleep(20)
                continue
            if status == 'acked':
                if backlog:
                    continue
                break
            if status not in ('ready', 'expired', 'failed'):
                raise ToolsError('定时运行回执状态无效')

            if status == 'ready' or (status == 'failed' and row.get('text')):
                write(row.get('text'))
            if status == 'failed':
                error = f"{row['error_code']}：{row['error']}"
                if not backlog:
                    # A capture timeout is transient; schedule_run reruns a failed row under a new attempt.
                    if (row.get('error_code') == 'timeout' and not row.get('text') and not retried
                            and deadline - time.monotonic() >= RETRY_MIN_SECONDS):
                        retried = True
                        continue
                    raise ToolsError(error)
                backlog_error = error
                write(failure_line(error), body=False)
            elif status == 'expired':
                if backlog and row.get('error_code'):
                    backlog_error = row['text']
                write(row.get('text'), body=(row.get('result') or {}).get('has_body', False))
            else:
                if (row.get('result') or {}).get('stderr'):
                    sys.stderr.write(row['result']['stderr'])
                try:
                    ack = _call('schedule_ack', {'run_id': run_id}, deadline)
                    if type(ack.get('run_id')) is not int or ack['run_id'] != run_id or ack.get('status') != 'acked':
                        raise ToolsError('确认回执与本次运行不符')
                except ToolsError as exc:
                    raise ToolsError(f'运行 #{run_id} 送达确认失败。{exc}') from None
            if not backlog:
                break
        if backlog_error and not last_char:
            raise ToolsError(backlog_error.rstrip())
    except (ToolsError, OSError) as exc:
        if not last_char:
            raise
        if last_char != '\n':
            stdout.write('\n')
        error = exc if isinstance(exc, ToolsError) else '定时任务的输出流不可用，请核对执行记录。'
        stdout.write(failure_line(error))


def policy_main(job):
    if len(sys.argv) != 1:
        sys.stderr.write(f'usage: {Path(sys.argv[0]).name}\n')
        return 2
    return scheduled_main(job)


def scheduled_main(job):
    try:
        with task_lock(lock_path(job)) as acquired:
            if acquired:
                deadline = None
                now = datetime.now(ZoneInfo('Asia/Shanghai'))
                slot = current_slot(job, now)
                if job == 'stop_alert':
                    end = datetime.fromisoformat(slot).replace(tzinfo=now.tzinfo) + timedelta(minutes=5)
                    # Leave one second to release the lock before the next trigger.
                    deadline = time.monotonic() + min(POLICY_SECONDS, (end - now).total_seconds() - 1)
                deliver(job, slot, stdout=sys.stdout, deadline=deadline)
        return 0
    except ToolsError as exc:
        sys.stderr.write(str(exc) + '\n')
    except OSError:
        sys.stderr.write('定时任务的锁或输出流不可用，请核对执行记录。\n')
    return 1
