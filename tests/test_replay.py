"""Identity and frozen-input checks using the real annual-report excerpts in test_check."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from a_share_research import db
from a_share_research.research import Material, ResearchError, check, execute
from test_check import MATERIAL, report


def test_snapshot_preserves_prompt_and_checks():
    snapshot = json.loads(json.dumps(MATERIAL.snapshot(), ensure_ascii=False))
    restored = Material.from_snapshot(snapshot)
    assert restored.prompt_block() == MATERIAL.prompt_block()
    assert restored.raw_pages == MATERIAL.raw_pages
    assert restored.snapshot()["sha256"] == snapshot["sha256"]
    assert check(report(), restored) == []
    snapshot["data"]["table"].pop()
    with pytest.raises(ResearchError, match="哈希"):
        Material.from_snapshot(snapshot)
    snapshot["version"] = 0
    with pytest.raises(ResearchError, match="版本"):
        Material.from_snapshot(snapshot)


def test_legacy_records_remain_unknown_and_old_writer_still_works(tmp_path):
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path, isolation_level=None)
    old.executescript(db.SCHEMA)  # original tables, before the additive identity columns
    run = db.create_run(old, MATERIAL.code, name=MATERIAL.name, status="done")
    body = report()
    db.save_report(old, run, MATERIAL.code, MATERIAL.name, MATERIAL.doc.period,
                   True, body["verdict"], body)
    conn = db.connect(path)
    legacy = conn.execute("SELECT * FROM runs WHERE id = ?", (run,)).fetchone()
    assert legacy["identity"] is None and legacy["material"] is None
    assert json.loads(conn.execute("SELECT body FROM reports").fetchone()[0]) == body
    later = db.create_run(old, MATERIAL.code)
    assert conn.execute("SELECT identity FROM runs WHERE id = ?", (later,)).fetchone()[0] is None
    old.close()
    conn.close()
    again = db.connect(path)
    assert again.execute("SELECT count(*) FROM reports").fetchone()[0] == 1
    again.close()


def test_old_or_wrong_company_material_fails_before_any_model_call(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    old = db.create_run(conn, MATERIAL.code, status="done")
    target = db.create_run(conn, MATERIAL.code)
    execute(conn, target, replay_of=old)
    run = conn.execute("SELECT status, error FROM runs WHERE id = ?", (target,)).fetchone()
    assert run["status"] == "failed" and "没有完整材料快照" in run["error"]
    db.update_run(conn, old, material=json.dumps(MATERIAL.snapshot()), code="600900.SH")
    another = db.create_run(conn, MATERIAL.code)
    execute(conn, another, replay_of=old)
    run = conn.execute("SELECT status, error FROM runs WHERE id = ?", (another,)).fetchone()
    assert run["status"] == "failed" and "公司不一致" in run["error"]
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    conn.close()


def test_real_missing_model_configuration_is_logged(tmp_path):
    env = os.environ.copy()
    env.pop("MODEL_ID", None)
    env["MODEL_PROVIDER"] = "openai-codex"
    path = tmp_path / "failure.db"
    subprocess.run([
        sys.executable, "-c",
        "from pathlib import Path; import sys\n"
        "from a_share_research import db, model\n"
        "from a_share_research.research import Runner\n"
        "conn = db.connect(Path(sys.argv[1]))\n"
        "run = db.create_run(conn, '600519.SH')\n"
        "try:\n"
        "    Runner(conn, run).ask('compose', sys.argv[2])\n"
        "except model.ModelError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('missing MODEL_ID must fail')\n",
        str(path), MATERIAL.prompt_block(),
    ], env=env, check=True, timeout=15)
    conn = db.connect(path)
    row = conn.execute("SELECT * FROM calls").fetchone()
    identity = json.loads(row["identity"])
    assert identity["requested"] == {"provider": "openai-codex", "model": None}
    assert identity["actual"] is None and identity["pi_version"] is None
    assert "MODEL_PROVIDER and MODEL_ID are required" in row["error"]
    assert row["seconds"] >= 0
    conn.close()
