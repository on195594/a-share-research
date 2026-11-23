"""Hermes delivery of service-owned scheduled runs."""
from contextlib import contextmanager
from datetime import datetime
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


def current_slot(job):
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if job == 'stop_alert':
        return now.replace(minute=now.minute // 5 * 5).strftime('%Y-%m-%dT%H:%M')
    if job == 'benchmark':
        return now.strftime('%Y-%m')
    if job == 'advisor_weekly':
        return now.strftime('%G-W%V')
    return now.date().isoformat()


def deliver(job, slot, *, stdout, render=None, deadline=float('inf')):
    retry_until = None

    def call(name, arguments):
        nonlocal retry_until
        while True:
            if time.monotonic() >= deadline:
                raise ToolsError('本轮执行时限已到，请按原时段查回')
            try:
                return tool(name, arguments)
            except ToolsError as exc:
                if exc.code != 'service_unavailable':
                    raise
                now = time.monotonic()
                if retry_until is None:
                    retry_until = min(now + 240, deadline)
                if now >= retry_until:
                    raise ToolsError(f'{name} 服务仍不可用，本轮重试时限已到') from None
                time.sleep(min(20, retry_until - now))

    args = dict(job=job, slot=slot)
    while True:
        row = call('schedule_run', args)
        run_id = row.get('run_id')
        if row.get('job') != job or row.get('slot') != slot or type(run_id) is not int or run_id < 1:
            raise ToolsError('定时运行回执与本次时段不符')
        status = row.get('status')
        if status != 'running':
            break
        if render is None:
            raise ToolsError('该时段正在处理，请稍后用原时段查回')
        remaining = deadline - time.monotonic()
        if remaining <= 20:
            raise ToolsError(f'本轮轮询时限已到，请查回运行 #{run_id}')
        time.sleep(20)

    if status == 'acked':
        return
    if status not in ('ready', 'expired', 'failed'):
        raise ToolsError('定时运行回执状态无效')
    if status == 'failed':
        # A completed scan/weekly digest still reaches Hermes if the subsequent monitor fails.
        text = render(row['result']) if render and row.get('result') else ''
    else:
        text = render(row['result']) if render and status == 'ready' else row.get('text')
    if not isinstance(text, str):
        raise ToolsError('定时运行正文无效')
    if text:
        stdout.write(text)
        stdout.flush()
    if status == 'failed':
        raise ToolsError(f"{row['error_code']}：{row['error']}")
    if status == 'expired':
        return
    if render is None and (row.get('result') or {}).get('stderr'):
        sys.stderr.write(row['result']['stderr'])
    try:
        ack = call('schedule_ack', {'run_id': run_id})
        if type(ack.get('run_id')) is not int or ack['run_id'] != run_id or ack.get('status') != 'acked':
            raise ToolsError('确认回执与本次运行不符')
    except ToolsError as exc:
        raise ToolsError(f'运行 #{run_id} 的正文已输出，送达确认失败。{exc}') from None


def policy_main(job):
    if len(sys.argv) != 1:
        sys.stderr.write(f'usage: {Path(sys.argv[0]).name}\n')
        return 2
    try:
        with task_lock(lock_path(job)) as acquired:
            if acquired:
                deliver(job, current_slot(job), stdout=sys.stdout)
        return 0
    except ToolsError as exc:
        sys.stderr.write(f'{exc}\n')
    except OSError:
        sys.stderr.write('定时任务的锁或输出流不可用，请核对执行记录。\n')
    return 1
