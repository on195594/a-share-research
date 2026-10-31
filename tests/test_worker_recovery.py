"""Real SQLite restart transitions and captured material reuse, without model calls."""

from copy import deepcopy
import json
import sqlite3
import sys

import pytest

from a_share_research import db, integrity, research, worker
from a_share_research.research_tools import Acquisition
from support.continuation import RECORDS, BODY
from support.reports import MATERIAL


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "worker.db")
    yield connection
    connection.close()


def interrupted(conn):
    record = next(r for r in RECORDS if r["run"]["request_id"] and r["run"]["request"])
    run_id = db.create_run(conn, MATERIAL.code, request_id=record["run"]["request_id"], status="running")
    request = deepcopy(record["run"]["request"])
    db.update_run(conn, run_id, request=json.dumps(request), step="explore", started_at=record["report"]["created_at"])
    return run_id, request


def row(conn, run_id):
    return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def test_first_restart_requeues_once_and_preserves_request_and_material(conn):
    run_id, request = interrupted(conn)
    db.save_material(conn, run_id, MATERIAL.snapshot())
    before = row(conn, run_id)
    worker.recover(conn)
    resumed = row(conn, run_id)
    assert resumed["status"] == "queued" and resumed["step"] == "resumed"
    assert resumed["error"] is None and resumed["finished_at"] is None
    assert json.loads(resumed["request"]) == request | {"auto_resume_count": 1}
    assert all(resumed[k] == before[k] for k in ("material", "started_at", "request_id", "identity"))
    # Another restart before dispatch must not consume a second attempt.
    worker.recover(conn)
    assert row(conn, run_id) == resumed
    db.update_run(conn, run_id, status="running", step="review")
    worker.recover(conn)
    failed = row(conn, run_id)
    assert failed["status"] == "failed" and failed["step"] is None and failed["finished_at"]
    assert failed["error"] == "研究中断（已自动恢复仍未完成，请重新开始）"
    assert failed["material"] == before["material"]
    worker.recover(conn)
    assert row(conn, run_id) == failed


@pytest.mark.parametrize("request_json", [None, "null"])
@pytest.mark.parametrize("kind", ["research", "price"])
def test_legacy_null_requests_also_have_one_attempt(conn, request_json, kind):
    run_id = db.create_run(conn, MATERIAL.code, request_id="recovery", kind=kind, status="running")
    db.update_run(conn, run_id, request=request_json)
    worker.recover(conn)
    assert json.loads(row(conn, run_id)["request"])["auto_resume_count"] == 1
    db.update_run(conn, run_id, status="running")
    worker.recover(conn)
    assert row(conn, run_id)["status"] == "failed"


def test_cli_and_nonrunning_runs_are_untouched(conn):
    ids = [db.create_run(conn, MATERIAL.code, status="running")]
    ids += [db.create_run(conn, MATERIAL.code, request_id=status, status=status)
            for status in ("queued", "done", "failed")]
    before = [row(conn, run_id) for run_id in ids]
    worker.recover(conn)
    assert [row(conn, run_id) for run_id in ids] == before


def test_requeue_and_attempt_marker_commit_together(conn):
    run_id, _ = interrupted(conn)
    before = row(conn, run_id)
    conn.execute("CREATE TRIGGER reject_resume BEFORE UPDATE OF status ON runs "
                 "BEGIN SELECT RAISE(ABORT, 'interrupted write'); END")
    with pytest.raises(sqlite3.IntegrityError, match="interrupted write"):
        worker.recover(conn)
    assert row(conn, run_id) == before


class PowerLoss(BaseException):
    pass


def execute_until(conn, run_id, boundary):
    """Interrupt real execution at a Python call boundary, without replacing any function."""
    observed = {}
    def interrupt(frame, event, arg):
        if event == "call" and frame.f_code is boundary.__code__:
            observed.update(frame.f_locals)
            raise PowerLoss
    old = sys.getprofile()
    try:
        sys.setprofile(interrupt)
        with pytest.raises(PowerLoss):
            research.execute(conn, run_id)
    finally:
        sys.setprofile(old)
    return observed


@pytest.mark.parametrize("explored", [False, True])
def test_execute_restores_material_before_restarting_exploration(conn, explored, tmp_path):
    run_id, _ = interrupted(conn)
    material = research.Material.from_snapshot(deepcopy(MATERIAL.snapshot()))
    if explored:
        material.extra = {"prior": deepcopy(BODY[39]["prior"]), "question": BODY[39]["question"]}
    snapshot = material.snapshot()
    db.save_material(conn, run_id, snapshot)
    worker.recover(conn)
    before = row(conn, run_id)
    # Stop just before constructing the exploration session; gather and all DB writes run normally.
    observed = execute_until(conn, run_id, Acquisition.__init__)
    assert observed["material"].snapshot() == snapshot
    assert observed["runner"].run_id == run_id
    resumed = row(conn, run_id)
    assert resumed["material"] == before["material"] and resumed["started_at"] == before["started_at"]
    assert integrity.check(tmp_path / "worker.db", tmp_path / "pdf")[0] == []
    worker.recover(conn)
    assert row(conn, run_id)["status"] == "failed"


def test_execute_without_snapshot_still_gathers(conn):
    run_id, _ = interrupted(conn)
    worker.recover(conn)
    assert execute_until(conn, run_id, research.gather)["code"] == MATERIAL.code


@pytest.mark.parametrize("damage", ["hash", "company"])
def test_execute_rejects_corrupt_or_wrong_company_snapshot(conn, damage):
    run_id, _ = interrupted(conn)
    snapshot = deepcopy(MATERIAL.snapshot())
    if damage == "hash":
        snapshot["data"]["pages"].clear()
    else:
        db.update_run(conn, run_id, code="002648.SZ")
    db.update_run(conn, run_id, material=json.dumps(snapshot))
    worker.recover(conn)
    research.execute(conn, run_id)
    failed = row(conn, run_id)
    assert failed["status"] == "failed"
    assert ("哈希不匹配" if damage == "hash" else "公司不一致") in failed["error"]
