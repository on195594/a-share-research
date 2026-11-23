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
    if job == 'benchmark':
        return now.strftime('%Y-%m')
    if job == 'advisor_weekly':
        return now.strftime('%G-W%V')
    return now.date().isoformat()


POLICY_SECONDS = 270


def failure_line(error):
    return '【定时任务失败】' + ' '.join(str(error).split())[:160] + '\n'


def report_failure(error, written):
    if written[0]:
        if written[0] != ord('\n'):
            sys.stdout.write('\n')
        sys.stdout.write(failure_line(error))
        return 0
    sys.stderr.write(str(error) + '\n')
    return 1


def deliver(job, slot, *, stdout, render=None, deadline=float('inf'), written=None):
    if written is None:
        written = bytearray(1)

    def write(text, *, body=True):
        if not isinstance(text, str):
            raise ToolsError('定时运行正文无效')
        if text:
            stdout.write(text)
            stdout.flush()
            if body or written[0]:
                written[0] = ord('\n') if text.endswith('\n') else ord(' ')

    def call(name, arguments):
        retry_until = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolsError('本轮执行时限已到，请按原时段查回')
            try:
                return tool(name, arguments, timeout=min(40, remaining))
            except ToolsError as exc:
                # schedule_run and schedule_ack are idempotent, so a lost reply is looked up again.
                if exc.code != 'service_unavailable' and not exc.transient:
                    raise
                now = time.monotonic()
                if retry_until is None:
                    retry_until = min(now + 240, deadline)
                if now >= retry_until:
                    raise ToolsError(f'{name} 服务仍不可用，本轮重试时限已到') from None
                time.sleep(min(20, retry_until - now))

    args = dict(job=job, slot=slot)
    backlog_error = None
    while True:
        row = call('schedule_run', args)
        run_id = row.get('run_id')
        # The service returns an earlier scan still awaiting its result before today's slot.
        backlog = job == 'advisor_scan' and isinstance(row.get('slot'), str) and row['slot'] < slot
        if (row.get('job') != job or (row.get('slot') != slot and not backlog)
                or type(run_id) is not int or run_id < 1):
            raise ToolsError('定时运行回执与本次时段不符')
        status = row.get('status')
        if status == 'running':
            if render is None:
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

        # A failed monitor can leave a completed disclosure scan or weekly digest.
        if render and (status == 'ready' or row.get('result')):
            write(render(row['result']))
        if status == 'failed':
            error = f"{row['error_code']}：{row['error']}"
            if not backlog:
                raise ToolsError(error)
            backlog_error = error
            write(failure_line(error), body=False)
        elif status == 'expired':
            if backlog and row.get('error_code'):
                backlog_error = row['text']
            write(row.get('text'), body=False)
        else:
            if render is None:
                write(row.get('text'))
                if (row.get('result') or {}).get('stderr'):
                    sys.stderr.write(row['result']['stderr'])
            try:
                ack = call('schedule_ack', {'run_id': run_id})
                if type(ack.get('run_id')) is not int or ack['run_id'] != run_id or ack.get('status') != 'acked':
                    raise ToolsError('确认回执与本次运行不符')
            except ToolsError as exc:
                raise ToolsError(f'运行 #{run_id} 送达确认失败。{exc}') from None
        if not backlog:
            break
    if backlog_error and not written[0]:
        raise ToolsError(backlog_error.rstrip())


def policy_main(job):
    if len(sys.argv) != 1:
        sys.stderr.write(f'usage: {Path(sys.argv[0]).name}\n')
        return 2
    written = bytearray(1)
    try:
        with task_lock(lock_path(job)) as acquired:
            if acquired:
                started = time.monotonic()
                budget = POLICY_SECONDS
                now = datetime.now(ZoneInfo('Asia/Shanghai'))
                slot = current_slot(job, now)
                if job == 'stop_alert':
                    end = datetime.fromisoformat(slot).replace(tzinfo=now.tzinfo) + timedelta(minutes=5)
                    # Leave one second to release the lock before the next trigger.
                    budget = min(budget, (end - now).total_seconds() - 1)
                deliver(job, slot, stdout=sys.stdout, deadline=started + budget, written=written)
        return 0
    except ToolsError as exc:
        return report_failure(exc, written)
    except OSError:
        return report_failure('定时任务的锁或输出流不可用，请核对执行记录。', written)
