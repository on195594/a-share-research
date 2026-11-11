"""Linux main-process deadline for the Hermes advisor, including blocking transports."""
from collections.abc import Callable
import ctypes
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time


class BudgetError(RuntimeError):
    """Safe failure text; child exceptions and diagnostic streams never escape."""


def _signal_group(pid: int, signum: int, finished: bool) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        # Cancellation can precede the child's setsid(), including the fork-return gap.
        if not finished:
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass


def _stop(pid: int, status: int | None) -> None:
    def reaped() -> bool:
        nonlocal status
        if status is None:
            waited, value = os.waitpid(pid, os.WNOHANG)
            if not waited:
                return False
            status = value
        # Only reap this child's session, never another child of the caller.
        while True:
            try:
                waited, _ = os.waitpid(-pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if not waited:
                return False

    for signum in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(pid, signum, status is not None)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if reaped():
                return
            time.sleep(.02)
    raise BudgetError('顾问子进程清理未完成；请核对任务进程和待查记录')


def run_bounded(callback: Callable[[float], str], seconds: float = 540) -> str:
    """Run one advisor callback under a shared monotonic deadline and reap its group.

    The caller must be a dedicated Linux main process and retain its task lock
    until this returns. The callback must not detach children into new sessions.
    TERM/INT request cancellation without raising across fork. Cleanup has at
    most four seconds of polling after the work deadline. SIGKILL of this
    supervisor cannot be handled; normal Hermes termination must allow cleanup.
    """
    if (threading.current_thread() is not threading.main_thread()
            or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0):
        raise BudgetError('顾问执行预算或运行线程无效')
    deadline = time.monotonic() + seconds
    libc = ctypes.CDLL(None, use_errno=True)
    original = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(original), 0, 0, 0) != 0 or libc.prctl(36, 1, 0, 0, 0) != 0:
        raise BudgetError('顾问进程回收监督不可用')
    cancelled = False

    def cancel(signum, frame):
        nonlocal cancelled
        cancelled = True

    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGINT)}
    pid, status = None, None
    try:
        with tempfile.TemporaryDirectory(prefix='ashare-advisor-') as directory:
            output = Path(directory) / 'result.json'
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                try:
                    os.setsid()
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                    with open(os.devnull, 'r+b', buffering=0) as stream:
                        for descriptor in (0, 1, 2):
                            os.dup2(stream.fileno(), descriptor)
                    if cancelled or time.monotonic() >= deadline:
                        os._exit(1)
                    value = callback(deadline)
                    if not isinstance(value, str):
                        os._exit(1)
                    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'w') as stream:
                        json.dump(value, stream, ensure_ascii=False)
                        stream.flush()
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            try:
                while True:
                    waited, value = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        status = value
                    if cancelled:
                        raise BudgetError('顾问任务已取消；下次按本地待查记录恢复')
                    if status is not None:
                        break
                    if time.monotonic() >= deadline:
                        raise BudgetError('顾问执行超过整轮时限；下次按本地待查记录恢复')
                    time.sleep(min(.05, max(0, deadline - time.monotonic())))
            finally:
                try:
                    _stop(pid, status)
                finally:
                    # Cleanup already owned this PID, even if it reported failure.
                    # A second wait can reap an already-reaped PID or hide its error.
                    pid = None
            if status != 0:
                raise BudgetError('顾问子任务未完成；请核对服务与本地待查记录')
            value = json.loads(output.read_text())
            if not isinstance(value, str):
                raise BudgetError('顾问子任务回执无效；请核对本地待查记录')
            return value
    except BudgetError:
        raise
    except Exception:
        raise BudgetError('顾问执行监督失败；请核对任务进程和待查记录') from None
    finally:
        try:
            if pid is not None and pid > 0:
                _stop(pid, status)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            libc.prctl(36, original.value, 0, 0, 0)
