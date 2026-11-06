"""K6 content search over real saved reports: scope, latest-report rule, literal matching and paging."""
import json
from pathlib import Path

import pytest

from a_share_research import db, reading, service
from support.continuation import BODY, RECORDS, load
from support.templates import ENV


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
    assert {12, 143} <= ids  # 12 is a correction, 143 a replay of captured records
    # Each term is the excluded report's own searchable text, so only eligibility can keep it out.
    for report_id in (12, 143):
        term = BODY[report_id]["risks"][0]["text"][:14]
        hits, _ = reading.search_items(conn, term, history=True)
        assert report_id not in {h["report_id"] for h in hits}
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


def test_body_reads_stop_after_enough_hits(conn, monkeypatch):
    monkeypatch.setattr(reading, "SEARCH_REPORT_BATCH_SIZE", 1)
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        hits, more = reading.search_items(conn, "的", history=True, page_size=1)
    finally:
        conn.set_trace_callback(None)
    body_reads = [q for q in queries if q.startswith("SELECT id, json_extract(body,")]
    assert len(hits) == 1 and more
    assert len(body_reads) == 1
    assert "json_extract(b.body" not in queries[0]


@pytest.mark.parametrize("query", ["的", "现金流", "不存在的搜索词XYZ"])
@pytest.mark.parametrize("history", [False, True])
def test_batch_boundaries_preserve_results_and_pagination(conn, monkeypatch, query, history):
    monkeypatch.setattr(reading, "SEARCH_REPORT_BATCH_SIZE", 1000)
    expected = [reading.search_items(conn, query, history=history, page=p, page_size=3)
                for p in (1, 2, 10)]
    monkeypatch.setattr(reading, "SEARCH_REPORT_BATCH_SIZE", 1)
    assert [reading.search_items(conn, query, history=history, page=p, page_size=3)
            for p in (1, 2, 10)] == expected


def test_library_only_fetches_missing_hit_companies(conn, monkeypatch):
    original = reading.latest_reports
    calls = []

    def tracked(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(reading, "latest_reports", tracked)
    scoped = reading.library_results(conn, "东方电缆 现金流")
    assert scoped["hits"]
    # Company recognition already supplied the latest row; no enrichment query is needed.
    assert not any("codes" in call for call in calls)
    assert all(call.get("query") for call in calls)
    assert all(hit["latest"]["checkpoint"] is None for hit in scoped["hits"])
    calls.clear()
    found = reading.library_results(conn, "现金流")
    enrichment = [call for call in calls if "codes" in call]
    assert len(enrichment) == 1
    assert set(enrichment[0]["codes"]) == {hit["code"] for hit in found["hits"]}


@pytest.mark.parametrize("history", [False, True])
def test_latest_metadata_includes_other_code_formats(conn, history):
    conn.execute("UPDATE reports SET code = '603606.SH' WHERE substr(code,1,6) = '603606'")
    conn.execute("UPDATE reports SET code = '603606', passed = 0 WHERE id = 39")
    result = reading.library_results(conn, text_of(37)[:14], history=history)
    hits = [hit for hit in result["hits"] if hit["code"].startswith("603606")]
    assert hits
    assert all(hit["latest"]["id"] == 39 and not hit["latest"]["passed"] for hit in hits)


def test_stale_known_metadata_is_refreshed(conn, monkeypatch):
    body = json.loads(conn.execute("SELECT body FROM reports WHERE id = 39").fetchone()["body"])
    body["stance"] = {"text": "东方电缆新增通过报告"}
    conn.execute("UPDATE reports SET body = ? WHERE id = 39", (json.dumps(body, ensure_ascii=False),))
    original = reading.search_reports

    def stale(*args, **kwargs):
        rows, more = original(*args, **kwargs)
        return [dict(row) | {"id": 37, "passed": 0} for row in rows], more

    monkeypatch.setattr(reading, "search_reports", stale)
    result = reading.library_results(conn, "东方电缆")
    assert result["hits"]
    assert all(hit["latest"]["id"] >= hit["report_id"] for hit in result["hits"])


def test_missing_batch_body_is_skipped(conn):
    class MissingBodyConnection:
        def execute(self, sql, params=()):
            if sql.startswith("SELECT id, json_extract(body,"):
                # Simulate a report deleted between metadata and body reads.
                return iter(())
            return conn.execute(sql, params)

    assert reading.search_items(MissingBodyConnection(), "的", history=True) == ([], False)


def test_latest_metadata_has_uniform_mapping_type(conn):
    results = [reading.library_results(conn, query) for query in ("现金流", "东方电缆 现金流")]
    assert all(result["hits"] for result in results)
    assert all(isinstance(hit["latest"], dict) for result in results for hit in result["hits"])


def test_item_link_points_at_the_claim_or_the_stance():
    assert reading.item_link(7, "risks[2]") == "/reports/7?claim=risks:2#claim-risks-2"
    assert reading.item_link(7, "stance") == "/reports/7#stance"


def test_library_results_link_the_full_company_code_the_stock_route_expects():
    # /stock/{code} looks reports up by their stored code, suffix included; a bare six digits is a 404.
    template = (Path(__file__).parents[1] / "a_share_research/templates/research_library.html").read_text()
    assert 'href="/stock/{{ hit.code }}"' in template


def test_scope_needs_one_named_company_and_ignores_fragments(conn):
    company, rest = reading.search_scope(conn, "东方电缆 现金流")
    assert company["code"] == "603606.SH" and rest == ["现金流"]
    company, rest = reading.search_scope(conn, "现金流 603606 下降")
    assert company["code"] == "603606.SH" and rest == ["现金流", "下降"]
    company, rest = reading.search_scope(conn, "东方电缆 603606 现金流")
    assert company["code"] == "603606.SH" and rest == ["现金流"]
    for query in ("东方电缆", "东方电缆 603606", "茅台 格力 现金流", "现金流 下降", "6036 现金流", "东 现金流", "1 2 3 4 5 东方电缆"):
        assert reading.search_scope(conn, query) is None, query


def test_overlong_query_is_refused_before_a_company_name_can_shorten_it(conn):
    for query in ("东方电缆 " + "a" * 96, "东方电缆 " + "a" * 101):
        assert len(query) > 100 and reading.search_scope(conn, query) is None
        refused = reading.library_results(conn, query)
        assert "1–100 字" in refused["error"]
        assert refused["scope_company"] is None and refused["companies"] == [] and refused["hits"] == []
    assert reading.search_scope(conn, "东方电缆 " + "a" * 95)[1] == ["a" * 95]  # exactly 100 still scopes


def test_library_names_a_scope_only_when_the_whole_query_names_no_company(conn):
    scoped = reading.library_results(conn, "东方电缆 现金流")
    assert scoped["scope_company"]["code"] == "603606.SH"
    assert [c["code"] for c in scoped["companies"]] == ["603606.SH"]
    assert scoped["hits"] and {h["code"] for h in scoped["hits"]} == {"603606.SH"}
    assert all("现金流" in h["text"] for h in scoped["hits"])
    # The escape link searches every company for both words instead; scoped hits needn't mention the name.
    everywhere = reading.library_results(conn, "东方电缆 现金流", whole_library=True)
    assert everywhere["scope_company"] is None and everywhere["companies"] == []
    assert all("东方电缆" in h["text"] and "现金流" in h["text"] for h in everywhere["hits"])
    assert len(everywhere["hits"]) < len(scoped["hits"])
    second_page = reading.library_results(conn, "东方电缆 现金流", page=50)
    assert second_page["scope_company"] and second_page["companies"] == [] and second_page["hits"] == []


def test_library_answers_one_query_from_both_sides(conn):
    named = reading.library_results(conn, "茅台")
    assert [c["code"] for c in named["companies"]] == ["600519.SH"] and named["scope_company"] is None
    word = "现金流"
    content = reading.library_results(conn, word)
    assert content["companies"] == [] and content["hits"]
    hit = content["hits"][0]
    assert hit["label"] in reading.SEARCH_SECTIONS.values() and hit["url"].startswith(f"/reports/{hit['report_id']}")
    assert hit["latest"]["code"][:6] == hit["code"][:6]
    browse = reading.library_results(conn, "")
    assert len(browse["companies"]) == 3 and browse["hits"] == [] and not browse["error"]
    too_many = reading.library_results(conn, "一 二 三 四 五 六")
    assert "最多 5 个词" in too_many["error"] and too_many["hits"] == []


def test_library_next_page_follows_content_hits_when_companies_run_out(conn):
    first = reading.library_results(conn, "的", history=True)
    assert first["companies"] == [] and len(first["hits"]) == reading.PAGE_SIZE and first["more"] is True
    second = reading.library_results(conn, "的", history=True, page=2)
    assert second["hits"] and {h["text"] for h in second["hits"]} != {h["text"] for h in first["hits"]}


def _library_page(**context):
    return ENV.get_template("research_library.html").render(page=1, more=False, **context)


def test_library_page_has_one_box_and_explains_a_scope():
    html = _library_page(q="东方电缆 现金流", companies=[], hits=[], history=False, history_url="/h", all_url="/all",
                         scope_company={"name": "东方电缆", "code": "603606.SH"})
    assert html.count('name="q"') == 1 and 'name="content"' not in html
    assert "研究内容只搜了 东方电缆" in html and '<a href="/all#results">改搜全库</a>' in html
    assert '<a href="/h#results">包括旧报告再查</a>' in html
    assert "改搜全库" not in _library_page(q="现金流", companies=[], hits=[])


def test_search_requires_an_api_session_and_returns_the_envelope(server):
    _, client, url, *_ = server
    assert client.get(url + "/api/search", params={"q": "现金流"}, headers={"Authorization": "Bearer invalid"}, timeout=3).status_code == 401
    found = client.get(url + "/api/search", params={"q": "现金流"}, timeout=3)
    assert found.status_code == 200 and set(found.json()) == {"version", "items", "page", "more"}
    assert client.get(url + "/api/search", params={"q": " "}, timeout=3).status_code == 422


def test_latest_company_summary_excludes_feedback_report(conn):
    latest = reading.latest_reports(conn, code_prefix='600519')[0]
    assert latest['id'] != 12  # captured feedback must not replace the formal stance


@pytest.mark.parametrize('query', ['', '现金流'])
def test_search_form_and_pagination_target_visible_result_heading(query):
    from support.templates import render
    html = render('research_library.html', q=query, history=False, page=2,
                  hits=[], companies=[], error='查询需要缩短', more=True,
                  previous_url='/research-library?page=1', next_url='/research-library?page=3')
    assert html.count('action="/research-library#results"') == 1
    assert html.count('id="results" tabindex="-1"') == 1
    assert html.index('id="results"') < html.index('role="alert"')
    assert 'href="/research-library?page=1#results"' in html
    assert 'href="/research-library?page=3#results"' in html
