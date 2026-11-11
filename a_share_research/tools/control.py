"""Private local release control, independent of HTTP credentials and database state."""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import socket
import stat
import threading
import time

DEFAULT_SOCKET = Path('/run/ashare-tools/control.sock')
_MARKER = b'{"schema":1,"paused":true}\n'


class ControlError(RuntimeError):
    def __init__(self, code, message=None, status=503):
        super().__init__(message or code)
        self.code, self.status = code, status


def _timeout(value):
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ControlError('invalid_command', 'timeout must be finite and nonnegative', 400)
    return value


@contextmanager
def _parent(path, *, private=False, create=False):
    """Walk with directory descriptors so no path component follows a symlink."""
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            if part in ('.', '..'):
                raise ControlError('unsafe_path')
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ControlError('unsafe_path', 'control socket directory must be owned and mode 0700')
        yield fd, path.name
    finally:
        os.close(fd)


def _read_marker(fd, name):
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ControlError('invalid_marker', 'pause marker cannot be safely opened') from exc
    with os.fdopen(handle, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or stream.read(len(_MARKER) + 1) != _MARKER):
            raise ControlError('invalid_marker', 'pause marker identity or contents are invalid')
    return info


def _marker_exists(path):
    try:
        with _parent(path) as (fd, name):
            return _read_marker(fd, name) is not None
    except FileNotFoundError:
        return False


def pause_offline(marker):
    """Persist the same pause marker before the service's first startup; never overwrite."""
    with _parent(marker, create=True) as (fd, name):
        try:
            handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            if _read_marker(fd, name) is None:
                raise ControlError('invalid_marker')
            return
        with os.fdopen(handle, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(_MARKER)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(fd)


def _remove_marker(marker):
    with _parent(marker) as (fd, name):
        info = _read_marker(fd, name)
        if info is None:
            return
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ControlError('invalid_marker', 'pause marker changed during resume')
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)


class Gate:
    def __init__(self, marker: Path | None = None, limit=2):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be a positive integer')
        self.marker = Path(marker) if marker is not None else None
        self._condition = threading.Condition()
        self._operations = threading.Lock()
        self._limit, self._in_flight = limit, 0
        self._paused = _marker_exists(self.marker) if self.marker is not None else False

    @contextmanager
    def acquire(self):
        with self._condition:
            if self._paused:
                raise ControlError('paused')
            if self._in_flight >= self._limit:
                raise ControlError('busy')
            self._in_flight += 1
        try:
            yield
        finally:
            with self._condition:
                self._in_flight -= 1
                self._condition.notify_all()

    def status(self):
        with self._condition:
            return {'paused': self._paused, 'in_flight': self._in_flight}

    def pause(self, timeout=30):
        timeout = _timeout(timeout)
        with self._operations:
            with self._condition:
                self._paused = True
                self._condition.notify_all()
                if self.marker is not None:
                    pause_offline(self.marker)
                deadline = time.monotonic() + timeout
                while self._in_flight:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ControlError('timeout', 'drain timed out; service remains paused')
                    self._condition.wait(remaining)
                return self.status()

    def resume(self):
        with self._operations, self._condition:
            if self.marker is not None and _marker_exists(self.marker):
                _remove_marker(self.marker)
            self._paused = False
            return self.status()


def _line(stream):
    raw = stream.readline(4097)
    if len(raw) > 4096 or not raw.endswith(b'\n'):
        raise ControlError('invalid_command', 'expected one JSON line, at most 4096 bytes', 400)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ControlError('invalid_command', 'invalid JSON', 400) from None


class ControlServer:
    """One serial operations thread behind a private Unix socket."""
    def __init__(self, gate, path=DEFAULT_SOCKET):
        self.gate, self.path = gate, Path(path).absolute()
        self._stop = threading.Event()
        self._socket = None
        self._identity = None
        self._thread = None

    def __enter__(self):
        with _parent(self.path, private=True, create=True):
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self._socket.bind(str(self.path))
                info = self.path.lstat()
                self._identity = (info.st_dev, info.st_ino)
                self.path.chmod(0o600)
                self._socket.listen(8)
                self._socket.settimeout(0.2)
                self._thread = threading.Thread(target=self._serve, name='tools-control', daemon=True)
                self._thread.start()
            except BaseException:
                self._socket.close()
                self._cleanup()
                raise
        return self

    def _cleanup(self):
        if self._identity is not None:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self._identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass

    def __exit__(self, *exc):
        self._stop.set()
        self._socket.close()
        self._thread.join()
        self._cleanup()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    with connection.makefile('rb') as stream:
                        command = _line(stream)
                    if not isinstance(command, dict) or set(command) - {'action', 'timeout'}:
                        raise ControlError('invalid_command', status=400)
                    action = command.get('action')
                    timeout = _timeout(command.get('timeout', 30))
                    if action == 'status':
                        result = self.gate.status()
                    elif action == 'pause':
                        result = self.gate.pause(timeout)
                    elif action == 'resume':
                        result = self.gate.resume()
                    else:
                        raise ControlError('invalid_command', status=400)
                except ControlError as error:
                    result = {'error': {'code': error.code, 'message': str(error), 'status': error.status}}
                except OSError:
                    result = {'error': {'code': 'control_unavailable', 'message': 'control operation failed', 'status': 503}}
                try:
                    connection.sendall(json.dumps(result).encode() + b'\n')
                except OSError:
                    pass


def client(path, action, timeout=30):
    timeout = _timeout(timeout)
    path = Path(path).absolute()
    with _parent(path, private=True):
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ControlError('unsafe_path', 'control endpoint must be an owned mode 0600 socket')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout + 1)
            connection.connect(str(path))
            connection.sendall(json.dumps({'action': action, 'timeout': timeout}).encode() + b'\n')
            with connection.makefile('rb') as stream:
                result = _line(stream)
    if not isinstance(result, dict):
        raise ControlError('invalid_response')
    if 'error' in result:
        raise ControlError(**result['error'])
    if set(result) != {'paused', 'in_flight'} or type(result['paused']) is not bool or type(result['in_flight']) is not int:
        raise ControlError('invalid_response')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'pause', 'resume'))
    parser.add_argument('--socket', type=Path, default=DEFAULT_SOCKET)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        result = client(args.socket, args.action, args.timeout)
    except (ControlError, OSError) as error:
        print(json.dumps({'error': {'code': getattr(error, 'code', 'control_unavailable'), 'message': str(error)}}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
