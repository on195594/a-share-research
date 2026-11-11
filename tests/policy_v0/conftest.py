from __future__ import annotations

import pytest

from a_share_research.policy.v0 import paths


@pytest.fixture(autouse=True)
def isolated_cache_database(tmp_path, monkeypatch):
    """Keep every test and child process away from external runtime state."""
    for name in (
        "A_STOCK_STATE_DIR",
        "A_STOCK_RISK_POLICY_FILE",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(tmp_path / "missing-runtime.env"))
    monkeypatch.delenv("POLICY_V0_DB", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))  # child processes resolve the default from it
    assert paths.cache_db_path() == tmp_path / "data" / "app.db"  # tests/conftest.py isolates db.DATA
    database = tmp_path / "cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    assert paths.cache_db_path() == database
    yield database
