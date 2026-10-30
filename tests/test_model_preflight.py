"""Real Node/Pi startup checks; no model completions or mocked subprocesses."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from a_share_research.model import SCRIPT


def check(env):
    process = subprocess.run(["node", str(SCRIPT), "--check"], env=env,
                             capture_output=True, text=True, timeout=30)
    return process.returncode, json.loads(process.stdout)


@pytest.fixture
def configured():
    if not all(os.environ.get(key) for key in ("MODEL_PROVIDER", "MODEL_ID", "PI_AGENT_DIR")):
        pytest.skip("Run with the existing worker environment for the installed Pi checks")
    return dict(os.environ)


def test_installed_runtime_resolves_model_without_calls_or_store_writes(configured):
    paths = [Path(configured["PI_AGENT_DIR"]) / name for name in ("auth.json", "models-store.json")]
    before = [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths]
    status, out = check(configured)
    assert status == 0 and out["ready"] is True
    assert (out["provider"], out["model"]) == (configured["MODEL_PROVIDER"], configured["MODEL_ID"])
    assert out["parameters"] is None and "usage" not in out and "message" not in out
    assert before == [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths]


def test_missing_configuration_blocks_worker_before_database_creation(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "MODEL_ID"}
    process = subprocess.run([sys.executable, "-m", "a_share_research.worker"],
                             env=env | {"DATA_DIR": str(tmp_path)},
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 1 and "MODEL_ID" in process.stderr
    assert not (tmp_path / "app.db").exists()


def test_missing_node_has_actionable_diagnostic(tmp_path):
    process = subprocess.run([sys.executable, "-c", "from a_share_research.model import preflight; preflight()"],
                             env=os.environ | {"PATH": str(tmp_path)},
                             capture_output=True, text=True, timeout=30)
    assert process.returncode != 0 and "找不到 Node" in process.stderr


def test_broken_pinned_runtime_is_not_silently_replaced(configured, tmp_path):
    missing = tmp_path / "missing-release"
    status, out = check(configured | {"PI_RUNTIME": str(missing)})
    assert status != 0 and "PI_RUNTIME" in out["error"] and str(missing) in out["error"]


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_model_store_damage_is_diagnosed_before_authentication(configured, tmp_path, damage):
    agent = Path(configured["PI_AGENT_DIR"])
    runtime = configured.get("PI_RUNTIME") or str(
        agent / "install/releases" / (agent / "install/current-version").read_text().strip()
        / "node_modules/@earendil-works/pi-coding-agent")
    if damage == "invalid":
        # Truncate a real catalog rather than inventing a provider/model fixture.
        (tmp_path / "models-store.json").write_bytes((agent / "models-store.json").read_bytes()[:1])
    status, out = check(configured | {"PI_AGENT_DIR": str(tmp_path), "PI_RUNTIME": runtime})
    assert status != 0 and "model store" in out["error"] and "restore" in out["error"]


def test_managed_release_pointer_drift_and_recovery(configured, tmp_path):
    agent = Path(configured["PI_AGENT_DIR"])
    install = tmp_path / "install"
    install.mkdir()
    (install / "current-version").symlink_to(tmp_path / "removed-pointer")
    env = configured | {"PI_AGENT_DIR": str(tmp_path), "PI_RUNTIME": ""}
    status, out = check(env)
    assert status != 0 and "current-version" in out["error"]
    # Restore via links to the actual runtime/stores; never copy credentials into the fixture.
    (tmp_path / "removed-pointer").write_text((agent / "install/current-version").read_text())
    (install / "releases").symlink_to(agent / "install/releases", target_is_directory=True)
    for name in ("auth.json", "models-store.json"):
        (tmp_path / name).symlink_to(agent / name)
    status, out = check(env)
    assert status == 0 and out["ready"]
