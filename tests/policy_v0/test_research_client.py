"""Research reads of policy v0: answered in process from the application database, read-only."""

import hashlib
import json

import pytest

from a_share_research import db as app_db, reading, service
from a_share_research.policy.v0 import cache, research_client
from test_service_delivery import SUCCESS, saved


@pytest.fixture
def research(isolated_cache_database):
    """The isolated database holding one real captured report that passed review and one that failed."""
    conn = app_db.connect(isolated_cache_database)
    run, report = saved(conn)
    saved(conn, passed=False)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    yield conn, run, report
    conn.close()


def _output(capsys, *args):
    assert cache.main(["research", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_every_read_returns_what_the_service_returns_and_changes_nothing(research, isolated_cache_database, capsys):
    conn, run, report = research
    code = SUCCESS["code"][:6]
    before = hashlib.sha256(isolated_cache_database.read_bytes()).hexdigest()

    assert _output(capsys, "reports", code) == {
        "version": service.VERSION, "reports": [dict(row) for row in reading.api_reports(conn, code)]}
    assert _output(capsys, "report", str(report)) == json.loads(json.dumps(service.report_view(conn, report)))
    compact = _output(capsys, "report", str(report), "--compact")
    assert compact == json.loads(json.dumps(service.report_view(conn, report, compact=True)))
    assert compact["view"] == "compact"
    assert _output(capsys, "status", str(run)) == {"version": service.VERSION, **dict(reading.api_run(conn, run))}
    request_id = conn.execute("SELECT request_id FROM runs WHERE id = ?", (run,)).fetchone()[0]
    assert _output(capsys, "resume", "--request-id", request_id) == json.loads(
        json.dumps(service.run_view(conn, request_id=request_id)))
    assert _output(capsys, "budget") == {"version": service.VERSION, "budget": service.run_budget(conn)}
    assert _output(capsys, "card", code) == json.loads(json.dumps(service.company_card(conn, code)))
    assert _output(capsys, "cards") == json.loads(json.dumps(service.company_cards(conn)))

    assert hashlib.sha256(isolated_cache_database.read_bytes()).hexdigest() == before


def test_missing_database_is_reported_and_not_created(isolated_cache_database, capsys):
    assert cache.main(["research", "reports", "600519"]) == 1
    assert "研究库不存在" in capsys.readouterr().err
    assert not isolated_cache_database.exists()


@pytest.mark.parametrize("args, message", [
    (["status", "999"], "Research 404：任务不存在"),
    (["report", "999"], "Research 404"),
    (["resume", "--request-id", "never-submitted"], "Research 404"),
])
def test_unknown_ids_fail_with_the_service_error(research, capsys, args, message):
    assert cache.main(["research", *args]) == 1
    output = capsys.readouterr()
    assert not output.out and message in output.err


@pytest.mark.parametrize("args", [
    ["start", "600519", "--question", "现金流变化？", "--request-id", "r-1"],
    ["feedback", "1", "--location", "summary", "--text", "数字有误", "--request-id", "r-2"],
])
def test_policy_runtime_cannot_submit_research(research, args):
    conn, _, _ = research
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert cache.main(["research", *args]) == 2
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs


def test_reader_refuses_a_route_it_does_not_serve(research):
    with pytest.raises(ValueError, match="只读取研究结果"):
        research_client.request_json("/api/research")
