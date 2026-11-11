from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from a_share_research.policy.v0 import paths


def test_invalid_config_permissions_fail_closed(tmp_path, monkeypatch) -> None:
    config = tmp_path / "runtime.env"
    config.write_text("TUSHARE_TOKEN=private-test-token\n", encoding="utf-8")
    config.chmod(0o644)
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(config))
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="0600"):
        paths.read_private_config(config)
    with pytest.raises(RuntimeError, match="0600"):
        paths.tushare_token()


def test_importing_paths_does_not_create_home_state(tmp_path) -> None:
    config = tmp_path / "runtime.env"
    config.write_text("A_STOCK_STATE_DIR=/unused\n", encoding="utf-8")
    config.chmod(0o644)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "A_STOCK_CONFIG_FILE": str(config),
    }
    result = subprocess.run(
        [sys.executable, "-c", "import a_share_research.policy.v0.paths"],
        cwd=tmp_path.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".local").exists()

