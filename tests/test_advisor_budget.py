"""Structural process fixtures only: deadlines, ownership and diagnostic isolation."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


HELPER = Path(__file__).resolve().parents[1] / 'ops/hermes'
PRELUDE = f'''import json,os,signal,sys,time
from pathlib import Path
sys.path.insert(0,{str(HELPER)!r})
import advisor_budget as budget
'''


def command(body):
    return [sys.executable, '-c', PRELUDE + body]


def run(tmp_path, body, timeout=10):
    return subprocess.run(command(body), env=os.environ | {'TMPDIR': str(tmp_path)},
                          capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize('text', ['', '完成：已核对'])
def test_result_only_and_temporary_files_removed(tmp_path, text):
    body = f'''
def child(deadline):
    assert time.monotonic() < deadline <= time.monotonic()+5
    print('private child stdout',flush=True)
    print('private child stderr',file=sys.stderr,flush=True)
    assert list(Path({str(tmp_path)!r}).glob('ashare-advisor-*'))
    return {text!r}
print(json.dumps(budget.run_bounded(child,5)))
'''
    result = run(tmp_path, body)
    assert result.returncode == 0 and result.stderr == ''
    assert json.loads(result.stdout) == text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('operation', ["raise ValueError('private credential')", "return {'invalid':True}", 'os._exit(0)'])
def test_child_failure_or_missing_receipt_is_safe(tmp_path, operation):
    result = run(tmp_path, f'''
def child(deadline):
    print('private output',flush=True)
    {operation}
try: budget.run_bounded(child,2)
except budget.BudgetError as exc: print(str(exc))
else: raise AssertionError('must fail')
''')
    assert result.returncode == 0 and result.stderr == ''
    assert '顾问' in result.stdout and 'private' not in result.stdout
    assert list(tmp_path.iterdir()) == []


FORK_CHILD = '''
def child(deadline):
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    descendant=os.fork()
    if descendant==0:
        while True: time.sleep(10)
    Path(PIDS).write_text(json.dumps([os.getpid(),descendant]))
    while True: time.sleep(10)
'''


def assert_gone(path):
    pids = json.loads(path.read_text())
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_deadline_kills_and_reaps_term_resistant_descendants(tmp_path):
    pids = tmp_path / 'pids.json'
    start = time.monotonic()
    result = run(tmp_path, f'PIDS={str(pids)!r}\n' + FORK_CHILD + '''
try: budget.run_bounded(child,.3)
except budget.BudgetError as exc: print(str(exc))
else: raise AssertionError('must time out')
''')
    assert result.returncode == 0 and '整轮时限' in result.stdout and result.stderr == ''
    assert .3 <= time.monotonic()-start < 7
    assert_gone(pids)
    assert not list(tmp_path.glob('ashare-advisor-*'))


def test_parent_termination_preserves_lock_until_group_is_reaped(tmp_path):
    pids, lock = tmp_path/'pids.json', tmp_path/'scan.lock'
    body = f'PIDS={str(pids)!r}\n' + FORK_CHILD + f'''
import fcntl
with open({str(lock)!r},'w') as handle:
    fcntl.flock(handle,fcntl.LOCK_EX)
    try: budget.run_bounded(child,30)
    except budget.BudgetError as exc: print(str(exc),flush=True)
'''
    process = subprocess.Popen(command(body), env=os.environ | {'TMPDIR': str(tmp_path)},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        end = time.monotonic()+5
        while not pids.exists():
            assert process.poll() is None and time.monotonic() < end
            time.sleep(.02)
        process.send_signal(signal.SIGTERM)
        # During the TERM grace window neither callback descendants nor its parent release ownership.
        import fcntl
        with lock.open('a') as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0 and '已取消' in stdout and stderr == ''
        assert_gone(pids)
        with lock.open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not list(tmp_path.glob('ashare-advisor-*'))
    finally:
        if process.poll() is None:
            process.kill();process.communicate(timeout=3)


def test_cancel_in_fork_return_gap_does_not_orphan_child(tmp_path):
    pid_path = tmp_path/'pid'
    result = run(tmp_path, f'''
fork=os.fork
def interrupted_fork():
    pid=fork()
    if pid:
        Path({str(pid_path)!r}).write_text(str(pid))
        os.kill(os.getpid(),signal.SIGTERM)
    return pid
budget.os.fork=interrupted_fork
try: budget.run_bounded(lambda deadline:time.sleep(60),30)
except budget.BudgetError as exc: print(str(exc))
''')
    assert result.returncode == 0 and '已取消' in result.stdout and result.stderr == ''
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()),0)
    assert not list(tmp_path.glob('ashare-advisor-*'))


def test_success_also_cleans_descendants_and_restores_supervisor_state(tmp_path):
    pids = tmp_path/'pids.json'
    result = run(tmp_path, f'''
import ctypes
before=ctypes.c_int()
ctypes.CDLL(None).prctl(37,ctypes.byref(before),0,0,0)
handler=signal.getsignal(signal.SIGTERM)
def child(deadline):
    descendant=os.fork()
    if descendant==0:
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        while True: time.sleep(10)
    Path({str(pids)!r}).write_text(json.dumps([os.getpid(),descendant]))
    return 'ok'
assert budget.run_bounded(child,5)=='ok'
after=ctypes.c_int()
ctypes.CDLL(None).prctl(37,ctypes.byref(after),0,0,0)
assert before.value==after.value and handler==signal.getsignal(signal.SIGTERM)
print('ok')
''')
    assert result.returncode == 0 and result.stdout.strip() == 'ok' and result.stderr == ''
    assert_gone(pids)
    assert not list(tmp_path.glob('ashare-advisor-*'))


def test_cleanup_failure_is_not_overwritten_by_a_second_wait(tmp_path):
    result = run(tmp_path, '''
stop=budget._stop
calls=0
def failed_cleanup(pid,status):
    global calls
    calls+=1
    stop(pid,status)
    raise budget.BudgetError('结构清理失败')
budget._stop=failed_cleanup
try: budget.run_bounded(lambda deadline:'ok',2)
except budget.BudgetError as exc: assert str(exc)=='结构清理失败'
else: raise AssertionError('must fail')
assert calls==1
print('ok')
''')
    assert result.returncode == 0 and result.stdout.strip() == 'ok' and result.stderr == ''
    assert not list(tmp_path.glob('ashare-advisor-*'))
