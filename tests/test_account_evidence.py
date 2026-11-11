"""Byte storage safety boundaries; these are not financial/screenshot fixtures."""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from a_share_research.account import evidence


def test_atomic_deduplicated_private_storage(tmp_path):
    directory = tmp_path / 'private' / 'evidence'
    content = b'evidence storage boundary sample'
    with ThreadPoolExecutor(max_workers=2) as pool:
        hashes = list(pool.map(lambda _: evidence.store(directory, content), range(8)))
    assert set(hashes) == {hashlib.sha256(content).hexdigest()}
    assert list(directory.iterdir()) == [directory / hashes[0]]
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / hashes[0]).stat().st_mode & 0o777 == 0o600
    assert evidence.read(directory, hashes[0]) == content
    assert evidence.verify(directory, hashes[0]) is True


def test_missing_and_bad_hash_do_not_create_files(tmp_path):
    directory = tmp_path / 'missing'
    for sha in ('../escape', 'A' * 64, 'x', '0' * 64):
        with pytest.raises(evidence.EvidenceError):
            evidence.verify(directory, sha)
    assert not directory.exists()


def test_directory_permissions_and_symlinks_rejected(tmp_path):
    public = tmp_path / 'public'
    public.mkdir(mode=0o755)
    with pytest.raises(evidence.EvidenceError):
        evidence.store(public, b'input')
    assert public.stat().st_mode & 0o777 == 0o755
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(private, target_is_directory=True)
    for directory in (link, link / 'nested'):
        with pytest.raises(evidence.EvidenceError):
            evidence.store(directory, b'input')
    assert not list(private.iterdir())


def test_tampering_and_existing_link_never_overwritten(tmp_path):
    directory = tmp_path / 'evidence'
    content = b'original'
    sha = evidence.store(directory, content)
    target = directory / sha
    target.write_bytes(b'tampered')
    for operation in (lambda: evidence.read(directory, sha), lambda: evidence.store(directory, content)):
        with pytest.raises(evidence.EvidenceError):
            operation()
    assert target.read_bytes() == b'tampered'
    target.unlink()
    external = tmp_path / 'external'
    external.write_bytes(content)
    external.chmod(0o600)
    target.symlink_to(external)
    with pytest.raises(evidence.EvidenceError):
        evidence.store(directory, content)
    assert target.is_symlink() and external.read_bytes() == content


def test_bad_inputs_and_file_permissions(tmp_path):
    for content in ('text', b'', b'x' * (evidence.MAX_BYTES + 1)):
        with pytest.raises(evidence.EvidenceError):
            evidence.store(tmp_path / 'evidence', content)
    with pytest.raises(evidence.EvidenceError):
        evidence.store(__import__('pathlib').Path('relative'), b'input')
    directory = tmp_path / 'evidence'
    sha = evidence.store(directory, b'input')
    (directory / sha).chmod(0o644)
    with pytest.raises(evidence.EvidenceError):
        evidence.verify(directory, sha)
    assert (directory / sha).stat().st_mode & 0o777 == 0o644


def test_failed_install_removes_temporary_file(tmp_path, monkeypatch):
    directory = tmp_path / 'evidence'
    def fail(*args, **kwargs):
        raise OSError('controlled failure')
    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(evidence.EvidenceError):
        evidence.store(directory, b'input')
    assert not list(directory.iterdir())
