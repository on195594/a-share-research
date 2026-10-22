"""One no-tool model completion through the pi runtime."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).with_name("pi_infer.mjs")


class ModelError(Exception):
    def __init__(self, message: str, identity: dict, seconds: float):
        super().__init__(message)
        self.identity, self.seconds = identity, seconds


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    usage: dict[str, Any] | None
    seconds: float
    identity: dict


def application_identity() -> dict:
    """Hash the shipped application, including dirty checkouts and images without Git."""
    package = SCRIPT.parent
    files = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(package.rglob("*"))
             if path.is_file() and path.suffix in (".py", ".mjs", ".html", ".css")}
    return {
        "application_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "a_stock_lib_version": version("a-stock-lib"),
        "python_version": platform.python_version(),
    }


def complete(prompt: str, *, timeout: int = 240, max_tokens: int = 8000) -> Completion:
    request = {
        "provider": os.environ.get("MODEL_PROVIDER"),
        "model": os.environ.get("MODEL_ID"),
        "prompt": prompt,
        "timeout": timeout,
        "max_tokens": max_tokens,
        # Calls sharing a long stable prefix share the provider prompt cache.
        "session_id": hashlib.sha256(prompt[:4096].encode()).hexdigest()[:32],
    }
    identity = {
        "requested": {k: request[k] for k in ("provider", "model")},
        "actual": None, "pi_version": None, "parameters": None,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "request_parameters": {k: request[k] for k in ("timeout", "max_tokens", "session_id")},
    }
    started = time.monotonic()
    try:
        if not request["provider"] or not request["model"]:
            raise ValueError("MODEL_PROVIDER and MODEL_ID are required")
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as file:
            json.dump(request, file, ensure_ascii=False)
            file.flush()
            proc = subprocess.run(
                ["node", str(SCRIPT), file.name],
                capture_output=True, text=True, timeout=timeout + 30,
            )
        if not proc.stdout.strip():
            raise ValueError(f"pi exited {proc.returncode} without a response")
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        identity.update({k: out.get(k) for k in ("pi_version", "parameters")})
        if out.get("provider") or out.get("model"):
            identity["actual"] = {k: out.get(k) for k in ("provider", "model")}
        if proc.returncode != 0 or out.get("error"):
            raise ValueError(out.get("error") or f"pi exited {proc.returncode}")
        if out["stop_reason"] != "stop" or out["tool_calls"] or not out["text"].strip():
            raise ValueError(f"incomplete model output: stop_reason={out['stop_reason']}")
        if (out["provider"], out["model"]) != (request["provider"], request["model"]):
            raise ValueError("model identity mismatch")
    except Exception as exc:
        raise ModelError(str(exc), identity, time.monotonic() - started) from exc
    return Completion(out["text"], out["provider"], out["model"], out["usage"],
                      time.monotonic() - started, identity)
