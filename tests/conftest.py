import pytest


@pytest.fixture(autouse=True)
def isolate_tushare_cache(monkeypatch, tmp_path):
    # Offline market replays may change between calls; never read/write the owner's cache.
    from a_share_research import db
    monkeypatch.setattr(db, "DATA", tmp_path / "data")
    monkeypatch.setenv("TUSHARE_CACHE", "0")
