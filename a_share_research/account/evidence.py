"""Private content-addressed account evidence; never logs document contents."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

MAX_BYTES = 20 * 1024 * 1024


class EvidenceError(ValueError):
    """Missing, unsafe or corrupted account evidence."""


def _directory(directory: Path, *, create: bool = False) -> int:
    directory = Path(directory)
    if not directory.is_absolute():
        raise EvidenceError('凭证目录必须为绝对路径')
    # Do not follow symlinks in any directory component, including pre-existing ancestors.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in directory.parts[1:]:
            if component in ('.', '..'):
                raise EvidenceError('凭证目录不能包含相对路径片段')
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        metadata = os.fstat(fd)
        if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
            raise EvidenceError('凭证目录必须由当前用户拥有且权限为0700')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _name(sha: str) -> str:
    if not isinstance(sha, str) or re.fullmatch(r'[0-9a-f]{64}', sha) is None:
        raise EvidenceError('凭证哈希须为64位小写十六进制')
    return sha


def _read(fd: int, sha: str) -> bytes:
    handle = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid() or metadata.st_size > MAX_BYTES):
            raise EvidenceError('凭证文件类型、权限或大小不合规')
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES or hashlib.sha256(content).hexdigest() != sha:
        raise EvidenceError('凭证内容与哈希不符')
    return content


def read(directory: Path, sha: str) -> bytes:
    """Read verified evidence; missing files and unsafe paths fail closed."""
    sha = _name(sha)
    try:
        fd = _directory(directory)
        try:
            return _read(fd, sha)
        finally:
            os.close(fd)
    except OSError as exc:
        raise EvidenceError('凭证缺失或路径不可安全访问') from exc


def verify(directory: Path, sha: str) -> bool:
    read(directory, sha)
    return True


def store(directory: Path, content: bytes) -> str:
    """Atomically install immutable bytes without replacing an existing object."""
    if not isinstance(content, bytes) or not content or len(content) > MAX_BYTES:
        raise EvidenceError('凭证须为非空bytes，且不超过20MiB')
    sha = hashlib.sha256(content).hexdigest()
    try:
        fd = _directory(directory, create=True)
        temporary = '.pending-' + secrets.token_hex(16)
        created = False
        try:
            try:
                _read(fd, sha)
                return sha
            except FileNotFoundError:
                pass
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            created = True
            with os.fdopen(handle, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, sha, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                _read(fd, sha)
            os.fsync(fd)
            return sha
        finally:
            if created:
                os.unlink(temporary, dir_fd=fd)
            os.close(fd)
    except OSError as exc:
        raise EvidenceError('凭证无法安全保存') from exc
