"""Real SQLite restart transitions and fresh gathering, without model calls."""

from copy import deepcopy
import json
import sqlite3
import sys

import pytest

from a_share_research import db, research, service, worker
from support.continuation import RECORDS
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
    run_id, _ = interrupted(conn)
    db.save_material(conn, run_id, MATERIAL.snapshot())
    before = row(conn, run_id)
    worker.recover(conn)
    resumed = row(conn, run_id)
    assert resumed["status"] == "queued" and resumed["step"] == "resumed"
    assert resumed["error"] == "服务重启后自动恢复" and resumed["finished_at"] is None
    assert all(resumed[k] == before[k] for k in ("request", "material", "started_at", "request_id", "identity"))
    # Another restart before dispatch must not consume a second attempt.
    worker.recover(conn)
    assert row(conn, run_id) == resumed
    db.update_run(conn, run_id, status="running", step="review")
    worker.recover(conn)
    failed = row(conn, run_id)
    assert failed["status"] == "failed" and failed["step"] is None and failed["finished_at"]
    assert failed["error"] == "研究中断（已自动恢复仍未完成，请重新开始）"
    assert failed["material"] == before["material"]
    assert failed["request"] == before["request"]
    worker.recover(conn)
    assert row(conn, run_id) == failed


@pytest.mark.parametrize("request_json", [None, "null"])
@pytest.mark.parametrize("kind", ["research", "price"])
def test_legacy_null_requests_also_have_one_attempt(conn, request_json, kind):
    run_id = db.create_run(conn, MATERIAL.code, request_id="recovery", kind=kind, status="running")
    db.update_run(conn, run_id, request=request_json)
    worker.recover(conn)
    assert row(conn, run_id)["request"] == request_json
    assert row(conn, run_id)["error"] == "服务重启后自动恢复"
    db.update_run(conn, run_id, status="running")
    worker.recover(conn)
    assert row(conn, run_id)["status"] == "failed"
    assert row(conn, run_id)["request"] == request_json


def test_resumed_request_can_be_submitted_idempotently(conn):
    record = next(r for r in RECORDS if r["run"]["request_id"] and r["run"]["request"])
    code, request_id = record["report"]["code"], record["run"]["request_id"]
    question = record["run"]["request"]["question"]
    run_id = service.submit(conn, code, request_id, question=question)
    original_request = row(conn, run_id)["request"]
    db.update_run(conn, run_id, status="running", step="explore")
    worker.recover(conn)
    assert row(conn, run_id)["request"] == original_request
    assert service.submit(conn, code, request_id, question=question) == run_id
    with pytest.raises(service.RequestError) as exc:
        service.submit(conn, code, request_id, question="")
    assert exc.value.status == 409
    db.update_run(conn, run_id, status="running", step="review")
    worker.recover(conn)
    assert service.submit(conn, code, request_id, question=question) == run_id


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


@pytest.mark.parametrize("snapshot_state", ["captured", "hash", "company", "missing"])
def test_execute_gathers_fresh_material_instead_of_interrupted_snapshot(conn, snapshot_state):
    run_id, _ = interrupted(conn)
    snapshot = deepcopy(MATERIAL.snapshot())
    if snapshot_state == "hash":
        snapshot["data"]["pages"].clear()
    elif snapshot_state == "company":
        db.update_run(conn, run_id, code="002648.SZ")
    if snapshot_state != "missing":
        db.update_run(conn, run_id, material=json.dumps(snapshot))
    worker.recover(conn)
    before = row(conn, run_id)
    # Stop at the real gather boundary, before external source calls.
    observed = execute_until(conn, run_id, research.gather)
    assert observed["code"] == before["code"]
    resumed = row(conn, run_id)
    assert resumed["status"] == "running" and resumed["step"] == "gather"
    assert resumed["started_at"] == before["started_at"]
    assert resumed["request"] == before["request"]
    assert resumed["error"] == "服务重启后自动恢复"
    worker.recover(conn)
    assert row(conn, run_id)["status"] == "failed"


def test_resumed_run_clears_error_on_success(conn):
    from a_share_research import price
    run_id, _ = interrupted(conn)
    worker.recover(conn)
    assert row(conn, run_id)["error"] == worker.AUTO_RESUME_ERROR
    # Research passed branch clears error
    research.publish(conn, run_id, MATERIAL, "2025-12-31", True, "值得继续研究", {"summary": "值得继续研究"})
    finished = row(conn, run_id)
    assert finished["status"] == "done"
    assert finished["error"] is None

    # Price done branch clears error
    price_run = db.create_run(conn, "600519", "price-resume-1", kind="price", status="running")
    db.update_run(conn, price_run, error=worker.AUTO_RESUME_ERROR)
    price.publish(conn, price_run, "600519", "贵州茅台", {"as_of": "2026-10-31", "expires": "2026-11-01", "summary": "2025-12-31 估值"})
    finished_price = row(conn, price_run)
    assert finished_price["status"] == "done"
    assert finished_price["error"] is None
