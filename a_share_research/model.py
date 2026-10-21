"""One no-tool model completion through the pi runtime."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).with_name("pi_infer.mjs")


class ModelError(Exception):
    pass


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    usage: dict[str, Any] | None
    seconds: float


def complete(prompt: str, *, timeout: int = 240, max_tokens: int = 8000) -> Completion:
    request = {
        "provider": os.environ["MODEL_PROVIDER"],
        "model": os.environ["MODEL_ID"],
        "prompt": prompt,
        "timeout": timeout,
        "max_tokens": max_tokens,
        # Calls sharing a long stable prefix share the provider prompt cache.
        "session_id": hashlib.sha256(prompt[:4096].encode()).hexdigest()[:32],
    }
    started = time.monotonic()
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as file:
        json.dump(request, file, ensure_ascii=False)
        file.flush()
        proc = subprocess.run(
            ["node", str(SCRIPT), file.name],
            capture_output=True, text=True, timeout=timeout + 30,
        )
    seconds = time.monotonic() - started
    if proc.returncode != 0:
        # stderr may carry provider URLs; keep only the last line.
        raise ModelError(f"pi exited {proc.returncode}: {proc.stderr.strip().splitlines()[-1:]}")
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if out["stop_reason"] != "stop" or out["tool_calls"] or not out["text"].strip():
        raise ModelError(f"incomplete model output: stop_reason={out['stop_reason']}")
    if (out["provider"], out["model"]) != (request["provider"], request["model"]):
        raise ModelError("model identity mismatch")
    return Completion(out["text"], out["provider"], out["model"], out["usage"], seconds)
