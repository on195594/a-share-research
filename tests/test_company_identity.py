"""Company identity regressions on captured reports and real TuShare daily bars.

Code-form and review-status changes below are isolated test variants of those records.
"""

import importlib
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

import a_share_research
from a_share_research import db, price, reading, service
from support.continuation import load


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "app.db")
    load(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8627")
    monkeypatch.setenv("SESSION_SECRET", "company-identity-test-" * 2)
    monkeypatch.setattr(a_share_research, "web", None, raising=False)
    monkeypatch.delitem(sys.modules, "a_share_research.web", raising=False)
    web = importlib.import_module("a_share_research.web")
    monkeypatch.setattr(web, "conn", conn)
    try:
        with TestClient(web.app, base_url="http://127.0.0.1:8627", follow_redirects=False) as browser:
            assert browser.post("/auth/dev", headers={"Origin": web.settings.base_url}).status_code == 303
            yield browser
    finally:
        sys.modules.pop("a_share_research.web", None)


@pytest.mark.parametrize("code", ["603606", "603606.SH"])
def test_stock_page_finds_company_history(client, code):
    response = client.get(f"/stock/{code}")
    assert response.status_code == 200
    assert "东方电缆" in response.text
    for report_id in (37, 39):
        assert f'href="/reports/{report_id}"' in response.text


@pytest.mark.parametrize("code", ["603606.SS", "abc"])
def test_stock_page_rejects_invalid_codes(client, code):
    assert client.get(f"/stock/{code}").status_code == 404


@pytest.mark.parametrize("report_code", ["603606.SH", "603606"])
@pytest.mark.parametrize("code", ["603606", "603606.SH"])
def test_submit_price_queues_a_worker_compatible_code(conn, report_code, code):
    conn.execute("UPDATE reports SET code = ? WHERE id = 39", (report_code,))
    run_id = service.submit_price(conn, code, "identity-price")
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert (run["code"], run["name"], run["kind"], run["status"]) == (
        "603606.SH", "东方电缆", "price", "queued",
    )
    assert price.SUPPORTED_CODE.fullmatch(run["code"])
    assert service.submit_price(conn, code, "identity-price") == run_id


def test_submit_price_prefers_the_report_exchange(conn):
    # Deliberately mismatched but syntactically valid input; the saved report wins.
    run_id = service.submit_price(conn, "603606.SZ", "identity-exchange")
    assert conn.execute("SELECT code FROM runs WHERE id = ?", (run_id,)).fetchone()["code"] == "603606.SH"


@pytest.mark.parametrize("code", ["603606.SS", "abc"])
def test_submit_price_rejects_invalid_codes_without_writing(conn, code):
    before = conn.total_changes
    with pytest.raises(service.RequestError, match="需要6位数字或带合法交易所后缀的股票代码") as error:
        service.submit_price(conn, code, "invalid-price")
    assert error.value.status == 400
    assert conn.total_changes == before


def test_submit_price_requires_a_previous_report(conn):
    before = conn.total_changes
    with pytest.raises(service.RequestError, match="请先研究这家公司。") as error:
        service.submit_price(conn, "600900", "missing-report-price")
    assert error.value.status == 404
    assert conn.total_changes == before


@pytest.mark.parametrize("stored_code", ["600900.SH", "600900"])
def test_latest_price_resolves_both_code_forms(conn, stored_code):
    data = json.loads((Path(__file__).parent / "data/600900-61d.json").read_text())
    levels = price.compute(data["stock"], data["benchmark"])
    day = data["stock"][-1]["trade_date"]
    as_of = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    # Expiry is deliberately set to the captured day; only cache identity is under test.
    body = {"code": "600900.SH", "name": "长江电力", "as_of": as_of, "expires": as_of, "levels": levels}
    run_id = db.create_run(conn, stored_code, kind="price", name=body["name"])
    price.publish(conn, run_id, stored_code, body["name"], body)
    bare = reading.latest_price(conn, "600900")
    suffixed = reading.latest_price(conn, "600900.SH")
    assert bare == suffixed
    assert bare is not None
    assert bare["levels"] == levels
    assert bare["code"] == "600900.SH"
    assert bare["as_of"] == "2026-10-20"
    assert reading.latest_price(conn, "600900.SS") is None
    assert reading.latest_price(conn, "603606") is None


@pytest.fixture(params=[("603606.SH", "603606"), ("603606", "603606.SH")])
def mixed_codes(conn, request):
    old, new = request.param
    conn.executemany("UPDATE reports SET code = ? WHERE id = ?", [(old, 37), (new, 39)])
    return old, new


def test_latest_reports_deduplicate_mixed_codes(conn, mixed_codes):
    for code in ("603606", "603606.SH"):
        assert [r["id"] for r in reading.latest_reports(conn, codes=[code])] == [39]
    latest = reading.latest_reports(conn)
    assert [r["id"] for r in latest if r["code"][:6] == "603606"] == [39]
    # Other captured companies still retain their latest formal reports.
    assert {(r["code"][:6], r["id"]) for r in latest} == {
        ("603606", 39), ("600519", 11), ("000651", 25),
    }


def test_historical_report_links_to_latest_formal_report(client, mixed_codes):
    response = client.get("/reports/37")
    assert response.status_code == 200
    assert "正在阅读历史报告" in response.text
    assert 'href="/reports/39"' in response.text
    assert "最新正式研究（复核通过）" in response.text


def test_home_failed_latest_report_links_to_last_passed(client, conn, mixed_codes):
    # Status-only variant: preserve the captured report content and historical passed report.
    conn.execute("UPDATE reports SET passed = 0 WHERE id = 39")
    conn.execute("UPDATE runs SET status = 'failed' WHERE id = (SELECT run_id FROM reports WHERE id = 39)")
    response = client.get("/")
    assert response.status_code == 200
    cards = [c for c in response.context["companies"] if c["code"][:6] == "603606"]
    assert len(cards) == 1
    card = cards[0]
    assert card["id"] == 39 and not card["passed"]
    assert card["last_passed"]["id"] == 37
    assert card["last_passed"]["passed"] == 1
    assert card["summary"] == "" and card["verdict"] is None
    assert "上次通过的判断：" in response.text
    assert 'href="/reports/37"' in response.text
