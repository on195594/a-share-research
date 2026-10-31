"""Release guards on captured runs in an isolated database; never stops services."""

import pytest

from a_share_research import db, deploy
from support.continuation import load


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
        conn.execute("UPDATE runs SET status='done' WHERE id=14")
        deploy.idle(database)
        reports = [r[0] for r in conn.execute("SELECT id FROM reports ORDER BY id LIMIT 2")]
        conn.execute("INSERT INTO research_progress (fingerprint, version, baseline_id, current_id,"
                     " input_hashes, status, created_at) VALUES ('f', 'v', ?, ?, '{}', 'queued', ?)",
                     (*reports, db.now()))
        for status in ("queued", "running"):
            conn.execute("UPDATE research_progress SET status=?", (status,))
            with pytest.raises(RuntimeError, match="研究进展"):
                deploy.idle(database)
        conn.execute("UPDATE research_progress SET status='failed'")
        deploy.idle(database)
        conn.execute("DELETE FROM research_progress")
        conn.execute("UPDATE runs SET status='running' WHERE id=14")
        changed = deploy.fingerprints(database)
        assert changed["runs"] != original["runs"]
        assert all(changed[table] == original[table] for table in original if table != "runs")
    finally:
        conn.close()
