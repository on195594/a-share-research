"""Private, read-only rollback compatibility checks; never restore a live database."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import TypedDict
import uuid

from a_share_research import backup


class ImageReport(TypedDict, total=False):
    ok: bool
    reason: str
    image: str
    account_state: bool
    source_mode: str | None


class Report(TypedDict):
    ok: bool
    scope: str
    reason: str
    images: dict[str, ImageReport]


def snapshot(database: Path, destination: Path) -> None:
    """Copy a stopped deployment's database and evidence into an exclusive private directory."""
    destination = backup._new(destination)
    backup.create(database, destination / 'bundle')
    side = destination / 'side'
    side.mkdir(mode=0o700)
    source = database.parent / 'account-snapshot.json'
    if source.exists() or source.is_symlink():
        backup._copy(source, side / source.name)


def assess(images: dict[str, str], database: Path, evidence_directory: Path) -> Report:
    """Run old immutable images on one fresh copy; uncertainty refuses automatic rollback.

    Call only after all current writers have stopped. The snapshot is retained for diagnosis.
    No host credentials, sockets, live data mounts, or network are exposed to the image.
    """
    report: Report = {'ok': False, 'reason': 'snapshot_failed', 'scope': 'read_only', 'images': {}}
    try:
        snapshot(database, evidence_directory)
    except (ValueError, OSError, RuntimeError, sqlite3.Error):
        return report
    for service, image in images.items():
        report['images'][service] = _assess_image(image, evidence_directory)
    report['ok'] = bool(images) and all(item['ok'] for item in report['images'].values())
    report['reason'] = 'read_compatible' if report['ok'] else 'image_probe_failed'
    return report


def _assess_image(image: str, evidence_directory: Path) -> ImageReport:
    report: ImageReport = {'ok': False, 'reason': 'image_probe_failed', 'image': image}
    name = 'ashare-rollback-probe-' + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--pull', 'never', '--name', name,
               '--network', 'none', '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--pids-limit', '64',
               '--cpus', '1.2', '--memory', '512m', '--user', f'{os.getuid()}:{os.getgid()}',
               '--mount', f'type=bind,src={evidence_directory.absolute()},dst=/snapshot,readonly',
               '--env', 'DATA_DIR=/snapshot/bundle', '--env', 'PYTHONDONTWRITEBYTECODE=1',
               '--entrypoint', 'python', image, '-c']
    try:
        command.extend([Path(__file__).with_name('rollback_probe.py').read_text(), '/snapshot'])
        process = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if len(process.stdout) > 4096:
            raise ValueError('oversize probe output')
        result = json.loads(process.stdout)
        reasons = {'read_compatible', 'image_integrity_failed', 'sqlite_integrity_failed',
                   'account_policy_integrity_failed', 'account_source_invalid',
                   'mirror_digest_mismatch', 'image_reader_failed', 'scan_requests_unsupported'}
        if (not isinstance(result, dict) or type(result.get('ok')) is not bool
                or not isinstance(result.get('reason'), str)
                or result.get('reason') not in reasons
                or set(result) - {'ok', 'reason', 'account_state', 'source_mode'}
                or ('account_state' in result and type(result['account_state']) is not bool)
                or result.get('source_mode') not in (None, 'mirror', 'ledger')
                or result['ok'] != (result['reason'] == 'read_compatible')
                or (result['ok'] and set(result) != {'ok', 'reason', 'account_state', 'source_mode'})
                or process.returncode != (0 if result['ok'] else 1)):
            raise ValueError('invalid probe result')
        report.update(result)
    except (OSError, ValueError, subprocess.SubprocessError):
        report.update(ok=False, reason='image_probe_failed')
    finally:
        # Killing a timed-out Docker CLI alone does not stop its container. Only remove our unique name.
        try:
            cleanup = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=15)
            if cleanup.returncode and 'No such container' not in cleanup.stderr:
                report.update(ok=False, reason='probe_cleanup_failed')
        except (OSError, subprocess.SubprocessError):
            report.update(ok=False, reason='probe_cleanup_failed')
    return report
