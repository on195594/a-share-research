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
    directory_mtime = paths[0].parent.stat().st_mtime_ns
    status, out = check(configured)
    assert status == 0 and out["ready"] is True
    assert (out["provider"], out["model"]) == (configured["MODEL_PROVIDER"], configured["MODEL_ID"])
    assert out["parameters"] is None and "usage" not in out and "message" not in out
    assert before == [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths]
    assert paths[0].parent.stat().st_mtime_ns == directory_mtime


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


def test_node_startup_failure_keeps_stderr_diagnostic():
    process = subprocess.run([sys.executable, "-c", "from a_share_research.model import preflight; preflight()"],
                             env=os.environ | {"NODE_OPTIONS": "--not-a-node-option"},
                             capture_output=True, text=True, timeout=30)
    assert process.returncode != 0 and "--not-a-node-option" in process.stderr
    assert "exit 9" in process.stderr


def test_broken_pinned_runtime_is_not_silently_replaced(configured, tmp_path):
    missing = tmp_path / "missing-release"
    status, out = check(configured | {"PI_RUNTIME": str(missing)})
    assert status != 0 and "PI_RUNTIME" in out["error"] and str(missing) in out["error"]


def isolated_agent(configured, tmp_path):
    agent = Path(configured["PI_AGENT_DIR"])
    runtime = configured.get("PI_RUNTIME") or str(
        agent / "install/releases" / (agent / "install/current-version").read_text().strip()
        / "node_modules/@earendil-works/pi-coding-agent")
    return agent, configured | {"PI_AGENT_DIR": str(tmp_path), "PI_RUNTIME": runtime}


def test_invalid_model_store_has_safe_root_cause(configured, tmp_path):
    agent, env = isolated_agent(configured, tmp_path)
    (tmp_path / "models-store.json").write_bytes((agent / "models-store.json").read_bytes()[:1])
    status, out = check(env)
    assert status != 0 and "models-store.json" in out["error"] and "SyntaxError" in out["error"]


@pytest.mark.parametrize("catalog", ["missing", "bom"])
def test_optional_catalog_and_bom_preserve_pi_compatibility(configured, tmp_path, catalog):
    agent, env = isolated_agent(configured, tmp_path)
    (tmp_path / "auth.json").symlink_to(agent / "auth.json")
    if catalog == "bom":
        (tmp_path / "models-store.json").write_bytes(b"\xef\xbb\xbf" + (agent / "models-store.json").read_bytes())
    else:
        # The configured model may exist only in the saved catalog. Use an actual shipped model.
        script = """
import {join} from 'node:path'; import {pathToFileURL} from 'node:url';
const load = name => import(pathToFileURL(join(process.env.PI_RUNTIME, 'dist/core', name)).href);
const {ModelRuntime} = await load('model-runtime.js'); const {AuthStorage} = await load('auth-storage.js');
const runtime = await ModelRuntime.create({credentials: AuthStorage.inMemory(), modelsPath: null,
  allowModelNetwork: false, refreshOnCreate: false});
console.log(JSON.stringify(runtime.getModels(process.env.MODEL_PROVIDER).map(m => m.id)));
"""
        models = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", script],
                                                    env=env | {"PI_OFFLINE": "1"}, text=True, timeout=30))
        if not models:
            pytest.skip("Configured provider has no shipped model catalog")
        env["MODEL_ID"] = models[0]
    before = tmp_path.stat().st_mtime_ns
    status, out = check(env)
    assert status == 0 and out["ready"]
    assert tmp_path.stat().st_mtime_ns == before


def test_missing_auth_is_checked_as_configuration_not_a_missing_file(configured, tmp_path):
    agent, env = isolated_agent(configured, tmp_path)
    (tmp_path / "models-store.json").symlink_to(agent / "models-store.json")
    status, out = check(env)
    # Some hosts use environment credentials only; both paths must leave the store absent.
    if status:
        assert "no configured credentials" in out["error"]
    else:
        assert out["ready"]
    assert not (tmp_path / "auth.json").exists()


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
