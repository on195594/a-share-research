"""Real subprocess lifecycle tests; payloads are protocol fixtures, not research evidence."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from a_share_research import acquisition_process as process


RESULT = {'material': {'sha256': 'protocol-fixture'}, 'result': {'ok': True}}


def command(monkeypatch, script):
    monkeypatch.setattr(process, '_command', lambda fd: [sys.executable, '-c', script])


def stopped(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split()[2] == 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return True


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def test_exchange_actual_stdin_and_stdout(monkeypatch):
    script = "import json,sys; request=json.load(sys.stdin); print(json.dumps({'material':{},'result':request}))"
    command(monkeypatch, script)
    request = {'action': 'acquire', 'text': '原文' * 100000}
    assert process.run(request, timeout=3) == {'material': {}, 'result': request}


@pytest.mark.parametrize('timeout', [True, 0, -1, float('nan'), float('inf')])
def test_reject_invalid_budget(timeout):
    with pytest.raises(ValueError):
        process.run({}, timeout=timeout)


@pytest.mark.parametrize('output', ['not json', '{"material":{},"result":NaN}',
                                    '{"material":{},"result":1e999}',
                                    '{"material":{},"result":{},"result":{}}',
                                    '{"material":{},"result":{},"other":{}}'])
def test_reject_malformed_result(monkeypatch, output):
    command(monkeypatch, f'print({output!r})')
    with pytest.raises(process.AcquisitionProcessError) as error:
        process.run({}, timeout=3)
    assert error.value.code == 'invalid_result'


def test_failure_never_exposes_stderr(monkeypatch):
    command(monkeypatch, "import sys; print('secret-token',file=sys.stderr); sys.exit(1)")
    with pytest.raises(process.AcquisitionProcessError) as error:
        process.run({}, timeout=3)
    assert error.value.code == 'failed'
    assert 'secret' not in str(error.value)


def test_output_bound_kills_collector(monkeypatch):
    monkeypatch.setattr(process, 'MAX_BYTES', 1024)
    command(monkeypatch, "import sys,time; sys.stdout.write('x'*4096); sys.stdout.flush(); time.sleep(20)")
    with pytest.raises(process.AcquisitionProcessError) as error:
        process.run({}, timeout=3)
    assert error.value.code == 'result_too_large'


@pytest.mark.parametrize('finish', [False, True])
def test_whole_group_is_stopped_on_timeout_and_success(monkeypatch, tmp_path, finish):
    pids = tmp_path / 'pids'
    script = f"""
import json,os,subprocess,sys,time
child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
open({str(pids)!r},'w').write(str(os.getpid())+' '+str(child.pid))
if {finish!r}:
    print({json.dumps(RESULT)!r})
else:
    time.sleep(60)
"""
    command(monkeypatch, script)
    started = time.monotonic()
    if finish:
        assert process.run({}, timeout=3) == RESULT
    else:
        with pytest.raises(process.AcquisitionProcessError) as error:
            process.run({}, timeout=.3)
        assert error.value.code == 'timeout'
        assert time.monotonic() - started < 2
    parent, child = map(int, pids.read_text().split())
    assert stopped(parent)
    wait_for(lambda: stopped(child))


def guardian_worker_script(pids):
    collector = f"""
import os,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
open({str(pids)!r},'w').write(str(os.getpid())+' '+str(child.pid))
time.sleep(60)
"""
    # Keep the real guardian and liveness pipe; only its fixed collector is replaced.
    guardian = f"""
import sys
from a_share_research import acquisition_process as m
popen=m.subprocess.Popen
m.subprocess.Popen=lambda command,**kwargs: popen([sys.executable,'-c',{collector!r}],**kwargs)
sys.exit(m._guard(int(sys.argv[1])))
"""
    return f"""
import sys
from a_share_research import acquisition_process as m
m._command=lambda fd: [sys.executable,'-c',{guardian!r},str(fd)]
m.run({{}},timeout=30)
"""


@pytest.mark.parametrize('termination', [signal.SIGTERM, signal.SIGKILL])
def test_worker_death_stops_guardian_and_descendants(tmp_path, termination):
    pids = tmp_path / 'pids'
    worker = subprocess.Popen([sys.executable, '-c', guardian_worker_script(pids)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(pids.exists)
        collector, descendant = map(int, pids.read_text().split())
        guardian = int(Path(f'/proc/{collector}/stat').read_text().split()[3])
        worker.send_signal(termination)
        assert worker.wait(timeout=3) == -termination
        wait_for(lambda: all(stopped(pid) for pid in (guardian, collector, descendant)))
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        if pids.exists():
            for pid in map(int, pids.read_text().split()):
                if not stopped(pid):
                    os.kill(pid, signal.SIGKILL)


def test_real_collector_returns_actionable_period_error_without_material():
    from support.reports import MATERIAL
    request = {'action': 'acquire', 'material': MATERIAL.snapshot(), 'tool': 'calculate',
               'arguments': {'label': '期间校验', 'operation': 'difference',
                             'left': {'ref': 'T.revenue', 'period': '2023-12-31'},
                             'right': {'ref': 'T.revenue', 'period': '2025-12-31'}}}
    with pytest.raises(process.AcquisitionProcessError) as error:
        process.run(request, timeout=10)
    assert error.value.code == 'invalid_arguments'
    assert '可用期间：2024-12-31、2025-12-31' in error.value.detail
    assert 'T.revenue 没有期间 2023-12-31' in error.value.detail


@pytest.mark.parametrize('error', [
    {'code': 'provider_error', 'message': 'raw response'},
    {'code': 'invalid_arguments', 'message': 'x' * 1025},
    {'code': 'invalid_arguments', 'message': 'line\nother'},
    {'code': 'invalid_arguments', 'message': 'problem', 'material': {}},
])
def test_nonzero_child_only_accepts_narrow_safe_failure_envelope(monkeypatch, error):
    command(monkeypatch, f'import sys; print({json.dumps({"error": error})!r}); sys.exit(1)')
    with pytest.raises(process.AcquisitionProcessError) as raised:
        process.run({}, timeout=3)
    assert raised.value.code == 'failed' and raised.value.detail is None
