"""Release guards on captured runs in an isolated database; never stops services."""

import pytest

from a_share_research import db, deploy
from test_continuation import load


def test_idle_guard_and_fingerprints_on_captured_records(tmp_path):
    database = tmp_path / "app.db"
    conn = db.connect(database)
    try:
        load(conn)
        original = deploy.fingerprints(database)
        deploy.idle(database)
        assert deploy.fingerprints(database) == original
        for status in ("queued", "running"):
            conn.execute("UPDATE runs SET status=? WHERE id=14", (status,))
            with pytest.raises(RuntimeError, match="尚未空闲"):
                deploy.idle(database)
        changed = deploy.fingerprints(database)
        assert changed["runs"] != original["runs"]
        assert all(changed[table] == original[table] for table in original if table != "runs")
    finally:
        conn.close()
