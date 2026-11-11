"""Read-only recovery checks: captured research and explicitly structural account fixtures."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from a_share_research import backup, rollback, rollback_probe
from test_backup import database_rows, source as backup_source
from test_policy_account_input import bound_book as ledger_book


@pytest.fixture
def source(tmp_path):
    yield from backup_source.__wrapped__(tmp_path)


@pytest.fixture
def bound_book(tmp_path):
    yield from ledger_book.__wrapped__(tmp_path)


def test_snapshot_and_shipping_reader_preserve_source_and_bundle(source, tmp_path):
    directory = source[0]
    (directory / 'account-snapshot.json').write_text('{"structural":true}')
    before = database_rows(directory / 'app.db')
    target = tmp_path / 'probe'
    rollback.snapshot(directory / 'app.db', target)
    manifest = backup.verify(target / 'bundle')
    assert (target / 'side/account-snapshot.json').read_bytes() == (directory / 'account-snapshot.json').read_bytes()
    result = subprocess.run([sys.executable, '-c', Path(rollback_probe.__file__).read_text(), str(target)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(ok=True, reason='read_compatible', account_state=True, source_mode=None)
    assert backup.verify(target / 'bundle') == manifest
    assert database_rows(directory / 'app.db') == before
    assert database_rows(target / 'bundle/app.db') == before


def test_ledger_reader_checks_bound_evidence_and_does_not_migrate(bound_book, tmp_path):
    database, _, _, _, sha = bound_book
    before = database_rows(database)
    target = tmp_path / 'probe'
    rollback.snapshot(database, target)
    assert rollback_probe.probe(target)['source_mode'] == 'ledger'
    # Explicit missing-file variant of the fixture; the database itself remains valid.
    (target / 'bundle/account-evidence' / sha).unlink()
    with pytest.raises(ValueError):
        rollback_probe.probe(target)
    assert database_rows(database) == before
    assert database_rows(target / 'bundle/app.db') == before


def test_snapshot_refuses_sidefile_symlink_and_retains_private_diagnosis(source, tmp_path):
    directory = source[0]
    (directory / 'account-snapshot.json').symlink_to(directory / 'app.db')
    target = tmp_path / 'probe'
    result = rollback.assess({'worker': 'sha256:old'}, directory / 'app.db', target)
    assert result['ok'] is False and result['reason'] == 'snapshot_failed'
    assert result['images'] == {}
    assert backup.verify(target / 'bundle')['ok']
    assert not (target / 'side/account-snapshot.json').exists()


def test_missing_database_is_not_created_and_docker_is_not_run(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('must not run Docker without a snapshot')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    missing = tmp_path / 'missing.db'
    assert rollback.assess({'worker': 'old'}, missing, tmp_path / 'probe')['ok'] is False
    assert not missing.exists()


@pytest.mark.parametrize('stdout,code', [
    ('', 0), ('not-json', 1), ('[]', 0),
    ('{"ok":1,"reason":"read_compatible"}', 0),
    ('{"ok":true,"reason":"read_compatible"}', 1),
    ('{"ok":true,"reason":"image_reader_failed"}', 0),
    ('{"ok":true,"reason":"read_compatible","secret":"private"}', 0),
    ('{"ok":true,"reason":"read_compatible","source_mode":"private"}', 0),
    ('{"ok":false,"reason":{}}', 1),
    ('x' * 4097, 0),
])
def test_malformed_or_failed_container_never_approves_or_leaks(stdout, code, tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'rm':
            return subprocess.CompletedProcess(command, 1, '', 'Error: No such container: our-name')
        return subprocess.CompletedProcess(command, code, stdout, 'private stderr')
    monkeypatch.setattr(subprocess, 'run', run)
    result = rollback._assess_image('sha256:old', tmp_path)
    assert result == dict(ok=False, reason='image_probe_failed', image='sha256:old')
    assert calls[-1][:3] == ['docker', 'rm', '-f']


def test_container_timeout_cleans_only_own_container(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'run':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert rollback._assess_image('sha256:old', tmp_path)['ok'] is False
    start, cleanup = calls
    assert start[start.index('--name') + 1] == cleanup[-1]
    assert cleanup[-1].startswith('ashare-rollback-probe-')


def test_container_has_only_private_readonly_mount_and_no_credentials(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv('TUSHARE_TOKEN', 'must-not-forward')
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'rm':
            return subprocess.CompletedProcess(command, 0, '', '')
        return subprocess.CompletedProcess(command, 0,
            json.dumps(dict(ok=True, reason='read_compatible', account_state=True, source_mode='ledger')), '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert rollback._assess_image('sha256:old', tmp_path)['ok'] is True
    command = calls[0]
    assert command[command.index('--network') + 1] == 'none'
    assert command[command.index('--user') + 1] == f'{os.getuid()}:{os.getgid()}'
    assert command[command.index('--mount') + 1] == f'type=bind,src={tmp_path},dst=/snapshot,readonly'
    assert command.count('--mount') == 1 and '-v' not in command and '--env-file' not in command
    assert '--read-only' in command and '--cap-drop' in command
    assert 'must-not-forward' not in ' '.join(command)


def test_both_readers_share_one_snapshot_and_either_failure_blocks(tmp_path, monkeypatch):
    snapshots, images = [], []
    monkeypatch.setattr(rollback, 'snapshot', lambda *args: snapshots.append(args))
    def reader(image, directory):
        images.append((image, directory))
        return dict(ok=image == 'worker', reason='read_compatible' if image == 'worker' else 'image_reader_failed', image=image)
    monkeypatch.setattr(rollback, '_assess_image', reader)
    target = tmp_path / 'probe'
    result = rollback.assess(dict(web='web', worker='worker'), tmp_path / 'app.db', target)
    assert len(snapshots) == 1 and images == [('web', target), ('worker', target)]
    assert result['ok'] is False and result['images']['worker']['ok'] is True


def test_old_integrity_that_ignores_accounts_cannot_silently_pass(source, tmp_path, monkeypatch):
    target = tmp_path / 'probe'
    rollback.snapshot(source[0] / 'app.db', target)
    from a_share_research import integrity
    monkeypatch.setattr(integrity, 'check', lambda *args: ([], {}))
    # Structural older-runtime simulation: full inspector skips accounts and their module is absent.
    def unavailable(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr(rollback_probe.importlib, 'import_module', unavailable)
    with pytest.raises(ModuleNotFoundError):
        rollback_probe.probe(target)


def test_sqlite_foreign_key_violation_is_rejected_even_if_old_inspector_skips_it(source, tmp_path, monkeypatch):
    target = tmp_path / 'probe'
    rollback.snapshot(source[0] / 'app.db', target)
    with sqlite3.connect(target / 'bundle/app.db') as conn:
        conn.execute('CREATE TABLE structural_parent(id INTEGER PRIMARY KEY)')
        conn.execute('CREATE TABLE structural_child(parent INTEGER REFERENCES structural_parent(id))')
        conn.execute('INSERT INTO structural_child VALUES (1)')
    from a_share_research import integrity
    monkeypatch.setattr(integrity, 'check', lambda *args: ([], {}))
    assert rollback_probe.probe(target) == dict(ok=False, reason='sqlite_integrity_failed')


def test_pre_capture_reader_cannot_ignore_new_receipt_columns(source, tmp_path, monkeypatch):
    target = tmp_path / 'probe'
    rollback.snapshot(source[0] / 'app.db', target)
    with sqlite3.connect(target / 'bundle/app.db') as conn:
        # Transport-only sentinel, not a policy result; old generic inspectors below skip it.
        conn.execute('INSERT INTO policy_versions VALUES (?,?,?,?,?)', ('structural', 'v0', 'unused', 1, '{}'))
        conn.execute('INSERT INTO policy_evaluations '
                     '(identity,created_at,created_by,request_id,input_hash,result_hash,input,result,capture_context) '
                     'VALUES (?,?,?,?,?,?,?,?,?)',
                     ('structural', 'unused', 1, 'new-context', 'unused', 'unused', '{}', '{}', '{"schema_version":8}'))
    from a_share_research import integrity
    from a_share_research.policy import registry
    monkeypatch.setattr(integrity, 'check', lambda *args: ([], {}))
    monkeypatch.setattr(registry, 'inspect', lambda conn: [])
    monkeypatch.delattr(registry, '_capture_mode')
    with pytest.raises(AttributeError):
        rollback_probe.probe(target)


def test_cleanup_failure_also_refuses_rollback(tmp_path, monkeypatch):
    def run(command, **kwargs):
        if command[1] == 'rm':
            return subprocess.CompletedProcess(command, 1, '', 'private daemon error')
        return subprocess.CompletedProcess(command, 0,
            json.dumps(dict(ok=True, reason='read_compatible', account_state=False, source_mode=None)), '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert rollback._assess_image('old', tmp_path) == dict(ok=False, reason='probe_cleanup_failed',
                                                        image='old', account_state=False, source_mode=None)
