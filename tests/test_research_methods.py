"""Method retrieval and revision using two real, reviewed Moutai corrections."""

import hashlib
import json
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from a_share_research import db, service
from a_share_research.checking import check
from a_share_research.research import _research_context
from a_share_research.research_tools import (
    ResearchTools, active_methods_digest, method_cards, method_catalog, research_methods, run_method_catalog,
)
from support.reports import MATERIAL, report

CARDS = Path(__file__).parent / "data/method-cards"  # real card copied from the shared wiki, 2026-11-14
CAPTURED = json.loads((Path(__file__).parent / "data/research-methods.json").read_text())["reports"]


def test_only_completed_revision_replaces_the_target_method(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    for row in CAPTURED:
        conn.execute("INSERT INTO runs(id,code,status,created_at,request) VALUES (?,?,?,?,?)",
                     (row["run_id"], row["code"], "done", row["created_at"], json.dumps(row["request"])))
        conn.execute("INSERT INTO reports(id,run_id,code,period,passed,body,created_at) VALUES (?,?,?,?,?,?,?)",
                     (row["id"], row["run_id"], row["code"], row["period"], row["passed"],
                      json.dumps(row["body"]), row["created_at"]))
    original = conn.execute("SELECT body FROM reports WHERE id=11").fetchone()[0]
    frozen = method_catalog(conn, before=CAPTURED[0]["created_at"])
    frozen_digest = active_methods_digest(conn, catalog=frozen)
    for status in ("running", "failed", "done"):
        db.update_run(conn, 18, status=status)
        found = research_methods(conn, "现金流 口径")
        keys = {(r["report_id"], r["location"]) for r in found}
        assert ((11, "methods[0]") in keys) == (status != "done")
        assert ((12, "methods[0]") in keys) == (status == "done")
        assert (11, "methods[1]") in keys
    assert found[0]["code"] == "600519.SH"
    assert found[0]["sources"]["D1"]["document"]["url"].startswith("https://static.cninfo.com.cn/")
    assert conn.execute("SELECT body FROM reports WHERE id=11").fetchone()[0] == original
    assert all(not entry["claims_to_recheck"].get("methods") for entry in _research_context(conn, "600519.SH", None))
    assert _research_context(conn, "600519.SH", 12)[0]["claims_to_recheck"]["methods"] == CAPTURED[1]["body"]["methods"]
    assert research_methods(conn, "no matching research method") == []
    # A later method correction cannot change the catalog available to an already-started batch/run.
    from copy import deepcopy
    material = deepcopy(MATERIAL)
    material.extra["method_catalog"] = frozen
    retrieved = ResearchTools(material, conn).research_methods("现金流 口径")
    assert (11, "methods[0]") in {(r["report_id"], r["location"]) for r in retrieved}
    assert all(r["report_id"] != 12 for r in retrieved)
    assert material.extra["methods_used"] == retrieved
    assert active_methods_digest(conn, catalog=frozen) == frozen_digest
    assert active_methods_digest(conn) != frozen_digest
    assert method_catalog(conn, before=CAPTURED[0]["created_at"]) == frozen
    feedback_id = service.submit(conn, "600519", "method-review", parent_report_id=12,
                                 feedback={"location": "methods[0]", "text": CAPTURED[1]["request"]["feedback"]["text"]})
    assert feedback_id > 18
    conn.close()


def test_method_claims_must_have_evidence_in_this_material():
    # These real methods cite half-year pages absent from the annual-only material.
    result = report(methods=CAPTURED[0]["body"]["methods"])
    problems = check(result, MATERIAL)
    assert problems and all(p["where"].startswith("methods[") for p in problems)
    assert check(report(methods=[]), MATERIAL) == []


def test_method_cards_join_only_single_stock_catalogs_and_carry_no_evidence_identity(tmp_path, caplog):
    raw = (CARDS / "cyclical-earnings-normalization.md").read_bytes()
    cards = method_cards(CARDS)
    assert [card["card"] for card in cards] == ["cyclical-earnings-normalization"]
    card = cards[0]
    assert card["sha256"] == hashlib.sha256(raw).hexdigest()
    assert card["source_urls"] == [
        "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/commodityvaluedrivers.htm"]
    assert card["title"] and card["verified_at"] == "2026-11-14"
    assert card["method"]["text"].startswith("# ") and "status: stable" not in card["method"]["text"]
    # A card is guidance only: it has no report, evidence source or table row a report could cite.
    assert not {"report_id", "sources", "table", "code"} & set(card)

    conn = db.connect(tmp_path / "app.db")
    assert run_method_catalog(conn, before=None, batch=False, cards_dir=CARDS) == cards
    assert run_method_catalog(conn, before=None, batch=True, cards_dir=CARDS) == []
    assert run_method_catalog(conn, before=None, batch=False, cards_dir=None) == []
    conn.close()

    # Retrieval, the returned-record trail and the digest reuse the existing frozen-catalog path.
    material = deepcopy(MATERIAL)
    material.extra["method_catalog"] = cards
    tools = ResearchTools(material, None)
    assert tools.research_methods("低PE 正常化") == cards
    assert tools.research_methods("应收保理") == []
    assert material.extra["methods_used"] == cards
    assert '"card": "cyclical-earnings-normalization"' in material.prompt_block()
    assert active_methods_digest(None, catalog=cards) != active_methods_digest(None, catalog=[])

    # Minimal labelled variants of the real card: drafts and pages without frontmatter are not loaded.
    (tmp_path / "draft.md").write_bytes(raw.replace(b"status: stable", b"status: draft"))
    (tmp_path / "plain.md").write_text("# no frontmatter")
    assert method_cards(tmp_path) == []
    assert "draft.md" in caplog.text and "plain.md" in caplog.text


def test_actual_wiki_cards_when_explicitly_requested(tmp_path):
    """Opt-in cross-repo check: real changed cards, never just the copied fixture."""
    configured = os.environ.get("WIKI_METHOD_CARDS_TEST_DIR")
    if not configured:
        pytest.skip("set WIKI_METHOD_CARDS_TEST_DIR to check the actual Wiki files")
    root = Path(configured).resolve()
    assert root.is_dir() and root != CARDS.resolve()
    # Explicit acceptance revision, not a runtime-derived expectation.
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip() == (
        "e123b1a35496f19a51dce54dd06d0e706c556933")
    original_bytes = {path.name: path.read_bytes() for path in root.glob("*.md")}
    # title, lifecycle, primary source, source count, retrieval queries
    expected = {
        "cyclical-earnings-normalization": (
            "周期利润正常化与低市盈率陷阱", "stable",
            "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/commodityvaluedrivers.htm",
            1, ["低PE", "正常化"]),
        "growth-reinvestment-and-return-on-capital": (
            "增长、再投资与资本回报", "stable",
            "https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/returnmeasures.pdf",
            2, ["ROIC", "外部股权融资"]),
        "price-implied-expectations-analysis": (
            "价格隐含预期分析（反向估值）", "stable",
            "https://www.expectationsinvesting.com/about",
            3, ["隐含预期", "反向DCF"]),
        "free-cash-flow-and-valuation-basis": (
            "自由现金流与估值口径", "stable",
            "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/definitions.html",
            7, ["FCFF", "少数股东权益"]),
        "earnings-quality-and-cash-conversion": (
            "盈利质量与现金转化", "stable",
            "https://doi.org/10.2308/tar-9608042309",
            8, ["Dechow–Dichev", "Dechow-Dichev"]),
    }
    expected_sources = {
        "cyclical-earnings-normalization": [
            "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/commodityvaluedrivers.htm"],
        "growth-reinvestment-and-return-on-capital": [
            "https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/returnmeasures.pdf",
            "https://pages.stern.nyu.edu/~adamodar/podcasts/valspr20/session9slides.pdf"],
        "price-implied-expectations-analysis": [
            "https://www.expectationsinvesting.com/about", "https://www.expectationsinvesting.com/tutorials",
            "https://www.expectationsinvesting.com/online-tutorial-8"],
        "free-cash-flow-and-valuation-basis": [
            "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/definitions.html",
            "https://pages.stern.nyu.edu/~adamodar/pdfiles/valn2ed/ch15.pdf",
            "https://pages.stern.nyu.edu/~adamodar/pdfiles/valn2ed/ch14.pdf",
            "https://www.berkshirehathaway.com/letters/1986.html", "https://www.berkshirehathaway.com/letters/1992.html",
            "https://cas.xmu.edu.cn/info/2083/4954.htm", "https://www.shui5.cn/article/9e/24800.html"],
        "earnings-quality-and-cash-conversion": [
            "https://www.cuhk.edu.hk/acy2/workshop/June2009Wasley/1996TAR).pdf",
            "https://doi.org/10.2308/tar-9608042309", "https://api.crossref.org/works/10.2308/tar-9608042309",
            "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=277231",
            "https://api.crossref.org/works/10.2139/ssrn.277231", "https://doi.org/10.2308/accr.2002.77.s-1.35",
            "https://api.crossref.org/works/10.2308/accr.2002.77.s-1.35", "https://cas.xmu.edu.cn/info/2083/4954.htm"],
    }
    actual = method_cards(root)
    assert {card["card"] for card in actual} == set(expected)
    actual_by_id = {card["card"]: card for card in actual}
    copies = tmp_path / "frozen-copies"
    copies.mkdir()
    metadata = {}
    for slug, (title, status, primary, count, queries) in expected.items():
        raw = (root / f"{slug}.md").read_bytes()
        header, body = raw.decode().split("\n---\n", 1)
        meta = dict(line.split(": ", 1) for line in header.splitlines()[1:] if ": " in line)
        assert meta["title"] == title and meta["status"] == status
        assert meta["sources"].startswith("[") and meta["sources"].endswith("]")
        urls = re.findall(r"https?://[^\s,\]]+", meta["sources"])
        assert urls == expected_sources[slug]
        assert len(urls) == count and primary in urls
        assert all(url.startswith("https://") for url in urls)
        assert all(query in body for query in queries)
        digest = hashlib.sha256(raw).hexdigest()
        metadata[slug] = (title, urls, digest, queries)
        assert actual_by_id[slug]["sha256"] == digest
        assert actual_by_id[slug]["title"] == title
        assert actual_by_id[slug]["source_urls"] == urls
        for query in queries:
            assert slug in {card["card"] for card in research_methods(None, query, catalog=actual)}
        (copies / f"{slug}.md").write_bytes(raw)
        print(json.dumps({"card": slug, "status": status, "title": title, "source_count": count,
                          "sha256": digest, "queries": queries}, ensure_ascii=False))

    staged = method_cards(copies)
    assert {card["card"] for card in staged} == set(expected)
    for card in staged:
        title, urls, digest, queries = metadata[card["card"]]
        assert card["title"] == title and card["source_urls"] == urls
        assert card["sha256"] == hashlib.sha256((copies / f"{card['card']}.md").read_bytes()).hexdigest()
        for query in queries:
            assert card["card"] in {entry["card"] for entry in research_methods(None, query, catalog=staged)}
        assert card["sha256"] == digest

    conn = db.connect(tmp_path / "app.db")
    assert run_method_catalog(conn, before=None, batch=False, cards_dir=root) == actual
    assert run_method_catalog(conn, before=None, batch=True, cards_dir=root) == []
    conn.close()
    material = deepcopy(MATERIAL)
    material.extra["method_catalog"] = deepcopy(staged)
    frozen = deepcopy(material.extra["method_catalog"])
    digest = active_methods_digest(None, catalog=frozen)
    tools = ResearchTools(material, None)
    assert "growth-reinvestment-and-return-on-capital" in {
        card["card"] for card in tools.research_methods("外部股权融资")}
    assert material.extra["methods_used"]
    changed = copies / "free-cash-flow-and-valuation-basis.md"
    changed.write_text(changed.read_text() + "\nCompatibility-only mutation.\n")
    assert method_cards(copies) != frozen
    assert material.extra["method_catalog"] == frozen
    assert active_methods_digest(None, catalog=frozen) == digest
    assert {path.name: path.read_bytes() for path in root.glob("*.md")} == original_bytes
    _assert_mixed_catalog_retrieval(tmp_path, root, actual)


def _assert_mixed_catalog_retrieval(tmp_path, root, cards):
    """Real completed corrections + actual Wiki; diagnostic queries, not production calls."""
    conn = db.connect(tmp_path / "mixed.db")
    for row in CAPTURED:
        conn.execute("INSERT INTO runs(id,code,status,created_at,request) VALUES (?,?,?,?,?)",
                     (row["run_id"], row["code"], "done", row["created_at"], json.dumps(row["request"])))
        conn.execute("INSERT INTO reports(id,run_id,code,period,passed,body,created_at) VALUES (?,?,?,?,?,?,?)",
                     (row["id"], row["run_id"], row["code"], row["period"], row["passed"],
                      json.dumps(row["body"]), row["created_at"]))
    historical = method_catalog(conn)
    assert [(entry["report_id"], entry["location"]) for entry in historical] == [(12, "methods[0]"), (11, "methods[1]")]
    catalog = run_method_catalog(conn, before=None, batch=False, cards_dir=root)
    assert catalog == historical + cards
    assert run_method_catalog(conn, before=None, batch=True, cards_dir=root) == historical
    frozen = deepcopy(catalog)
    digest = active_methods_digest(None, catalog=catalog)
    old = lambda query, entries: [x for x in entries if any(t in x["method"]["text"] for t in query.split())][:5]
    # Positions in the frozen catalog; both priority and remainder must be stable.
    expected = {
        "利润 ROIC": [5, 0, 1, 2, 3],
        "利润 反向估值": [6, 0, 1, 2, 3],
        "利润 ROIC 反向估值": [5, 6, 0, 1, 2],
        "现金流 反向估值": [6, 0, 1, 3, 4],
    }
    material = deepcopy(MATERIAL)
    material.extra["method_catalog"] = catalog
    tools = ResearchTools(material, conn)
    used = []
    for query, positions in expected.items():
        found = tools.research_methods(query)
        assert found == [catalog[i] for i in positions]
        assert found != old(query, catalog) and len(found) == 5
        for entry in found:
            if entry not in used:
                used.append(entry)
        assert material.extra["methods_used"] == used
        assert tools.research_methods(query) == found  # Repeat calls do not duplicate the trail.
        assert material.extra["methods_used"] == used
        assert research_methods(conn, query, catalog=historical) == old(query, historical)
        assert research_methods(conn, query) == old(query, historical)
    for query in ("ROIC", "反向估值", "正常化", "应收保理从未出现_9a7e"):
        assert research_methods(None, query, catalog=catalog) == old(query, catalog)
    assert research_methods(None, "应收保理从未出现_9a7e", catalog=catalog) == []
    for query in ("利润 RO", "利润 roic", "利润 normalization", "利润 正常化"):
        assert research_methods(None, query, catalog=catalog) == old(query, catalog)
    # Known broad substring behavior: "no" matches normalization in Relations, not a negative example.
    assert research_methods(None, "no matching research method", catalog=catalog) == old(
        "no matching research method", catalog)
    assert research_methods(None, "no matching research method", catalog=catalog)
    assert research_methods(None, "利润", catalog=historical * 4) == old("利润", historical * 4)
    # Real titles have only one hit in this Wiki revision. Use a title-only labelled variant
    # to exercise title priority under overflow, without inventing/copying card prose.
    target = deepcopy(cards[-1])
    target["title"] = "利润"
    overflow = catalog[:-1] + [target]
    assert research_methods(None, "利润", catalog=overflow) == [target] + overflow[:4]
    # Even an explicit keyword cannot reorder a <=5-hit catalog.
    short = [historical[0], cards[-1]]
    assert research_methods(None, "利润 反向估值", catalog=short) == short
    # Five actual card hits do not overflow, regardless of explicit names.
    assert research_methods(None, "利润 ROIC 反向估值", catalog=cards) == cards
    assert catalog == frozen and material.extra["method_catalog"] == frozen
    assert active_methods_digest(None, catalog=catalog) == digest
    assert digest == "b23d70db389b33e372f372856db5c20adc45adeef2cea6e606e92bb7f2ba6e0d"
    conn.close()


@pytest.mark.parametrize("query", [None, 42, [], "", "  \t", "x" * 81])
def test_method_query_rejects_invalid_input(query):
    with pytest.raises(ValueError, match="1–80"):
        research_methods(None, query, catalog=[])


def test_method_query_empty_and_boundary_inputs():
    assert research_methods(None, "x" * 80, catalog=[]) == []
    assert research_methods(None, "  x  ", catalog=[]) == []


@pytest.mark.parametrize("term,prioritize", [("低PE", True), ("正常化", False)])
def test_overflow_priority_with_captured_card_without_wiki(term, prioritize):
    # Multiplicity-only boundary fixture: real correction text and a real card, no new prose.
    historical = [{"method": CAPTURED[1]["body"]["methods"][0]},
                  {"method": CAPTURED[0]["body"]["methods"][1]}] * 3
    card = method_cards(CARDS)[0]
    catalog = historical + [card]
    frozen = deepcopy(catalog)
    digest = active_methods_digest(None, catalog=catalog)
    found = research_methods(None, f"利润 {term}", catalog=catalog)
    assert found == ([card] + historical[:4] if prioritize else historical[:5])
    assert catalog == frozen and active_methods_digest(None, catalog=catalog) == digest


def test_retrieved_methods_page_label_is_not_adoption():
    from support.templates import render
    from support.valuation import BODY, RECORD

    cards = method_cards(CARDS)
    body = deepcopy(BODY) | {"methods_used": cards}
    html = render("report.html", report=RECORD | {"passed": True}, body=body,
                  rows={row["id"]: row for row in body["table"]}, fin=[], market=[],
                  comparison={"old": None}, retry={"label": "重试原研究问题", "note": ""})
    assert "检索到的方法" in html and cards[0]["title"] in html
    assert cards[0]["source_urls"][0] in html
