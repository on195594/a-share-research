"""Delivery contracts use local SQLite and captured source material, never a paid run."""
import json
from pathlib import Path

import pytest

from a_share_research import db, reading, service
from a_share_research.research import MAX_CALLS
from test_check import MATERIAL
from test_continuation import load

RECORDS = json.loads((Path(__file__).parent / "data/continuation-records.json").read_text())["records"]
SUCCESS = next(item["report"] for item in RECORDS if item["report"]["id"] == 143)
FAILURE = json.loads((Path(__file__).parent / "data/research-delivery-failure.json").read_text())


def saved(conn, *, passed=True, parent=None):
    source = SUCCESS if passed else FAILURE
    run = service.submit(conn, source["code"], f"request-{parent}-{passed}", parent_report_id=parent)
    body = source["body"] if passed else {key: FAILURE[key] for key in ("problems", "gaps", "announcement_searches")}
    period = source["period"] if passed else max(source["coverage_periods"])
    db.save_report(conn, run, source["code"], source["name"], period, passed,
                   source.get("verdict") if passed else None, body)
    # Real captured annual-report excerpts supplement the captured same-company success body.
    db.update_run(conn, run, status="done" if passed else "failed", finished_at=db.now(),
                  material=json.dumps(MATERIAL.snapshot()) if passed else None,
                  error=None if passed else FAILURE["error"])
    return run, conn.execute("SELECT id FROM reports WHERE run_id = ?", (run,)).fetchone()[0]


def test_read_only_request_recovery_does_not_duplicate_or_restart(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    run = service.submit(conn, "600519", "lost response", question="现金流")
    before = conn.total_changes
    view = service.run_view(conn, request_id="lost response")
    assert view["id"] == run and view["status"] == "queued"
    assert view["budget"]["model_calls_recorded"] == 0
    assert view["budget"]["cost"] is None
    assert view["budget"]["model_call_limit"] == MAX_CALLS
    assert conn.total_changes == before
    with pytest.raises(service.RequestError) as exc:
        service.run_view(conn, request_id="not submitted")
    assert exc.value.status == 404
    assert service.submit(conn, "600519", "lost response", question="现金流") == run
    with pytest.raises(service.RequestError) as exc:
        service.submit(conn, "600519", "lost response", question="改了问题")
    assert exc.value.status == 409
    conn.close()


def test_compact_success_keeps_citations_and_drops_page_text(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    _, report = saved(conn)
    before = conn.total_changes
    full = service.report_view(conn, report)
    small = service.report_view(conn, report, compact=True)
    assert "view" not in full and "model_calls" in full
    assert small["report"]["summary"] == full["report"]["summary"]
    assert "pages" not in small["evidence"]["sources"]["D0"]
    assert small["evidence"]["sources"]["D0"]["pages_available"]
    assert small["continuation"]["freshness_status"] == "historical_not_live_checked"
    assert small["continuation"]["financial_period"] == SUCCESS["period"]
    assert len(json.dumps(small["evidence"])) < len(json.dumps(full["evidence"]))
    assert conn.total_changes == before
    conn.close()


def test_failed_result_exposes_problems_not_unreviewed_conclusions(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    run, report = saved(conn, passed=False)
    # Inject a real summary into a failed record to verify that unreviewed prose cannot leak.
    failed_body = json.loads(row(conn, report)["body"])
    failed_body["summary"] = SUCCESS["body"]["summary"]
    conn.execute("UPDATE reports SET body = ? WHERE id = ?", (json.dumps(failed_body), report))
    before = conn.total_changes
    view = service.report_view(conn, report, compact=True)
    assert view["passed"] is False
    assert "summary" not in view["report"]
    assert view["continuation"]["current_checkpoints"] == []
    assert view["diagnostics"]["status"] == "review_failed"
    assert view["diagnostics"]["problems"] == FAILURE["problems"]
    assert view["diagnostics"]["gaps"]
    assert service.run_view(conn, run_id=run)["diagnostics"] == view["diagnostics"]
    assert service.run_view(conn, request_id="request-None-False")["status"] == "failed"
    assert conn.total_changes == before
    conn.close()


def test_continuation_keeps_old_questions_separate_from_new_evidence(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    _, parent = saved(conn)
    _, child = saved(conn, parent=parent)
    view = service.report_view(conn, child, compact=True)["continuation"]
    assert view["parent_report_id"] == parent
    assert view["previous_checkpoints"] == SUCCESS["body"]["checkpoints"]
    assert view["previous_checkpoints_basis"] == "agenda_to_recheck_not_current_facts"
    assert view["table_changes"]["diff"]["disclosed"] == []
    assert view["table_changes"]["diff"]["revised"] == []  # Same tables do not invent a thesis change.
    assert view["baseline_status"] == "ready"
    conn.close()


@pytest.fixture
def records(tmp_path):
    conn = db.connect(tmp_path / "records.db")
    load(conn)  # Captured production/K2 records, not a synthetic investment report.
    yield conn
    conn.close()


def row(conn, report_id):
    return conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()


def test_compact_comparison_matches_shared_owner_on_real_records(records):
    before = records.total_changes
    for child, parent in [(9, 8), (12, 11), (39, 37)]:
        compact = service.report_view(records, child, compact=True)
        full = service.report_view(records, child)
        assert len(json.dumps(compact)) < len(json.dumps(full))
        actual = compact["continuation"]
        expected = reading.comparison(records, row(records, child))
        assert actual["parent_report_id"] == parent
        assert actual["table_changes"] == {k: expected[k] for k in ("diff", "excluded", "missing")}
        old = row(records, parent)
        old_body = json.loads(old["body"])
        assert actual["baseline_status"] == "ready"
        assert actual["baseline"]["generated_at"] == old["created_at"]
        assert actual["baseline"]["market_date"] == old_body.get("market_date")
        assert actual["baseline"]["document"] == old_body.get("document")
        assert actual["baseline"]["announcement_searches"] == old_body.get("announcement_searches", [])
        assert actual["baseline"]["financial_period"] == max([old["period"], *service._table_periods(old_body)])
    assert records.total_changes == before


@pytest.mark.parametrize("damage", ["different_unit", "missing_unit", "null_row", "null_table"])
def test_compact_legacy_and_incompatible_rows_are_excluded_not_compared(records, damage):
    old_body = json.loads(row(records, 37)["body"])
    body = json.loads(row(records, 39)["body"])
    common = next(r for r in body["table"] if r["id"].startswith("T.")
                  and any(o["id"] == r["id"] for o in old_body["table"]))
    label = common["label"]
    if damage == "different_unit":
        common["unit"] = "不同单位"
    elif damage == "missing_unit":
        common.pop("unit")
    elif damage == "null_row":
        body["table"].append(None)
    else:
        body["table"] = None
    records.execute("UPDATE reports SET body = ? WHERE id = 39", (json.dumps(body),))
    before = records.total_changes
    actual = service.report_view(records, 39, compact=True)["continuation"]["table_changes"]
    expected = reading.comparison(records, row(records, 39))
    assert actual == {key: expected[key] for key in ("diff", "excluded", "missing", "reason") if key in expected}
    if damage in {"different_unit", "missing_unit"}:
        assert label in actual["excluded"]
        assert not any(r["label"] == label for r in actual.get("diff", {}).get("revised", []))
    if damage == "null_row":
        assert "未知指标" in actual["excluded"]
    if damage == "null_table":
        assert "reason" in actual and "diff" not in actual
    assert records.total_changes == before


@pytest.mark.parametrize("state", ["ready", "failed", "not_ready", "missing", "not_same_code", "conflict"])
def test_baseline_state_is_explicit_even_without_checkpoints(records, state):
    old = row(records, 37)
    body = json.loads(old["body"])
    body.pop("checkpoints", None)
    records.execute("UPDATE reports SET body = ? WHERE id = 37", (json.dumps(body),))
    if state == "failed":
        records.execute("UPDATE reports SET passed = 0 WHERE id = 37")
        records.execute("UPDATE runs SET status = 'failed' WHERE id = ?", (old["run_id"],))
    elif state == "not_ready":
        records.execute("UPDATE runs SET status = 'running' WHERE id = ?", (old["run_id"],))
    elif state == "missing":
        records.execute("DELETE FROM reports WHERE id = 37")
    elif state == "not_same_code":
        records.execute("UPDATE reports SET code = '600519.SH' WHERE id = 37")
    elif state == "conflict":
        new = row(records, 39)
        body = json.loads(new["body"])
        body["parent_report_id"] = 12
        records.execute("UPDATE reports SET body = ? WHERE id = 39", (json.dumps(body),))
        records.execute("UPDATE runs SET request = ? WHERE id = ?",
                        (json.dumps({"parent_report_id": 37}), new["run_id"]))
    before = records.total_changes
    view = service.report_view(records, 39, compact=True)["continuation"]
    assert view["baseline_status"] == state
    assert view["previous_checkpoints"] == []
    if state != "ready":
        assert "diff" not in view["table_changes"] and view["table_changes"]["reason"]
    assert records.total_changes == before


def test_exception_without_report_retains_material_gaps_and_does_not_restart(records):
    run = row(records, 39)["run_id"]
    # Reconstruct only the material fields this read-only view uses from captured API evidence.
    material = {"data": {"extra": {key: FAILURE[key] for key in ("gaps", "announcement_searches")}}}
    records.execute("DELETE FROM reports WHERE id = 39")
    db.update_run(records, run, status="failed", material=json.dumps(material), error="调用超时")
    before = records.total_changes
    result = service.run_view(records, run_id=run)
    assert result["report_id"] is None and result["status"] == "failed"
    assert result["error"] == "调用超时"
    assert result["diagnostics"]["gaps"] == FAILURE["gaps"]
    assert result["diagnostics"]["gaps_basis"] == "material_snapshot"
    assert result["diagnostics"]["announcement_searches"] == FAILURE["announcement_searches"]
    assert "error 与 diagnostics.gaps" in result["diagnostics"]["next_step"]
    assert "已取证材料" not in result["diagnostics"]["next_step"]
    assert result["diagnostics"]["problems"] == []
    assert "material" not in result and "report_body" not in result
    assert records.total_changes == before


def test_legacy_json_null_request_and_material_are_readable(records):
    run = row(records, 39)["run_id"]
    db.update_run(records, run, request="null", material="null")
    before = records.total_changes
    assert service.report_view(records, 39)["question"] == ""
    assert service.report_view(records, 39, compact=True)["question"] == ""
    assert service.run_view(records, run_id=run)["diagnostics"]["gaps"]
    assert records.total_changes == before


def test_explicit_feedback_retry_and_read_only_recovery_coexist(records):
    # Captured report 12/run 18 carries the original feedback; use the same failed
    # lifecycle transition as the upstream retry tests, without running a worker.
    records.execute("UPDATE reports SET passed = 0 WHERE id = 12")
    records.execute("UPDATE runs SET status = 'failed' WHERE id = 18")
    original = service.feedback_retry_payload(records.execute("SELECT * FROM runs WHERE id = 18").fetchone())
    retried = service.retry_feedback(records, 18, "delivery-retry-interop")
    before = records.total_changes
    view = service.run_view(records, request_id="delivery-retry-interop")
    assert view["id"] == retried and view["status"] == "queued"
    assert view["budget"]["automatic_paid_restart"] is False
    assert records.total_changes == before
    saved_request = json.loads(records.execute("SELECT request FROM runs WHERE id = ?", (retried,)).fetchone()[0])
    assert saved_request["feedback"] == original["feedback"]
    assert saved_request["parent_report_id"] == original["parent_report_id"]
    assert service.report_view(records, 12, compact=True)["passed"] is False
    assert records.total_changes == before


def test_configuration_budget_does_not_claim_zero_observed_calls(records):
    run = row(records, 39)["run_id"]
    db.log_call(records, run, "review", "local diagnostic fixture")
    before = records.total_changes
    config = service.run_budget(records)
    assert config["scope"] == "configuration" and config["run_id"] is None
    assert config["model_calls_recorded"] is None and config["cost"] is None
    observed = service.run_budget(records, run)
    assert observed["scope"] == "run" and observed["run_id"] == run
    assert observed["model_calls_recorded"] == 1 and observed["cost"] is None
    assert records.total_changes == before
