"""Monthly cron entry points deliver the service text and acknowledge the same run."""
from datetime import datetime
import subprocess
from zoneinfo import ZoneInfo

import pytest

from support.hermes import SOURCE


@pytest.mark.parametrize('mode,fmt', [('submit', '%Y-%m'), ('status', '%Y-%m-%dT%H')])
def test_monthly_cron_delivers_server_receipt(ashare_cli, tmp_path, monkeypatch, mode, fmt):
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    job = 'monthly_' + mode
    text = '【月批】正文\n'
    ashare_cli.reply('schedule_run', {'job': job, 'slot': '$slot', 'run_id': 3, 'status': 'ready', 'text': text})
    ashare_cli.reply('schedule_ack', {'run_id': 3, 'status': 'acked'})
    shanghai = ZoneInfo('Asia/Shanghai')
    before = datetime.now(shanghai)
    result = subprocess.run([str(SOURCE / f'a-share-monthly-{mode}.sh')], text=True, capture_output=True, timeout=5)
    after = datetime.now(shanghai)
    assert (result.returncode, result.stdout, result.stderr) == (0, text, '')
    (name, request), ack = ashare_cli.calls
    assert name == 'schedule_run' and request['job'] == job
    assert request['slot'] in (before.strftime(fmt), after.strftime(fmt))
    assert ack == ('schedule_ack', {'run_id': 3})
    assert all(path.suffix == '.lock' for path in home.rglob('*') if path.is_file())
