"""Bounded subprocess execution for trusted external evidence acquisition.

The private guardian watches a liveness pipe, so even an uncatchable worker death
kills the collector's process group. It never imports models or opens app.db.
"""
from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

MAX_BYTES = 32 * 1024 * 1024


class AcquisitionProcessError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(f'取证子进程未完成：{code}' + (f'；{detail}' if detail else ''))


class _Interrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


@contextmanager
def _interruptible():
    previous = {}
    received = []

    def interrupt(signum, frame):
        if not received:
            received.append(signum)
            raise _Interrupted(signum)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _command(liveness_fd: int) -> list[str]:
    return [sys.executable, '-m', __name__, '--guard', str(liveness_fd)]


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # The final descendant may exit between completion and cleanup.
    process.wait()


def _exchange(process: subprocess.Popen, payload: bytes, deadline: float) -> bytes:
    output = bytearray()
    remaining_input = memoryview(payload)
    with selectors.DefaultSelector() as selector:
        for stream, events in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcquisitionProcessError('timeout')
            for key, _ in selector.select(remaining):
                if key.fileobj is process.stdin:
                    try:
                        size = os.write(key.fd, remaining_input[:65536])
                    except BrokenPipeError:
                        size = len(remaining_input)
                    remaining_input = remaining_input[size:]
                    if not remaining_input:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(output) + len(chunk) > MAX_BYTES:
                        raise AcquisitionProcessError('result_too_large')
                    else:
                        output.extend(chunk)
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AcquisitionProcessError('timeout') from None
    return bytes(output)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('nonfinite JSON number')



def _failure(output: bytes) -> AcquisitionProcessError:
    """Only the collector's narrow validation-error envelope may cross the boundary."""
    try:
        result = json.loads(output, object_pairs_hook=_unique, parse_constant=_constant)
        if not isinstance(result, dict) or set(result) != {'error'}:
            raise ValueError('unexpected failure envelope')
        error = result['error']
        if not isinstance(error, dict) or set(error) != {'code', 'message'} or error['code'] != 'invalid_arguments':
            raise ValueError('unexpected failure code')
        detail = error['message']
        if not isinstance(detail, str) or not 1 <= len(detail) <= 1024 or not detail.isprintable():
            raise ValueError('invalid failure detail')
        return AcquisitionProcessError('invalid_arguments', detail)
    except (ValueError, UnicodeError, RecursionError):
        return AcquisitionProcessError('failed')


def run(request: dict, *, timeout: float) -> dict:
    """Run one fixed collector, kill descendants on every exit, return its JSON result.

    Timeout includes encoding, startup and I/O. Errors carry a stable code, never
    provider stderr or credentials. Termination signals retain their original
    behavior after cleanup. Failed or partial output must not be committed.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be a positive finite number')
    deadline = time.monotonic() + timeout
    if not isinstance(request, dict):
        raise ValueError('request must be an object')
    payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode()
    if len(payload) > MAX_BYTES:
        raise AcquisitionProcessError('request_too_large')
    process = None
    read_fd, write_fd = os.pipe()
    try:
        with _interruptible():
            try:
                if time.monotonic() >= deadline:
                    raise AcquisitionProcessError('timeout')
                process = subprocess.Popen(_command(read_fd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, pass_fds=(read_fd,), start_new_session=True)
                os.close(read_fd)
                read_fd = -1
                output = _exchange(process, payload, deadline)
                if process.returncode:
                    raise _failure(output)
            finally:
                if process is not None:
                    _kill_group(process)
                    process.stdin.close()
                    process.stdout.close()
    except _Interrupted as exc:
        # The context restored the worker's handler; honor it after reaping.
        signal.raise_signal(exc.signum)
        raise AcquisitionProcessError('interrupted') from None
    except OSError:
        raise AcquisitionProcessError('failed') from None
    finally:
        if read_fd >= 0:
            os.close(read_fd)
        os.close(write_fd)
    try:
        result = json.loads(output, object_pairs_hook=_unique, parse_constant=_constant)
        if not isinstance(result, dict) or set(result) != {'material', 'result'} or not isinstance(result['material'], dict):
            raise ValueError('invalid collector envelope')
        # Also catches JSON floats overflowing to infinity, including nested values.
        json.dumps(result, allow_nan=False)
    except (ValueError, UnicodeError, RecursionError):
        raise AcquisitionProcessError('invalid_result') from None
    if time.monotonic() >= deadline:
        raise AcquisitionProcessError('timeout')
    return result


def _guard(liveness_fd: int) -> int:
    def watch_parent():
        try:
            while os.read(liveness_fd, 1):
                pass
        finally:
            # This guardian and every collector descendant share this group.
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch_parent, daemon=True).start()
    child = subprocess.Popen([sys.executable, '-m', 'a_share_research.external_acquisition'],
                             stdin=sys.stdin, stdout=sys.stdout, stderr=subprocess.DEVNULL)
    return child.wait()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--guard':
        raise SystemExit(2)
    raise SystemExit(_guard(int(sys.argv[2])))
