from __future__ import annotations

import os

import pytest

from a_share_research.policy.v0 import paths


@pytest.fixture(autouse=True)
def isolated_cache_database(tmp_path, monkeypatch):
    """Keep every test and child process away from external runtime state."""
    for name in (
        "A_STOCK_STATE_DIR",
        "A_STOCK_RESEARCH_URL",
        "A_STOCK_RESEARCH_TOKEN",
        "A_STOCK_RISK_POLICY_FILE",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(tmp_path / "missing-runtime.env"))
    external_path = paths.cache_db_path()
    database = tmp_path / "cache.db"
    monkeypatch.setenv("CACHE_DB_PATH", str(database))
    assert paths.cache_db_path() == database
    assert paths.cache_db_path() != external_path
    yield database
    monkeypatch.setenv("CACHE_DB_PATH", str(database))
    assert os.environ["CACHE_DB_PATH"] != str(external_path)
