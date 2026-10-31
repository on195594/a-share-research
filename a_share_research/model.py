"""Model completions and native tool messages through the pi runtime."""

import atexit
import hashlib
import http.client
import json
import logging
import os
import platform
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import cache
from importlib.metadata import version
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).with_name("pi_infer.mjs")
logger = logging.getLogger(__name__)
_daemon_failed_pid = None


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DaemonManager:
    def __init__(self):
        self.pid = os.getpid()
        self.process = None
        self.directory = None
        self.output = None
        self.stderr = None
        self.stderr_tail = ""
        self.socket_path = None
        self.fingerprint = None
        self.lock = threading.Lock()
        atexit.register(self.close)

    def close(self):
        # A forked worker must never terminate its parent's daemon.
        if self.pid != os.getpid():
            return
        try:
            if self.process is not None:
                if self.process.stdin is not None:
                    self.process.stdin.close()
                if self.process.poll() is None:
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                if self.stderr is not None:
                    size = os.fstat(self.stderr.fileno()).st_size
                    self.stderr_tail = os.pread(self.stderr.fileno(), 2048, max(0, size - 2048)).decode(errors="replace")
                if self.process.returncode:
                    logger.warning("Pi daemon exited %s; stderr tail: %s",
                                   self.process.returncode, self.stderr_tail)
        finally:
            try:
                if self.output is not None:
                    self.output.close()
                if self.stderr is not None:
                    self.stderr.close()
                if self.directory is not None:
                    Path(self.socket_path).unlink(missing_ok=True)
                    Path(self.directory).rmdir()
            except OSError:
                logger.exception("Pi daemon cleanup failed: %s", self.directory)
            finally:
                self.process = self.output = self.stderr = None
                self.directory = self.socket_path = self.fingerprint = None

    def _fingerprint(self):
        agent = os.environ.get("PI_AGENT_DIR")
        if not agent:
            raise ValueError("Set PI_AGENT_DIR to the existing pi agent directory")
        try:
            release = (Path(agent) / "install/current-version").read_bytes()
        except FileNotFoundError:
            if not os.environ.get("PI_RUNTIME"):
                raise
            release = None
        try:
            store_mtime = (Path(agent) / "models-store.json").stat().st_mtime_ns
        except FileNotFoundError:
            store_mtime = None
        return (agent, os.environ.get("PI_RUNTIME"), release, store_mtime,
                hashlib.sha256(SCRIPT.read_bytes()).hexdigest())

    def start(self):
        global _daemon_failed_pid
        try:
            self._start()
        except (OSError, ValueError):
            _daemon_failed_pid = os.getpid()
            self.close()
            raise

    def _start(self):
        self.close()
        self.fingerprint = self._fingerprint()
        self.directory = tempfile.mkdtemp(prefix=f"pi_infer_{self.pid}_", dir="/tmp")
        self.socket_path = str(Path(self.directory) / "infer.sock")
        # A file keeps runtime diagnostics without an unread pipe blocking Node.
        self.output = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()
        self.stderr_tail = ""
        self.process = subprocess.Popen(
            ["node", str(SCRIPT), "--serve", self.socket_path],
            stdin=subprocess.PIPE, stdout=self.output, stderr=self.stderr,
        )
        deadline = time.monotonic() + 30
        offset, pending = 0, b""
        while time.monotonic() < deadline:
            chunk = os.pread(self.output.fileno(), 65536, offset)
            offset += len(chunk)
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    out = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(out, dict):
                    if out.get("ready") and out.get("socket") == self.socket_path:
                        return
                    if out.get("error"):
                        raise OSError(out["error"])
            if self.process.poll() is not None:
                raise OSError(f"pi daemon exited {self.process.returncode}")
            time.sleep(0.01)
        raise TimeoutError("pi daemon readiness timed out")

    def complete(self, request: dict) -> dict:
        with self.lock:
            if os.environ.get("PI_DAEMON") == "0" or _daemon_failed_pid == os.getpid():
                self.close()
                self.last_transport = "oneshot"
                return _complete_once(request)
            started = time.monotonic()
            sending = False
            try:
                if (self.process is None or self.process.poll() is not None
                        or self.fingerprint != self._fingerprint()):
                    self.start()
                body = json.dumps(request).encode()
                connection = UnixHTTPConnection(self.socket_path, request["timeout"] + 30)
                try:
                    connection.connect()
                    # Even a failed send may have reached Node; never replay it.
                    sending = True
                    connection.request("POST", "/complete", body,
                                       {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    out = json.loads(response.read())
                    if not isinstance(out, dict) or (response.status != 200 and not out.get("error")):
                        raise ValueError(f"invalid pi daemon response (HTTP {response.status})")
                    self.last_transport = "daemon"
                    return out
                finally:
                    connection.close()
            except (OSError, http.client.HTTPException, ValueError) as exc:
                self.close()
                if sending:
                    raise ModelError(f"Pi daemon request failed; not retried: {exc}",
                                     {"requested": {k: request.get(k) for k in ("provider", "model")}},
                                     time.monotonic() - started) from exc
                logger.warning("Pi daemon unavailable; falling back to one-shot completion: %s", exc)
                self.last_transport = "oneshot"
                return _complete_once(request)


@cache
def _daemon(pid: int) -> DaemonManager:
    return DaemonManager()


def _complete_once(request: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as file:
        json.dump(request, file, ensure_ascii=False)
        file.flush()
        proc = subprocess.run(
            ["node", str(SCRIPT), file.name],
            capture_output=True, text=True, timeout=request["timeout"] + 30,
        )
    diagnostic = " ".join(proc.stderr.strip().splitlines()[-1:])
    if not proc.stdout.strip():
        raise ValueError(f"pi exited {proc.returncode} without a response: {diagnostic}")
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid pi response: {exc}; {diagnostic}") from exc
    if proc.returncode and not out.get("error"):
        out["error"] = f"pi exited {proc.returncode}"
    return out


class ModelError(Exception):
    def __init__(self, message: str, identity: dict, seconds: float):
        super().__init__(message)
        self.identity, self.seconds = identity, seconds


@dataclass
class Completion:
    text: str
    usage: dict[str, Any] | None
    seconds: float
    identity: dict
    message: dict


@cache
def application_identity() -> dict:
    """Identify the application files at process startup, including images without Git."""
    package = SCRIPT.parent
    files = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(package.rglob("*"))
             if path.is_file() and path.suffix in (".py", ".mjs", ".html", ".css")}
    return {
        "application_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "a_stock_lib_version": version("a-stock-lib"),
        "python_version": platform.python_version(),
    }


application_identity()  # Imported Python code does not follow later worktree edits.


def preflight() -> dict:
    """Resolve the installed runtime and configured model offline, without a completion."""
    try:
        proc = subprocess.run(["node", str(SCRIPT), "--check"],
                              capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise RuntimeError("Pi 启动预检失败：找不到 Node；请恢复 worker 的 Node 运行时及 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Pi 启动预检超时：请检查运行时目录与模型存储是否可读；未领取任务") from exc
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        diagnostic = " ".join(proc.stderr.strip().splitlines()[-1:])
        raise RuntimeError(f"Pi 启动预检无有效响应（exit {proc.returncode}）：请检查 Node 版本及 pi_infer.mjs 是否完整；{diagnostic}") from exc
    if proc.returncode or not result.get("ready"):
        raise RuntimeError(f"Pi 启动预检失败：{result.get('error') or '运行时未就绪'}")
    return result


def complete(prompt: str, *, timeout: int = 240, max_tokens: int = 8000,
             messages: list | None = None, tools: list | None = None) -> Completion:
    request = {
        "provider": os.environ.get("MODEL_PROVIDER"),
        "model": os.environ.get("MODEL_ID"),
        "prompt": prompt,
        "timeout": timeout,
        "max_tokens": max_tokens,
        # Calls sharing a long stable prefix share the provider prompt cache.
        "session_id": hashlib.sha256(prompt[:4096].encode()).hexdigest()[:32],
    }
    if messages is not None:
        request |= {"messages": messages, "tools": tools or []}
    identity = {
        "requested": {k: request[k] for k in ("provider", "model")},
        "actual": None, "pi_version": None, "parameters": None,
        "node_version": None, "model_source": None, "extension": None,
        "pi_infer_sha256": None,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "request_parameters": {k: request[k] for k in ("timeout", "max_tokens", "session_id")},
    }
    started = time.monotonic()
    try:
        identity["pi_infer_sha256"] = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
        if not request["provider"] or not request["model"]:
            raise ValueError("MODEL_PROVIDER and MODEL_ID are required")
        daemon = _daemon(os.getpid())
        out = daemon.complete(request)
        identity["request_parameters"]["transport"] = getattr(daemon, "last_transport", "daemon")
        identity.update({k: out.get(k) for k in
                         ("pi_version", "parameters", "node_version", "model_source", "extension")})
        if out.get("provider") or out.get("model"):
            identity["actual"] = {k: out.get(k) for k in ("provider", "model")}
        if out.get("error"):
            raise ValueError(out["error"])
        valid_tools = bool(tools) and out["stop_reason"] == "toolUse" and out["tool_calls"] > 0
        if not valid_tools and (out["stop_reason"] != "stop" or out["tool_calls"] or not out["text"].strip()):
            raise ValueError(f"incomplete model output: stop_reason={out['stop_reason']}")
        if (out["provider"], out["model"]) != (request["provider"], request["model"]):
            raise ValueError("model identity mismatch")
    except Exception as exc:
        daemon = _daemon(os.getpid())
        identity["request_parameters"]["transport"] = getattr(daemon, "last_transport",
                                                              "oneshot" if os.environ.get("PI_DAEMON") == "0" or _daemon_failed_pid == os.getpid() else "daemon")
        raise ModelError(str(exc), identity, time.monotonic() - started) from exc
    return Completion(out["text"], out["usage"],
                      time.monotonic() - started, identity, out["message"])
