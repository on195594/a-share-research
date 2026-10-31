"""Real Node/Pi processes and Unix sockets; live completions are explicitly opt-in."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

import pytest

from a_share_research import model


@pytest.fixture
def configured():
    if not all(os.environ.get(key) for key in ("MODEL_PROVIDER", "MODEL_ID", "PI_AGENT_DIR")):
        pytest.skip("Run with the existing worker environment for real Pi daemon checks")
    return dict(os.environ)


@pytest.fixture
def payload(configured):
    chain = json.loads(gzip.decompress(
        (Path(__file__).parent / "data/explore-call-chain.json.gz").read_bytes()))
    prompt = chain["calls"][0]["prompt"]
    return {
        "provider": os.environ["MODEL_PROVIDER"], "model": os.environ["MODEL_ID"],
        "prompt": prompt, **json.loads(prompt), "timeout": -1, "max_tokens": 8000,
        "session_id": hashlib.sha256(prompt[:4096].encode()).hexdigest()[:32],
    }


@pytest.fixture
def daemon(configured):
    manager = model.DaemonManager()
    try:
        yield manager
    finally:
        manager.close()


def post(manager, body, path="/complete"):
    connection = model.UnixHTTPConnection(manager.socket_path, 30)
    try:
        connection.request("POST", path, body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.getheader("Content-Type") == "application/json"
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_lazy_start_ready_handshake_and_private_socket(daemon, payload):
    assert daemon.process is None and daemon.socket_path is None
    assert "RangeError" in daemon.complete(payload)["error"]
    lines = os.pread(daemon.output.fileno(), 65536, 0).decode().splitlines()
    assert json.loads(lines[-1]) == {"ready": True, "socket": daemon.socket_path}
    assert Path(daemon.socket_path).is_socket()
    assert Path(daemon.directory).stat().st_mode & 0o777 == 0o700
    assert Path(daemon.socket_path).stat().st_mode & 0o777 == 0o600


def test_request_errors_are_isolated_and_match_one_shot(daemon, payload, caplog):
    daemon.start()
    process = daemon.process
    status, out = post(daemon, b"{")
    assert status == 400 and "SyntaxError" in out["error"]
    expected = model._complete_once(payload)
    for path in ("/", "/complete"):
        status, out = post(daemon, json.dumps(payload).encode(), path)
        assert status == 500 and out == expected
    assert daemon.complete(payload) == expected
    assert daemon.process is process and process.poll() is None
    assert "falling back" not in caplog.text


def test_crashed_daemon_restarts_on_next_request(daemon, payload):
    daemon.start()
    process, path = daemon.process, Path(daemon.socket_path)
    process.kill()
    process.wait(timeout=10)
    assert "RangeError" in daemon.complete(payload)["error"]
    assert daemon.process.pid != process.pid and daemon.process.poll() is None
    assert not path.exists()


def test_broken_socket_falls_back_then_recovers(daemon, payload, caplog):
    daemon.start()
    process, directory = daemon.process, Path(daemon.directory)
    Path(daemon.socket_path).unlink()
    assert daemon.complete(payload) == model._complete_once(payload)
    assert "falling back to one-shot" in caplog.text
    assert daemon.process is None and process.poll() == 0
    assert not directory.exists()
    assert "RangeError" in daemon.complete(payload)["error"]
    assert daemon.process.pid != process.pid


@pytest.mark.parametrize("failure", ["send_timeout", "timeout", "crash"])
def test_sent_request_is_never_replayed(daemon, payload, failure):
    request = {key: payload[key] for key in ("provider", "model", "timeout", "max_tokens", "session_id")}
    request["prompt"] = payload["prompt"][:128]
    if failure == "send_timeout":
        request = payload
    daemon.start()
    process = daemon.process
    process.send_signal(signal.SIGSTOP)
    os.waitpid(process.pid, os.WUNTRACED)
    calls = {"requests": 0, "reads": 0, "fallbacks": 0}

    # Observe real calls; inject OS faults only after HTTPConnection.request returns.
    def observe(frame, event, arg):
        if event == "call" and frame.f_code is model._complete_once.__code__:
            calls["fallbacks"] += 1
        if event == "call" and frame.f_code is model.UnixHTTPConnection.getresponse.__code__:
            calls["reads"] += 1
        if (event == "call" and frame.f_code is model.UnixHTTPConnection.request.__code__
                and failure == "send_timeout"):
            frame.f_locals["self"].sock.settimeout(0.05)
        if event == "return" and frame.f_code is model.UnixHTTPConnection.request.__code__:
            calls["requests"] += 1
            if failure == "crash":
                process.kill()
                process.wait(timeout=10)
            else:
                frame.f_locals["self"].sock.settimeout(0.05)

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        with pytest.raises(model.ModelError, match="not retried") as caught:
            daemon.complete(request)
    finally:
        sys.setprofile(previous)
    assert isinstance(caught.value.__cause__, (OSError, model.http.client.HTTPException))
    assert calls == {"requests": 1, "reads": int(failure != "send_timeout"), "fallbacks": 0}
    assert daemon.process is None and process.poll() is not None


def test_stdin_eof_exits_daemon(daemon):
    daemon.start()
    path = Path(daemon.socket_path)
    daemon.process.stdin.close()
    assert daemon.process.wait(timeout=10) == 0
    assert not path.exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_closes_daemon_and_removes_socket(daemon, sig):
    daemon.start()
    path = Path(daemon.socket_path)
    daemon.process.send_signal(sig)
    assert daemon.process.wait(timeout=10) == 0
    assert not path.exists()


def test_close_is_idempotent(daemon):
    daemon.start()
    process, directory = daemon.process, Path(daemon.directory)
    daemon.close()
    daemon.close()
    assert process.returncode == 0 and not directory.exists()


def test_cleanup_failure_clears_state_and_releases_lock(daemon, payload, caplog):
    daemon.start()
    directory = Path(daemon.directory)
    # A real unexpected directory entry makes rmdir fail.
    leftover = directory / "stderr-copy"
    leftover.touch()
    Path(daemon.socket_path).unlink()
    try:
        assert "RangeError" in daemon.complete(payload)["error"]
        assert "cleanup failed" in caplog.text
        assert daemon.process is daemon.directory is daemon.socket_path is None
        assert daemon.output is daemon.stderr is daemon.fingerprint is None
        assert daemon.lock.acquire(blocking=False)
        daemon.lock.release()
        assert "RangeError" in daemon.complete(payload)["error"]
    finally:
        leftover.unlink()
        directory.rmdir()


def test_startup_failure_retains_and_logs_stderr_tail(tmp_path):
    script = """
from a_share_research.model import DaemonManager
daemon = DaemonManager()
try:
    daemon.start()
except OSError:
    assert len(daemon.stderr_tail.encode()) == 2048
    assert daemon.process is daemon.directory is None
    print(daemon.stderr_tail, end='')
else:
    raise AssertionError('Invalid Node option should fail')
"""
    process = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                             env=os.environ | {"PI_AGENT_DIR": str(tmp_path),
                                               "PI_RUNTIME": str(tmp_path),
                                               "NODE_OPTIONS": "--not-a-node-option-" + "x" * 2100},
                             timeout=40, check=True)
    assert len(process.stdout.encode()) == 2048
    assert process.stdout.strip() in process.stderr and "stderr tail:" in process.stderr


def test_python_exit_reaps_child_and_socket(configured):
    script = """
import json
from a_share_research.model import DaemonManager
daemon = DaemonManager()
daemon.start()
print(json.dumps({'pid': daemon.process.pid, 'socket': daemon.socket_path}))
"""
    process = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, timeout=40, check=True)
    out = json.loads(process.stdout)
    assert not Path(out["socket"]).parent.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(out["pid"], 0)


@pytest.mark.parametrize("exit_mode", ["kill", "crash"])
def test_parent_death_closes_stdin_and_exits_daemon(configured, exit_mode):
    script = """
import json, os, sys
from a_share_research.model import DaemonManager
daemon = DaemonManager()
daemon.start()
print(json.dumps({'pid': daemon.process.pid, 'socket': daemon.socket_path}), flush=True)
sys.stdin.read(1)
os._exit(1)
"""
    process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = None
    try:
        out = json.loads(process.stdout.readline())
        if exit_mode == "kill":
            process.kill()
        else:
            process.stdin.write("x")
            process.stdin.flush()
        process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while True:
            stat = Path(f"/proc/{out['pid']}/stat")
            try:
                state = stat.read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            assert time.monotonic() < deadline, "Orphan daemon survived parent death"
            time.sleep(0.01)
        assert not Path(out["socket"]).exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        if out:
            try:
                os.kill(out["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            Path(out["socket"]).unlink(missing_ok=True)
            Path(out["socket"]).parent.rmdir()


def test_fork_uses_own_daemon_and_preserves_parent(configured):
    script = """
import os, sys
from pathlib import Path
from a_share_research.model import _daemon
parent = _daemon(os.getpid())
parent.start()
child = os.fork()
if child == 0:
    own = _daemon(os.getpid())
    own.start()
    assert own.process.pid != parent.process.pid
    assert own.socket_path != parent.socket_path
    print(own.socket_path, flush=True)
    sys.exit(0)
_, status = os.waitpid(child, 0)
assert status == 0
assert parent.process.poll() is None and Path(parent.socket_path).is_socket()
"""
    process = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, timeout=40, check=True)
    assert not Path(process.stdout.strip()).parent.exists()


def test_startup_failure_falls_back_and_preserves_model_error(configured, tmp_path):
    script = """
import json, os
from a_share_research.model import complete, ModelError, _daemon
try:
    complete('启动检查')
except ModelError as error:
    assert 'PI_RUNTIME' in str(error)
    assert error.seconds > 0
    assert error.identity['requested']['model'] == os.environ['MODEL_ID']
    assert error.identity['node_version'] and error.identity['pi_infer_sha256']
    assert _daemon(os.getpid()).process is None
    print(json.dumps(error.identity['requested']))
else:
    raise AssertionError('Expected ModelError')
"""
    process = subprocess.run([sys.executable, "-c", script],
                             env=os.environ | {"PI_RUNTIME": str(tmp_path / "missing")},
                             capture_output=True, text=True, timeout=40, check=True)
    assert "falling back to one-shot" in process.stderr
    assert json.loads(process.stdout)["model"] == os.environ["MODEL_ID"]


@pytest.mark.parametrize("change", ["version", "store", "script"])
def test_runtime_drift_restarts_before_request(configured, payload, tmp_path, change):
    agent = Path(configured["PI_AGENT_DIR"])
    release = (agent / "install/current-version").read_text().strip()
    install = tmp_path / "install"
    releases = install / "releases"
    releases.mkdir(parents=True)
    (install / "current-version").write_text(release)
    for name in (release, release + "-updated"):
        (releases / name).symlink_to(agent / "install/releases" / release, target_is_directory=True)
    (tmp_path / "auth.json").symlink_to(agent / "auth.json")
    store = tmp_path / "models-store.json"
    store.touch(mode=0o600)
    shutil.copyfile(agent / "models-store.json", store)
    infer = tmp_path / "pi_infer.mjs"
    shutil.copyfile(model.SCRIPT, infer)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload))
    script = """
import json, os, sys
from pathlib import Path
from a_share_research import model
model.SCRIPT = Path(sys.argv[1])
request = json.loads(Path(sys.argv[2]).read_text())
agent = Path(os.environ['PI_AGENT_DIR'])
daemon = model.DaemonManager()
try:
    assert 'RangeError' in daemon.complete(request)['error']
    process, old_socket = daemon.process, Path(daemon.socket_path)
    fingerprint = daemon.fingerprint
    assert 'RangeError' in daemon.complete(request)['error']
    assert daemon.process is process
    change = sys.argv[3]
    if change == 'version':
        pointer = agent / 'install/current-version'
        pointer.write_text(pointer.read_text().strip() + '-updated')
    elif change == 'store':
        store = agent / 'models-store.json'
        stat = store.stat()
        os.utime(store, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    else:
        with model.SCRIPT.open('a') as file:
            file.write('\\n')
    assert 'RangeError' in daemon.complete(request)['error']
    assert daemon.process.pid != process.pid and process.returncode == 0
    assert not old_socket.exists() and daemon.fingerprint != fingerprint
finally:
    daemon.close()
"""
    subprocess.run([sys.executable, "-c", script, str(infer), str(request), change],
                   env=configured | {"PI_AGENT_DIR": str(tmp_path), "PI_RUNTIME": ""},
                   capture_output=True, text=True, timeout=60, check=True)


def test_startup_failure_opens_process_circuit(configured, payload, tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload))
    script = """
import json, os, sys
from pathlib import Path
from a_share_research import model
request = json.loads(Path(sys.argv[1]).read_text())
runtime = os.environ.get('PI_RUNTIME', '')
calls = {'starts': 0, 'fallbacks': 0}
def observe(frame, event, arg):
    if event == 'call':
        if frame.f_code is model.DaemonManager._start.__code__:
            calls['starts'] += 1
        if frame.f_code is model._complete_once.__code__:
            calls['fallbacks'] += 1
sys.setprofile(observe)
daemon = model.DaemonManager()
try:
    os.environ['PI_RUNTIME'] = sys.argv[2]
    assert 'PI_RUNTIME' in daemon.complete(request)['error']
    assert daemon.process is None
    os.environ['PI_RUNTIME'] = runtime
    assert 'RangeError' in daemon.complete(request)['error']
    assert daemon.process is None
    # The breaker also applies to a new manager in the same Python process.
    assert 'RangeError' in model.DaemonManager().complete(request)['error']
    assert calls == {'starts': 1, 'fallbacks': 3}, calls
finally:
    sys.setprofile(None)
    daemon.close()
"""
    subprocess.run([sys.executable, "-c", script, str(request), str(tmp_path / "missing")],
                   env=configured, capture_output=True, text=True, timeout=60, check=True)


def test_pi_daemon_switch_forces_one_shot(configured, payload, tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload))
    script = """
import json, os, sys
from pathlib import Path
from a_share_research import model
request = json.loads(Path(sys.argv[1]).read_text())
calls = {'starts': 0, 'fallbacks': 0}
def observe(frame, event, arg):
    if event == 'call':
        if frame.f_code is model.DaemonManager._start.__code__:
            calls['starts'] += 1
        if frame.f_code is model._complete_once.__code__:
            calls['fallbacks'] += 1
sys.setprofile(observe)
daemon = model._daemon(os.getpid())
try:
    for enabled in ('0', '1', '0'):
        os.environ['PI_DAEMON'] = enabled
        try:
            model.complete(request['prompt'], timeout=-1)
        except model.ModelError as error:
            assert 'RangeError' in str(error)
        else:
            raise AssertionError('Expected ModelError')
        assert (daemon.process is None) == (enabled == '0')
        if enabled == '1':
            process = daemon.process
    assert process.returncode == 0
    assert calls == {'starts': 1, 'fallbacks': 2}, calls
finally:
    sys.setprofile(None)
    daemon.close()
"""
    subprocess.run([sys.executable, "-c", script, str(request)], env=configured,
                   capture_output=True, text=True, timeout=60, check=True)


def test_model_error_retains_request_identity(payload):
    daemon = model._daemon(os.getpid())
    try:
        with pytest.raises(model.ModelError, match="RangeError") as caught:
            model.complete(payload["prompt"], timeout=-1,
                           messages=payload["messages"], tools=payload["tools"])
        error = caught.value
        assert error.seconds > 0 and error.identity["pi_version"]
        assert error.identity["prompt_sha256"] == hashlib.sha256(payload["prompt"].encode()).hexdigest()
        assert error.identity["request_parameters"]["session_id"] == payload["session_id"]
        assert error.identity["request_parameters"]["timeout"] == -1
        assert daemon.process.poll() is None
    finally:
        daemon.close()


@pytest.mark.skipif(os.environ.get("PI_DAEMON_LIVE_TEST") != "1",
                    reason="Set PI_DAEMON_LIVE_TEST=1 for paid real-provider completions")
def test_live_completion_tools_timeout_and_fallback(payload):
    daemon = model._daemon(os.getpid())
    try:
        native = model.complete(payload["prompt"], messages=payload["messages"],
                                tools=payload["tools"], timeout=120)
        process = daemon.process
        assert native.message["stopReason"] == "toolUse"
        assert any(block["type"] == "toolCall" for block in native.message["content"])
        with pytest.raises(model.ModelError):
            model.complete(payload["prompt"], timeout=0)
        assert daemon.process is process and process.poll() is None
        prompt = "请用一句中文概括以下已采集研究资料：\n" + payload["messages"][0]["content"]
        normal = model.complete(prompt, timeout=120)
        assert daemon.process is process
        Path(daemon.socket_path).unlink()
        fallback = model.complete(prompt, timeout=120)
        assert daemon.process is None and process.poll() == 0
        for completion in (native, normal, fallback):
            assert isinstance(completion, model.Completion) and completion.usage
            assert completion.seconds > 0
            assert completion.identity["actual"] == completion.identity["requested"]
            assert completion.identity["parameters"]["transport"] == "sse"
            assert completion.identity["pi_infer_sha256"] == hashlib.sha256(model.SCRIPT.read_bytes()).hexdigest()
        assert normal.text.strip() and fallback.text.strip()
        assert normal.identity["request_parameters"]["transport"] == "daemon"
        assert fallback.identity["request_parameters"]["transport"] == "oneshot"
        core = lambda id_dict: {k: (v if k != "request_parameters" else {pk: pv for pk, pv in v.items() if pk != "transport"}) for k, v in id_dict.items()}
        assert core(normal.identity) == core(fallback.identity)
    finally:
        daemon.close()
