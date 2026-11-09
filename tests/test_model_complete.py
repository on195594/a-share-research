"""Real Node/Pi one-shot completions; paid live completions are explicitly opt-in."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from a_share_research import model


@pytest.fixture
def configured():
    if not all(os.environ.get(key) for key in ("MODEL_PROVIDER", "MODEL_ID", "PI_AGENT_DIR")):
        pytest.skip("Run with the existing worker environment for real Pi checks")
    return dict(os.environ)


@pytest.fixture
def payload(configured):
    chain = json.loads(gzip.decompress(
        (Path(__file__).parent / "data/explore-call-chain.json.gz").read_bytes()))
    prompt = chain["calls"][0]["prompt"]
    return {
        "provider": os.environ["MODEL_PROVIDER"], "model": os.environ["MODEL_ID"],
        "prompt": prompt, **json.loads(prompt), "timeout": -1, "max_tokens": 16000,
        "session_id": hashlib.sha256(prompt[:4096].encode()).hexdigest()[:32],
    }


def test_request_error_is_returned_with_runtime_identity(payload):
    out = model._complete_once(payload)
    assert "RangeError" in out["error"]
    assert out["pi_version"] and out["node_version"]


def test_model_error_retains_request_identity(payload):
    with pytest.raises(model.ModelError, match="RangeError") as caught:
        model.complete(payload["prompt"], timeout=-1,
                       messages=payload["messages"], tools=payload["tools"])
    error = caught.value
    assert error.seconds > 0 and error.identity["pi_version"]
    assert error.identity["prompt_sha256"] == hashlib.sha256(payload["prompt"].encode()).hexdigest()
    assert error.identity["request_parameters"] == {
        "timeout": -1, "max_tokens": 16000, "reasoning": "high", "session_id": payload["session_id"]}


def test_environment_configures_reasoning_and_max_tokens(payload, monkeypatch):
    monkeypatch.setenv("MODEL_REASONING", "medium")
    monkeypatch.setenv("MODEL_MAX_TOKENS", "12000")
    with pytest.raises(model.ModelError, match="RangeError") as caught:
        model.complete(payload["prompt"], timeout=-1,
                       messages=payload["messages"], tools=payload["tools"])
    error = caught.value
    assert error.identity["request_parameters"]["max_tokens"] == 12000
    assert error.identity["request_parameters"]["reasoning"] == "medium"
    assert error.identity["parameters"]["reasoning"] == "medium"
    assert error.identity["parameters"]["maxTokens"] == 12000


def test_environment_max_tokens_overrides_call_site(payload, monkeypatch):
    monkeypatch.setenv("MODEL_MAX_TOKENS", "14000")
    with pytest.raises(model.ModelError, match="RangeError") as caught:
        model.complete(payload["prompt"], timeout=-1, max_tokens=8000,
                       messages=payload["messages"], tools=payload["tools"])
    error = caught.value
    assert error.identity["request_parameters"]["max_tokens"] == 14000
    assert error.identity["parameters"]["maxTokens"] == 14000


def test_missing_runtime_raises_model_error_with_identity(configured, tmp_path):
    script = """
import json, os
from a_share_research.model import complete, ModelError
try:
    complete('启动检查')
except ModelError as error:
    assert 'PI_RUNTIME' in str(error)
    assert error.seconds > 0
    assert error.identity['node_version'] and error.identity['pi_infer_sha256']
    print(json.dumps(error.identity['requested']))
else:
    raise AssertionError('Expected ModelError')
"""
    process = subprocess.run([sys.executable, "-c", script],
                             env=os.environ | {"PI_RUNTIME": str(tmp_path / "missing")},
                             capture_output=True, text=True, timeout=40, check=True)
    assert json.loads(process.stdout)["model"] == os.environ["MODEL_ID"]


@pytest.mark.skipif(os.environ.get("PI_LIVE_TEST") != "1",
                    reason="Set PI_LIVE_TEST=1 for paid real-provider completions")
def test_live_completion_tools_and_timeout(payload):
    native = model.complete(payload["prompt"], messages=payload["messages"],
                            tools=payload["tools"], timeout=120)
    assert native.message["stopReason"] == "toolUse"
    assert any(block["type"] == "toolCall" for block in native.message["content"])
    with pytest.raises(model.ModelError):
        model.complete(payload["prompt"], timeout=0)
    normal = model.complete("请用一句中文概括以下已采集研究资料：\n" + payload["messages"][0]["content"],
                            timeout=120)
    assert normal.text.strip()
    for completion in (native, normal):
        assert isinstance(completion, model.Completion) and completion.usage
        assert completion.seconds > 0
        assert completion.identity["actual"] == completion.identity["requested"]
        assert completion.identity["parameters"]["transport"] == "sse"
        assert completion.identity["pi_infer_sha256"] == hashlib.sha256(model.SCRIPT.read_bytes()).hexdigest()
