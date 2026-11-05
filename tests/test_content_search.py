"""K6 content search over real saved reports: scope, latest-report rule, literal matching and paging."""
import json
from pathlib import Path

import pytest

from a_share_research import db, reading, service
from support.continuation import BODY, RECORDS, load


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "app.db")
    load(connection)
    yield connection
    connection.close()


def text_of(report_id, section="risks", index=0):
    return BODY[report_id][section][index]["text"]


def eligible(conn, code):
    return conn.execute(
        "SELECT r.id FROM reports r JOIN runs u ON u.id = r.run_id WHERE substr(r.code,1,6) = ? AND r.passed = 1"
        " AND u.status = 'done' AND u.kind = 'research' AND u.replay_of IS NULL"
        " AND json_extract(COALESCE(u.request,'{}'),'$.feedback') IS NULL ORDER BY r.created_at DESC, r.id DESC",
        (code,)).fetchall()


def test_default_scope_is_the_company_cards_report(conn):
    latest = service.company_card(conn, "603606")["report_id"]
    phrase = text_of(latest)[:12]
    hits, _ = reading.search_items(conn, phrase, code="603606")
    assert hits and {h["report_id"] for h in hits} == {latest}
    assert hits[0]["location"].startswith("risks[") and hits[0]["period"] and phrase in hits[0]["text"]


def test_a_term_only_in_an_older_report_needs_history(conn):
    latest, older = 39, 37
    newer_text = json.dumps(BODY[latest], ensure_ascii=False)
    term = next(item["text"][:14] for item in BODY[older]["risks"] if item["text"][:14] not in newer_text)
    assert reading.search_items(conn, term, code="603606")[0] == []
    assert {h["report_id"] for h in reading.search_items(conn, term, code="603606", history=True)[0]} == {older}


def test_corrections_and_replays_never_appear_even_with_history(conn):
    ids = {r["report"]["id"] for r in RECORDS}
    assert {12, 143} <= ids  # 茅台: 12 is a correction, 143 a replay of captured records
    hits, _ = reading.search_items(conn, "茅台", history=True)
    found = {h["report_id"] for h in hits}
    for report_id in (12, 143):
        assert report_id not in found
    allowed = {row["id"] for row in eligible(conn, "600519")}
    assert {h["report_id"] for h in hits if h["code"].startswith("600519")} <= allowed


def test_every_term_must_match_and_case_is_ignored(conn):
    phrase = text_of(39)
    first, second = phrase[:6], phrase[-6:]
    assert reading.search_items(conn, f"{first} {second}", code="603606")[0]
    assert reading.search_items(conn, f"{first} 绝不会出现的词", code="603606")[0] == []


def test_like_wildcards_are_literal(conn):
    for wildcard in ("%", "_"):
        assert all(wildcard in h["text"] for h in reading.search_items(conn, wildcard, history=True)[0])


def test_section_filter_and_stance_text_are_supported(conn):
    hits, _ = reading.search_items(conn, text_of(39)[:12], code="603606", section="business")
    assert all(h["location"].startswith("business[") for h in hits)
    body = json.loads(conn.execute("SELECT body FROM reports WHERE id = 39").fetchone()["body"])
    body["stance"] = {"action": "等价格", "text": "立场说明里的独特短语甲乙丙", "cites": []}
    conn.execute("UPDATE reports SET body = ? WHERE id = 39", (json.dumps(body, ensure_ascii=False),))
    assert [h["location"] for h in reading.search_items(conn, "独特短语甲乙丙")[0]] == ["stance"]


def test_results_never_carry_the_verdict(conn):
    hits, _ = reading.search_items(conn, text_of(39)[:12], code="603606")
    assert set(hits[0]) == {"report_id", "code", "name", "created_at", "period", "location", "text"}


def test_pages_are_stable_and_report_more(conn):
    first, more = reading.search_items(conn, "的", history=True, page_size=3)
    second, _ = reading.search_items(conn, "的", history=True, page=2, page_size=3)
    assert len(first) == 3 and more and second and not {json.dumps(h) for h in first} & {json.dumps(h) for h in second}
    dates = [h["created_at"] for h in first + second]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.parametrize("query,options", [
    ("", {}), ("   ", {}), ("字" * 101, {}), ("a b c d e f", {}),
    ("现金流", {"code": "60051"}), ("现金流", {"section": "methods"}), ("现金流", {"section": "table"}),
])
def test_invalid_queries_are_refused_with_a_clear_error(conn, query, options):
    with pytest.raises(service.RequestError):
        reading.search_items(conn, query, **options)


def test_item_link_points_at_the_claim_or_the_stance():
    assert reading.item_link(7, "risks[2]") == "/reports/7?claim=risks:2#claim-risks-2"
    assert reading.item_link(7, "stance") == "/reports/7#stance"


def test_library_results_link_the_full_company_code_the_stock_route_expects():
    # /stock/{code} looks reports up by their stored code, suffix included; a bare six digits is a 404.
    template = (Path(__file__).parents[1] / "a_share_research/templates/research_library.html").read_text()
    assert 'href="/stock/{{ hit.code }}"' in template


def test_search_requires_an_api_session_and_returns_the_envelope(server):
    _, client, url, *_ = server
    assert client.get(url + "/api/search", params={"q": "现金流"}, headers={"Authorization": "Bearer invalid"}, timeout=3).status_code == 401
    found = client.get(url + "/api/search", params={"q": "现金流"}, timeout=3)
    assert found.status_code == 200 and set(found.json()) == {"version", "items", "page", "more"}
    assert client.get(url + "/api/search", params={"q": " "}, timeout=3).status_code == 422
