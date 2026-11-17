"""Opt-in real image/readiness checks on a NEW empty v23 database, never live data.

ASHARE_ISOLATED_IMAGE_TEST=1 pytest -q tests/test_release_policy_images.py
Optional ASHARE_IMAGE_REPORT writes a sanitized report. No images are built or pulled.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess

import pytest

from a_share_research import db, rollback

pytestmark = pytest.mark.skipif(os.environ.get('ASHARE_ISOLATED_IMAGE_TEST') != '1',
                                reason='explicit isolated local Docker image test only')

READY = '''
import json, threading, time, urllib.request, urllib.error
from pathlib import Path
import uvicorn
from a_share_research import db
from a_share_research.tools.server import create_app
from a_share_research.tools.control import Gate
control = Gate(marker=Path('/tmp/isolated-tools-paused'))
app = create_app(Path('/data/app.db'), account_shadow_enabled=True, gate=control)
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8621, log_level='critical'))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
for _ in range(100):
    if server.started: break
    time.sleep(.05)
def ready():
    try: response = urllib.request.urlopen('http://127.0.0.1:8621/ready', timeout=2)
    except urllib.error.HTTPError as e: response = e
    with response: return dict(http=response.status, body=json.load(response))
result = dict(image_schema=len(db.MIGRATIONS), ready=ready())
control.pause(0)
result['paused_ready'] = ready()
server.should_exit = True
thread.join(3)
print(json.dumps(result))
'''


def test_real_v23_readers_and_full_tools_readiness(tmp_path, monkeypatch):
    data = tmp_path/'data'
    data.mkdir()
    database = data/'app.db'
    # This historical image pair probes v23, not the current development schema.
    with monkeypatch.context() as legacy:
        legacy.setattr(db, 'MIGRATIONS', db.MIGRATIONS[:23])
        with closing(db.connect(database)) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 23
    reports = {}
    for code, expected in (('f480507', False), ('cb9a035', True)):
        images = {}
        for service in ('web', 'worker'):
            result = subprocess.run(['docker', 'image', 'inspect', f'a-share-research-{service}:{code}',
                                     '--format', '{{.Id}}'], capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, 'required local image missing (never pull implicitly)'
            images[service] = result.stdout.strip()
        probe = rollback.assess(images, database, tmp_path/('probe-'+code))
        result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
            '--tmpfs', '/tmp:rw,mode=1777', '--mount', f'type=bind,src={data},dst=/data,readonly',
            '--entrypoint', 'python', images['worker'], '-c', READY], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, 'isolated tools server probe failed'
        ready = json.loads(result.stdout)
        assert probe['ok'] is expected
        assert ready['ready']['http'] == (200 if expected else 503)
        assert ready['ready']['body']['database_ready'] is expected
        assert ready['paused_ready']['http'] == 503 and ready['paused_ready']['body']['paused'] is True
        assert ready['paused_ready']['body']['database_ready'] is expected
        reports[code] = dict(images=images, read_compatibility=probe, tools=ready)
    report = dict(scope='isolated_empty_v23_only', production_acceptance=False,
                  policy_activation_and_orchestration='covered separately by local registry and simulated Docker tests',
                  reports=reports)
    if os.environ.get('ASHARE_IMAGE_REPORT'):
        Path(os.environ['ASHARE_IMAGE_REPORT']).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
