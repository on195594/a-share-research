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


@pytest.mark.parametrize("existing_mode", [None, 0o700, 0o755])
def test_db_parent_permissions_do_not_change_existing_directories(
    tmp_path, existing_mode
):
    parent = tmp_path / "state"
    if existing_mode is not None:
        parent.mkdir(mode=existing_mode)
        parent.chmod(existing_mode)
    if existing_mode == 0o755:
        with pytest.raises(PermissionError, match="0700"):
            paths.ensure_db_parent(parent / "cache.db")
    else:
        paths.ensure_db_parent(parent / "cache.db")
    assert parent.stat().st_mode & 0o777 == (existing_mode or 0o700)


def test_relative_data_directory_is_refused_instead_of_starting_a_second_ledger(
    monkeypatch, capsys, tmp_path
) -> None:
    from a_share_research import db as app_db
    from a_share_research.policy.v0 import cache

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POLICY_V0_DB", raising=False)
    monkeypatch.setattr(app_db, "DATA", Path("data"))
    with pytest.raises(RuntimeError, match="absolute"):
        paths.cache_db_path()
    assert cache.main(["holdings"]) == 1
    assert "运行配置不可用" in capsys.readouterr().err
    assert cache.main(["--confirm-write", "add-holding", "600036", "40", "100"]) == 1
    assert list(tmp_path.iterdir()) == []
