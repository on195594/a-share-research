"""Model completions and native tool messages through the pi runtime."""

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import cache
from importlib.metadata import version
from pathlib import Path
from typing import Any

from a_share_research.contract import JUDGMENT_CONTRACT, SURFACE_SHA256

SCRIPT = Path(__file__).with_name("pi_infer.mjs")


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


def configured_model(role: str | None = None) -> tuple[str | None, str | None, str]:
    """Provider, model and reasoning for a call role.

    The review stage may use a second model (MODEL_REVIEW_*) so that the reviewer's errors are less
    correlated with the author's; every other role, and review without that setting, uses MODEL_*.
    """
    reasoning = os.environ.get("MODEL_REASONING", "high")
    if role == "review" and (os.environ.get("MODEL_REVIEW_PROVIDER") or os.environ.get("MODEL_REVIEW_ID")):
        return (os.environ.get("MODEL_REVIEW_PROVIDER"), os.environ.get("MODEL_REVIEW_ID"),
                os.environ.get("MODEL_REVIEW_REASONING", reasoning))
    return os.environ.get("MODEL_PROVIDER"), os.environ.get("MODEL_ID"), reasoning


def judgment_identity() -> dict:
    """Record the current frozen judgment contract and configured model roles."""
    keys = ("provider", "model", "reasoning")
    return {"contract": JUDGMENT_CONTRACT, "surface_sha256": SURFACE_SHA256,
            "author": dict(zip(keys, configured_model())),
            "reviewer": dict(zip(keys, configured_model("review")))}


def _check(env: dict) -> dict:
    try:
        proc = subprocess.run(["node", str(SCRIPT), "--check"], env=env,
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


def preflight() -> dict:
    """Resolve the installed runtime and configured model(s) offline, without a completion."""
    result = _check(dict(os.environ))
    if os.environ.get("MODEL_REVIEW_PROVIDER") or os.environ.get("MODEL_REVIEW_ID"):
        provider, model_id, reasoning = configured_model("review")
        if not provider or not model_id:
            raise RuntimeError("Pi 启动预检失败：复核模型须同时设置 MODEL_REVIEW_PROVIDER 与 MODEL_REVIEW_ID")
        review = _check(os.environ | {"MODEL_PROVIDER": provider, "MODEL_ID": model_id, "MODEL_REASONING": reasoning})
        result["review"] = {k: review.get(k) for k in ("provider", "model", "reasoning")}
    return result


def complete(prompt: str, *, timeout: int | None = None, max_tokens: int | None = None,
             reasoning: str | None = None, messages: list | None = None,
             tools: list | None = None, role: str | None = None) -> Completion:
    env_max_tokens = os.environ.get("MODEL_MAX_TOKENS")
    resolved_max_tokens = int(env_max_tokens) if env_max_tokens is not None else (max_tokens if max_tokens is not None else 16000)
    resolved_timeout = timeout if timeout is not None else int(os.environ.get("MODEL_TIMEOUT", "240"))
    provider, model_id, role_reasoning = configured_model(role)
    resolved_reasoning = reasoning if reasoning is not None else role_reasoning
    request = {
        "provider": provider,
        "model": model_id,
        "reasoning": resolved_reasoning,
        "prompt": prompt,
        "timeout": resolved_timeout,
        "max_tokens": resolved_max_tokens,
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
        "request_parameters": {k: request[k] for k in ("timeout", "max_tokens", "reasoning", "session_id")},
    }
    started = time.monotonic()
    try:
        identity["pi_infer_sha256"] = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
        if not request["provider"] or not request["model"]:
            raise ValueError("MODEL_PROVIDER and MODEL_ID (MODEL_REVIEW_* for review) are required")
        out = _complete_once(request)
        identity.update({k: out.get(k) for k in
                         ("pi_version", "parameters", "node_version", "model_source", "extension")})
        if out.get("provider") or out.get("model"):
            identity["actual"] = {k: out.get(k) for k in ("provider", "model")}
        if out.get("error"):
            raise ValueError(out["error"])
        # In a tool loop, calls are a complete turn even when none were offered (Gemini imitates the
        # history after the loop stops); the caller decides how to answer them.
        valid_tools = messages is not None and out["stop_reason"] == "toolUse" and out["tool_calls"] > 0
        if not valid_tools and (out["stop_reason"] != "stop" or out["tool_calls"] or not out["text"].strip()):
            raise ValueError(f"incomplete model output: stop_reason={out['stop_reason']}")
        if (out["provider"], out["model"]) != (request["provider"], request["model"]):
            raise ValueError("model identity mismatch")
    except Exception as exc:
        raise ModelError(str(exc), identity, time.monotonic() - started) from exc
    return Completion(out["text"], out["usage"],
                      time.monotonic() - started, identity, out["message"])
