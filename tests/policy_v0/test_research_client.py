"""Research reads of policy v0: answered in process from the application database, read-only."""

import hashlib
import json

import pytest
from fastapi import Request

from a_share_research import api_keys, db as app_db, reading, service
from a_share_research.account import cycle_analysis, ledger, mirror, position_store
from a_share_research.account.positions import PositionEntry
from a_share_research.account.reconciliation import identity
from a_share_research.policy.v0 import account_store, cache, monitor_research
from a_share_research.tools.registry import Context, invoke
from test_account import source_database
from test_account_ledger import batch, book as book
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
    assert _output(capsys, "budget") == {"version": service.VERSION, "budget": service.run_budget(conn)}
    assert _output(capsys, "card", code) == json.loads(json.dumps(service.company_card(conn, code)))
    assert _output(capsys, "cards") == {**service.company_cards(conn), "holdings_available": False}

    assert hashlib.sha256(isolated_cache_database.read_bytes()).hexdigest() == before


def test_verified_holdings_expand_only_monitor_cards(research, isolated_cache_database, tmp_path):
    conn, _, report = research
    (tmp_path / "old").mkdir()
    mirror.mirror(conn, source_database(tmp_path / "old" / "cache.db"))
    principal = api_keys.authenticate(conn, api_keys.create(conn, "cards-test", role="research"))
    public = invoke(Context(conn, isolated_cache_database, principal), "research_cards", {})["data"]
    assert public == {"version": service.VERSION, "cards": [], "missing": []}

    monitored = monitor_research.fetch_cards()
    assert monitored["status"] == "ok"
    [card] = monitored["cards"]
    assert (card["code"], card["origin"], card["report_id"]) == ("600519", "external", report)
    assert monitored["missing"] == [{"code": "000858", "origin": "external"}]

    conn.execute("INSERT INTO batches(status,created_at,rule,l5,frozen_at) VALUES ('frozen',?,'{}',?,?)",
                 (app_db.now(), json.dumps({"codes": ["000858.SZ"]}), app_db.now()))
    assert monitor_research.fetch_cards()["missing"] == [{"code": "000858", "origin": "l5"}]
    # A missing piece of mirror evidence invalidates the holding scope, while L5 stays available.
    (tmp_path / "account-snapshot.json").unlink()
    degraded = monitor_research.fetch_cards()
    assert degraded["status"] == "ok" and "未能核实持仓范围" in degraded["reason"]
    assert degraded["cards"] == [] and degraded["missing"] == [{"code": "000858", "origin": "l5"}]


def test_ledger_holding_card_is_only_visible_to_monitor(book, web, bind_account, tmp_path, monkeypatch):  # noqa: F811
    conn, assistant, evidence, sha = book
    directory = evidence.rename(tmp_path / "account-evidence")
    database = tmp_path / "app.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))

    position_store.initialize(conn, assistant, "a", [], directory, [sha])
    opening = account_store.initialize(conn, assistant, "a", directory, [sha])
    purchase = batch(book)
    purchase.position_entries["0"] = PositionEntry(policy_identity=opening["profile_identity"],
                                                   name=SUCCESS["name"], framework="E消费")
    receipt = ledger.submit(conn, assistant, "a", purchase, directory)
    assert receipt["status"] == "posted"
    cycle_analysis.initialize(conn, assistant, "a", "event:" + identity(purchase.events[0], "a"),
                              dict(expected_account_version=receipt["version"], evidence_hashes=[sha]), directory)
    bind_account(conn, "a")

    code = purchase.events[0].code
    with app_db.transaction(conn, immediate=False):
        view = service.company_cards(conn, holdings=True)
        assert conn.in_transaction and view["holdings_available"]
        assert [card["code"] for card in view["cards"]] == [code]
    monitored = monitor_research.fetch_cards()
    assert monitored["status"] == "ok"
    assert [(card["code"], card["origin"]) for card in monitored["cards"]] == [(code, "external")]

    token = api_keys.create(conn, "cards-test", role="research")
    principal = api_keys.authenticate(conn, token)
    public = invoke(Context(conn, database, principal), "research_cards", {})["data"]
    assert public == {"version": service.VERSION, "cards": [], "missing": []}
    request = Request({"type": "http", "method": "GET", "path": "/api/cards",
                       "headers": [(b"authorization", f"Bearer {token}".encode())]})
    route = next(r for r in web.app.routes if r.path == request.scope["path"] and "GET" in r.methods)
    assert route.endpoint(request) == public


def test_missing_database_is_reported_and_not_created(isolated_cache_database, capsys):
    assert cache.main(["research", "reports", "600519"]) == 1
    assert "研究库不存在" in capsys.readouterr().err
    assert not isolated_cache_database.exists()


@pytest.mark.parametrize("args, message", [
    (["status", "999"], "Research 404：任务不存在"),
    (["report", "999"], "Research 404"),
])
def test_unknown_ids_fail_with_the_service_error(research, capsys, args, message):
    assert cache.main(["research", *args]) == 1
    output = capsys.readouterr()
    assert not output.out and message in output.err


def test_local_resume_directs_scoped_request_to_authenticated_status(research, capsys):
    conn, _, _ = research
    request_id = "interrupted-request"
    principal = api_keys.authenticate(conn, api_keys.create(conn, "resume-test", role="research"))
    requester = service.requester(conn, principal)
    run = service.submit(conn, SUCCESS["code"], request_id, requester=requester)
    assert service.research_status(conn, requester=requester, request_id=request_id)["id"] == run
    assert _output(capsys, "status", str(run))["id"] == run
    assert cache.main(["research", "resume", "--request-id", request_id]) == 1
    output = capsys.readouterr()
    assert not output.out and "原凭据" in output.err and "research_status" in output.err


@pytest.mark.parametrize("args", [
    ["start", "600519", "--question", "现金流变化？", "--request-id", "r-1"],
    ["feedback", "1", "--location", "summary", "--text", "数字有误", "--request-id", "r-2"],
])
def test_policy_runtime_cannot_submit_research(research, args):
    conn, _, _ = research
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert cache.main(["research", *args]) == 2
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs


