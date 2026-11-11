"""Scoring process/notification contracts; empty payloads are structural, not score evidence."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from test_ops_hermes import load


@pytest.fixture
def scoring(tmp_path, monkeypatch):
    module = load('a-share-scoring')
    monkeypatch.setattr(module, 'STATE', tmp_path / 'state.json')
    return module


def supervisor(scoring, tmp_path, child, *, seconds=5, extra=()):
    return [sys.executable, '-c', scoring.SUPERVISOR, str(time.time() + seconds),
            sys.executable, '-c', child, scoring.OUTPUT_ARGUMENT, *map(str, extra)]


def environment(tmp_path):
    return os.environ | {'DATA_DIR': str(tmp_path / 'persistent-cache')}


def test_supervisor_returns_only_successful_json_and_preserves_cache(scoring, tmp_path):
    child = '''import json,os,sys
from pathlib import Path
output=Path(sys.argv[1])
cache=Path(os.environ['DATA_DIR'])
(cache/'retained').write_text('cached evidence')
print('private scorer stdout')
print('private scorer stderr',file=sys.stderr)
output.write_text(json.dumps({'structural':True,'temporary':str(output)}))
'''
    result = subprocess.run(supervisor(scoring, tmp_path, child), env=environment(tmp_path),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stderr == ''
    value = json.loads(result.stdout)
    assert value['structural'] is True
    assert not Path(value['temporary']).parent.exists()
    assert (tmp_path / 'persistent-cache/retained').read_text() == 'cached evidence'
    assert 'private scorer' not in result.stdout


@pytest.mark.parametrize('child', [
    "import sys; print('private failure',file=sys.stderr); sys.exit(7)",
    "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('broken json')",
    "pass",
])
def test_failed_or_missing_output_never_reads_an_old_result(scoring, tmp_path, child):
    result = subprocess.run(supervisor(scoring, tmp_path, child), env=environment(tmp_path),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and result.stdout == '' and result.stderr == ''


def test_late_start_does_not_spawn_work(scoring, tmp_path):
    marker = tmp_path / 'must-not-exist'
    child = 'from pathlib import Path; import sys; Path(sys.argv[2]).touch()'
    result = subprocess.run(supervisor(scoring, tmp_path, child, seconds=-1, extra=(marker,)),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 124 and not marker.exists()


def test_signal_before_popen_returns_still_cleans_up_spawned_child(scoring, tmp_path):
    pid_file = tmp_path / 'spawned-pid'
    # Inject exactly the constructor boundary; no timing-sensitive repetition is needed.
    injection = f'''real_popen = subprocess.Popen
def signal_before_return(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    Path({str(pid_file)!r}).write_text(str(process.pid))
    os.kill(os.getpid(), signal.SIGTERM)
    return process
subprocess.Popen = signal_before_return
'''
    source = scoring.SUPERVISOR.replace('deadline = float(sys.argv[1])', injection + '\ndeadline = float(sys.argv[1])', 1)
    command = supervisor(scoring, tmp_path, 'import time; time.sleep(60)', seconds=30)
    command[2] = source
    result = subprocess.run(command, env=environment(tmp_path), capture_output=True, text=True, timeout=10)
    assert result.returncode == 124 and result.stdout == result.stderr == ''
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


FORKING_CHILD = '''import json,os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
Path(sys.argv[2]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(60)
'''


@pytest.mark.parametrize('cancel', [False, True])
def test_deadline_or_signal_kills_and_reaps_scoring_descendants(scoring, tmp_path, cancel):
    pids = tmp_path / 'pids.json'
    process = subprocess.Popen(supervisor(scoring, tmp_path, FORKING_CHILD, seconds=60 if cancel else 1,
                                         extra=(pids,)), env=environment(tmp_path),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        end = time.monotonic() + 5
        while not pids.exists() and process.poll() is None and time.monotonic() < end:
            time.sleep(.01)
        assert pids.exists()
        if cancel:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 124 and stdout == stderr == ''
        for pid in json.loads(pids.read_text()):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def payloads():
    return {
        'V0': {'rows': [], 'summary': {'verdict': []}},
        'V1': {'summary': {'scored': 0, 'compared': 0, 'by_confidence': {}, 'beat_baseline': None}},
        'H1-H3': {'summary': {'h1': [], 'h3': []}},
        'A3': {'summary': []},
    }


def test_two_cohorts_share_deadline_and_all_five_notification_keys_survive(scoring, monkeypatch, capsys):
    lock = threading.Lock()
    active, maximum, observed = 0, 0, []
    def score(name, module, flags, wall_deadline, monotonic_deadline):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            observed.append((name, wall_deadline, monotonic_deadline))
        time.sleep(.03)
        with lock:
            active -= 1
        return payloads()[name]
    monkeypatch.setattr(scoring, 'score', score)
    scoring.main()
    assert maximum == 2 and len(observed) == 4
    assert len({row[1:] for row in observed}) == 1
    assert {row[0] for row in observed} == set(payloads())
    state = json.loads(scoring.STATE.read_text())
    assert set(state) == {'V0', 'V1', 'H1', 'H3', 'A3'}
    assert '判断记分周报' in capsys.readouterr().out
    before = scoring.STATE.read_bytes(), scoring.STATE.stat().st_mtime_ns
    scoring.main()
    assert capsys.readouterr().out == ''
    assert (scoring.STATE.read_bytes(), scoring.STATE.stat().st_mtime_ns) == before


@pytest.mark.parametrize('failure', ['timeout', 'command', 'invalid'])
def test_failure_keeps_previous_state(scoring, monkeypatch, capsys, failure):
    original = b'{"previous":"untouched structural state"}\n'
    scoring.STATE.write_bytes(original)
    def score(name, *args):
        if name == 'V1':
            if failure == 'timeout':
                raise subprocess.TimeoutExpired('private command', 540, output='private output')
            if failure == 'command':
                raise subprocess.CalledProcessError(7, 'private command', stderr='private secret')
            return {'malformed': True}
        return payloads()[name]
    monkeypatch.setattr(scoring, 'score', score)
    scoring.main()
    output = capsys.readouterr().out
    assert scoring.STATE.read_bytes() == original
    assert '任务未完成' in output and 'private' not in output
    if failure == 'timeout':
        assert '540 秒' in output


def test_held_state_lock_is_silent_and_does_no_work(scoring, monkeypatch, capsys):
    monkeypatch.setattr(scoring, 'run', lambda: pytest.fail('lock holder already owns the work'))
    with scoring.STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scoring.main()
    assert capsys.readouterr().out == '' and not scoring.STATE.exists()


def test_local_budget_exhaustion_never_launches_docker(scoring, monkeypatch):
    monkeypatch.setattr(scoring.subprocess, 'run', lambda *a, **k: pytest.fail('late docker invocation'))
    with pytest.raises(subprocess.TimeoutExpired):
        scoring.score('V0', 'validation', [], time.time() - 1, time.monotonic() - 1)


def test_docker_wrapper_preserves_stage_cache_and_cleanup_margin(scoring, monkeypatch):
    def run(command, **kwargs):
        assert command[:6] == ['docker', 'exec', '-e', 'DATA_DIR=/tmp/a-share-scoring/V0',
                               'a-share-research-worker', 'python']
        assert command[7] == scoring.SUPERVISOR and command[-1] == scoring.OUTPUT_ARGUMENT
        assert 19 < kwargs['timeout'] <= 20
        return subprocess.CompletedProcess(command, 0, '{"structural":true}', '')
    monkeypatch.setattr(scoring.subprocess, 'run', run)
    assert scoring.score('V0', 'validation', [], time.time() + 10, time.monotonic() + 10) == {'structural': True}
