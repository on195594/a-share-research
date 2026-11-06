"""K7a: a diagnostic run keeps its evidence but never counts as formal research."""

import json
from pathlib import Path

import pytest

from a_share_research import db, reading, research, screening, service, validation, worker, zones
from a_share_research.research_tools import active_methods_digest, research_methods

CAPTURED = json.loads((Path(__file__).parent / "data/research-methods.json").read_text())["reports"]
CODE = CAPTURED[0]["code"]


def insert(conn, row, *, run_id, report_id, kind="research", request=None):
    conn.execute("INSERT INTO runs(id,kind,code,status,created_at,request) VALUES (?,?,?,?,?,?)",
                 (run_id, kind, row["code"], "done", row["created_at"], json.dumps(request or row["request"])))
    conn.execute("INSERT INTO reports(id,run_id,code,period,passed,body,created_at) VALUES (?,?,?,?,?,?,?)",
                 (report_id, run_id, row["code"], row["period"], row["passed"], json.dumps(row["body"]), row["created_at"]))


@pytest.fixture
def stored(tmp_path):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    insert(conn, CAPTURED[0], run_id=CAPTURED[0]["run_id"], report_id=CAPTURED[0]["id"])
    yield conn, path
    conn.close()


def consumers(conn, path):
    """What every formal reader sees; each entry must ignore a diagnostic report."""
    return {
        "load_reports": [r["id"] for r in validation.load_reports(str(path))],
        "load_forecasts": [r["report"] for r in validation.load_forecasts(str(path))],
        "load_agenda": [r["id"] for r in validation.load_agenda(str(path))],
        "zones": [r["id"] for r in zones.load_reports(str(path))],
        "latest": [r["id"] for r in reading.latest_reports(conn)],
        "api_reports": [r["id"] for r in reading.api_reports(conn, CODE[:6])],
        "stock_reports": [r["id"] for r in reading.stock_reports(conn, CODE)],
        "batch_reuse": (lambda r: r and r["id"])(screening.reusable_report(conn, CODE, "2000-01-01")),
        "prior": (lambda r: r and r["report_id"])(research._prior_report(conn, CODE, 10_000)),
        "context": [c["report_id"] for c in research._research_context(conn, CODE, None)],
        "methods": [m["report_id"] for m in research_methods(conn, "现金流 口径")],
        "methods_digest": active_methods_digest(conn),
    }


def test_diagnostic_changes_no_formal_reader(stored):
    conn, path = stored
    before = consumers(conn, path)
    # A later correction-shaped diagnostic is the worst case: it reads as the newest report and a method source.
    insert(conn, CAPTURED[1], run_id=500, report_id=500, kind="diagnostic")
    conn.commit()
    assert consumers(conn, path) == before
    assert conn.execute("SELECT passed FROM reports WHERE id=500").fetchone()["passed"] == 1  # evidence is kept


def test_same_report_as_formal_research_is_visible(stored):
    # Mutual check: the assertion above fails if readers could not see this report at all.
    conn, path = stored
    before = consumers(conn, path)
    insert(conn, CAPTURED[1], run_id=500, report_id=500)
    conn.commit()
    after = consumers(conn, path)
    assert 500 in after["api_reports"] and 500 in after["stock_reports"] and after["methods"] != before["methods"]
    assert after["methods_digest"] != before["methods_digest"]


def test_diagnostic_is_not_a_base_for_formal_followups_and_stays_readable_by_id(stored):
    conn, _ = stored
    insert(conn, CAPTURED[1], run_id=500, report_id=500, kind="diagnostic")
    with pytest.raises(service.RequestError, match="诊断结果"):
        service.submit(conn, CODE[:6], "follow-up", parent_report_id=500, question="继续")
    assert service.run_view(conn, run_id=500)["kind"] == "diagnostic"
    assert service.report_view(conn, 500)["purpose"] == "diagnostic"
    assert service.report_view(conn, CAPTURED[0]["id"])["purpose"] == "research"


def test_submit_diagnostic_is_idempotent_and_cannot_reuse_a_formal_request(stored):
    conn, _ = stored
    source = {"event": "gap", "report_id": CAPTURED[0]["id"]}
    first = service.submit_diagnostic(conn, CODE[:6], "diag-1", question="核查缺口", source=source)
    assert service.submit_diagnostic(conn, CODE[:6], "diag-1", question="核查缺口", source=source) == first
    row = conn.execute("SELECT kind, request FROM runs WHERE id=?", (first,)).fetchone()
    assert row["kind"] == "diagnostic" and json.loads(row["request"])["source"] == source
    with pytest.raises(service.RequestError):
        service.submit_diagnostic(conn, CODE[:6], "diag-1", question="别的问题", source=source)
    formal = service.submit(conn, CODE[:6], "formal-1", question="现金流")
    with pytest.raises(service.RequestError):
        service.submit_diagnostic(conn, CODE[:6], "formal-1", question="现金流", source=source)
    with pytest.raises(service.RequestError):
        service.submit(conn, CODE[:6], "diag-1", question="核查缺口")
    assert conn.execute("SELECT kind FROM runs WHERE id=?", (formal,)).fetchone()["kind"] == "research"


def test_worker_leaves_diagnostics_for_the_idle_branch(stored):
    conn, _ = stored
    diagnostic = service.submit_diagnostic(conn, CODE[:6], "diag-1", question="核查", source={"event": "gap"})
    assert worker.next_run(conn) is None
    assert worker.next_diagnostic(conn)["id"] == diagnostic
    formal = service.submit(conn, CODE[:6], "formal-1", question="现金流")
    assert worker.next_run(conn)["id"] == formal


REAL = json.loads((Path(__file__).parent / "data/valuation-warning-reports-20261105.json").read_text())["reports"]
FORECAST = json.loads((Path(__file__).parent / "data/k7a-forecast-report.json").read_text())["report"]


def scored_readers(conn, path, code):
    return {
        "load_reports": {r["id"] for r in validation.load_reports(str(path))},
        "load_forecasts": {r["report"] for r in validation.load_forecasts(str(path))},
        "load_agenda": {r["id"] for r in validation.load_agenda(str(path))},
        "zones": {r["id"] for r in zones.load_reports(str(path))},
        "prior": (lambda r: r and r["report_id"])(research._prior_report(conn, code, 10_000)),
    }


@pytest.mark.parametrize("kind, counted", [("diagnostic", False), ("research", True)])
def test_plain_research_shaped_run_is_scored_only_when_formal(tmp_path, kind, counted):
    # Not correction-shaped, so only the kind filter stands between it and every score.
    row = REAL[0]
    body = row["body"] | {"prior": {"report_id": 1, "period": "2026-06-30"}, "prior_status": {"risks": [], "checkpoints": []},
                          "table": FORECAST["body"]["table"], "checkpoints": FORECAST["body"]["checkpoints"]}
    conn = db.connect(tmp_path / "app.db")
    insert(conn, {**row, "body": body, "passed": 1, "request": {"question": "现金流", "feedback": None}},
           run_id=700, report_id=700, kind=kind)
    conn.commit()
    seen = scored_readers(conn, tmp_path / "app.db", row["code"])
    conn.close()
    expected = {"load_reports": {700}, "load_forecasts": {700}, "load_agenda": {700}, "zones": {700}, "prior": 700}
    assert seen == (expected if counted else {key: None if key == "prior" else set() for key in expected})
