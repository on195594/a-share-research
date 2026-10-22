"""Identity and frozen-input checks using the real annual-report excerpts in test_check."""

import json
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from a_share_research import db
from a_share_research import model
from a_share_research.research import Material, ResearchError, check, execute
from test_check import MATERIAL, report


def test_snapshot_preserves_prompt_and_checks():
    snapshot = json.loads(json.dumps(MATERIAL.snapshot(), ensure_ascii=False))
    restored = Material.from_snapshot(snapshot)
    assert restored.prompt_block() == MATERIAL.prompt_block()
    assert restored.raw_pages == MATERIAL.raw_pages
    assert restored.raw_pages and all(isinstance(n, int) for n in restored.raw_pages)
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
    assert run["status"] == "failed" and run["error"] == "材料快照与任务公司不一致"
    db.update_run(conn, old, code=MATERIAL.code)
    wrong_target = db.create_run(conn, "600900.SH")
    execute(conn, wrong_target, replay_of=old)
    run = conn.execute("SELECT status, error FROM runs WHERE id = ?", (wrong_target,)).fetchone()
    assert run["status"] == "failed" and run["error"] == "重放材料与目标公司不一致"
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("node_failure", [False, True])
def test_real_model_configuration_failure_is_logged(tmp_path, node_failure):
    env = os.environ.copy()
    env.pop("MODEL_ID", None)
    env["MODEL_PROVIDER"] = "openai-codex"
    if node_failure:
        env.update(MODEL_ID="gpt-6.1-sol", NODE_OPTIONS="--no-such-flag")
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
        "    raise AssertionError('invalid configuration must fail')\n",
        str(path), MATERIAL.prompt_block(),
    ], env=env, check=True, timeout=15)
    conn = db.connect(path)
    row = conn.execute("SELECT * FROM calls").fetchone()
    identity = json.loads(row["identity"])
    assert identity["requested"] == {"provider": "openai-codex", "model": "gpt-6.1-sol" if node_failure else None}
    assert identity["actual"] is None and identity["pi_version"] is None
    assert identity["pi_infer_sha256"] == hashlib.sha256(model.SCRIPT.read_bytes()).hexdigest()
    expected = "--no-such-flag" if node_failure else "MODEL_PROVIDER and MODEL_ID are required"
    assert expected in row["error"]
    assert row["seconds"] >= 0
    conn.close()


def test_replay_stores_identical_material_and_compose_prompt_before_model_failure(tmp_path):
    env = os.environ.copy()
    env.pop("MODEL_ID", None)
    env.pop("TUSHARE_TOKEN", None)
    path = tmp_path / "replay.db"
    subprocess.run([
        sys.executable, "-c",
        "from pathlib import Path; import sys, json\n"
        "from a_share_research import db, model\n"
        "from a_share_research.research import Material, Runner, compose, execute\n"
        "conn = db.connect(Path(sys.argv[1]))\n"
        "material = Material.from_snapshot(json.loads(sys.argv[2]))\n"
        "source = db.create_run(conn, material.code)\n"
        "db.update_run(conn, source, material=sys.argv[2])\n"
        "try:\n"
        "    compose(Runner(conn, source), material)\n"
        "except model.ModelError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('missing MODEL_ID must fail')\n"
        "target = db.create_run(conn, material.code)\n"
        "execute(conn, target, replay_of=source)\n",
        str(path), json.dumps(MATERIAL.snapshot()),
    ], env=env, check=True, capture_output=True, text=True, timeout=15)
    conn = db.connect(path)
    source, target = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
    assert json.loads(target["material"]) == json.loads(source["material"]) == json.loads(json.dumps(MATERIAL.snapshot()))
    identity = json.loads(target["identity"])
    assert identity["replay_of"] == source["id"]
    assert identity["material_sha256"] == MATERIAL.snapshot()["sha256"]
    first, replay = conn.execute("SELECT * FROM calls ORDER BY id").fetchall()
    assert first["prompt"] == replay["prompt"] and replay["step"] == "compose"
    assert json.loads(replay["identity"])["prompt_sha256"] == hashlib.sha256(first["prompt"].encode()).hexdigest()
    assert target["status"] == "failed" and "MODEL_PROVIDER and MODEL_ID are required" in target["error"]
    conn.close()


def test_application_identity_is_frozen_at_import(tmp_path):
    package = tmp_path / "a_share_research"
    shutil.copytree(model.SCRIPT.parent, package, ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run([
        sys.executable, "-c",
        "from pathlib import Path\n"
        "from a_share_research import model\n"
        "assert model.SCRIPT.parent == Path.cwd() / 'a_share_research'\n"
        "before = model.application_identity().copy()\n"
        "template = model.SCRIPT.parent / 'templates/report.html'\n"
        "template.write_bytes(template.read_bytes() + b'\\n')\n"
        "assert model.application_identity() == before\n"
        "model.application_identity.cache_clear()\n"
        "assert model.application_identity() != before\n",
    ], cwd=tmp_path, check=True, timeout=15)


def test_web_latest_and_today_queries_exclude_old_and_new_replays(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    source = db.create_run(conn, MATERIAL.code, status="done")
    body = report()
    db.save_report(conn, source, MATERIAL.code, MATERIAL.name, MATERIAL.doc.period, True, body["verdict"], body)
    for marked_body in (False, True):
        replay = db.create_run(conn, MATERIAL.code, status="done")
        db.update_run(conn, replay, identity=json.dumps({"replay_of": source}))
        replay_body = body | ({"replay_of": source} if marked_body else {})
        db.save_report(conn, replay, MATERIAL.code, MATERIAL.name, MATERIAL.doc.period, True, body["verdict"], replay_body)
    conn.close()
    env = os.environ | {"DATA_DIR": str(tmp_path), "DEV_LOGIN": "1", "PUBLIC_BASE_URL": "http://127.0.0.1:8617",
                        "SESSION_SECRET": "local-query-test-" * 3}
    subprocess.run([
        sys.executable, "-c",
        "from a_share_research import web\n"
        "assert [r['run_id'] for r in web.latest_reports()] == [1]\n"
        "assert web.recent_report('600519')['run_id'] == 1\n"
        "web.conn.execute(\"UPDATE reports SET created_at = '2026-04-17' WHERE run_id = 1\")\n"
        "assert web.recent_report('600519') is None\n"
        "web.conn.execute('DELETE FROM reports WHERE run_id = 1')\n"
        "assert web.latest_reports() == []\n",
    ], env=env, check=True, timeout=15)
