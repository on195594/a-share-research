"""Same-company continuation, batch reuse and report pages on real runs and reports.

Records come from the production database and the isolated K2 validation copies (see the data file).
"""

import importlib
import json
import sys
from pathlib import Path

from markupsafe import escape

import a_share_research
from a_share_research import db, screening, reading
from a_share_research.research import _prior_report, _research_context, _verified_facts, prior_question
from a_share_research.reading import since_prior
from support.reports import report
from support.continuation import BODY, RECORDS, load
from support.templates import render

ROOT = Path(__file__).resolve().parents[1]

def test_prior_report_skips_real_feedback_and_replays_and_carries_only_the_agenda(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    # 茅台: report 12 is a correction and 143 a replay, so 11 (a follow-up) is the latest judgment.
    prior = _prior_report(conn, "600519.SH", 999)
    assert prior is not None and prior["report_id"] == 11
    assert prior["risks"] == [item["text"] for item in BODY[11]["risks"]]
    assert "verdict" not in prior and "summary" not in prior
    # 东方电缆: the continuation 39 is the latest; from 39's own run only 37 is earlier.
    assert (_prior_report(conn, "603606.SH", 999) or {}).get("report_id") == 39
    assert (_prior_report(conn, "603606.SH", 56) or {}).get("report_id") == 37
    assert _prior_report(conn, "603606.SH", 53) is None
    # 39 asked the automatic question, which is how a replay of its frozen material recognises it.
    assert BODY[39]["question"] == prior_question(BODY[39]["prior"]) and BODY[39]["prior"]["question_added"]
    conn.close()


def test_followups_on_a_report_see_its_checkpoints_with_citations(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    claims = _research_context(conn, "603606.SH", 39)[0]["claims_to_recheck"]
    assert claims["checkpoints"] == BODY[39]["checkpoints"] and all(c["cites"] for c in claims["checkpoints"])
    conn.close()


def test_batches_never_reuse_a_continuation(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    assert screening.reusable_report(conn, "603606.SH", "2026-10-20")["id"] == 37
    assert screening.reusable_report(conn, "000651.SZ", "2026-10-20")["id"] == 25  # the replay 148 is skipped
    conn.close()


def test_home_lists_own_research_and_counts_batch_only_companies(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8617")
    monkeypatch.setenv("SESSION_SECRET", "continuation-home-test-" * 2)
    # Import a fresh web module on the temp database without leaking it to later tests.
    monkeypatch.setattr(a_share_research, "web", None, raising=False)
    monkeypatch.delitem(sys.modules, "a_share_research.web", raising=False)
    web = importlib.import_module("a_share_research.web")
    try:
        load(web.conn)
        latest = reading.latest_reports(web.conn)
        companies = [r for r in latest if r['own']]
        batch_only = sum(1 for r in latest if not r['own'])
        assert [(c["code"], c["id"]) for c in companies] == [("603606.SH", 39), ("600519.SH", 12)]
        assert batch_only == 1  # 格力: a batch report plus a replay, never researched on its own
        assert companies[0]["checkpoint"] == BODY[39]["checkpoints"][0]["text"]
    finally:
        web.conn.close()



def test_report_pages_render_for_reports_before_and_after_checkpoints(tmp_path):
    conn = db.connect(tmp_path / "render.db")
    load(conn)
    for report_id in (37, 39):
        record = next(r["report"] for r in RECORDS if r["report"]["id"] == report_id)
        body = record["body"]
        html = render("report.html", report=record, body=body, feedback=None, feedback_label="", replay_of=None,
                      rows={r["id"]: r for r in body["table"]}, price=None, followups=[],
                      comparison=reading.comparison(conn, conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()),
                      market=[r for r in body["table"] if r["id"].startswith("M.")],
                      fin=[r for r in body["table"] if r["id"].startswith("T.")])
        assert ('id="checkpoints"' in html) == (report_id == 39)
        assert ("与上次研究对照" in html) == (report_id == 39)
        assert "本报告未记录经营序列" in html
        # Keep the complete risk block near the conclusion, before a potentially long answer.
        start = html.index('<section id="risks"')
        risks = html[start:html.index('</section>', start)]
        assert html.index('class="conclusion"') < start
        if body.get('question'):
            assert start < html.index('<section id="answer"')
            nav = html[html.index('<nav aria-label="报告章节">'):html.index('</nav>')]
            assert nav.index('href="#risks"') < nav.index('href="#answer"')
        positions = []
        for i, risk in enumerate(body['risks']):
            paragraph = f'<p>{escape(risk["text"])}</p>'
            assert risks.count(paragraph) == 1 and html.count(paragraph) == 1
            positions.append(risks.index(paragraph))
            for cite in risk['cites']:
                if cite.get('quote'):
                    assert str(escape(cite['quote'])) in risks
            assert f'value="risks[{i}]"' in html  # Correction locations retain the original indices.
        assert positions == sorted(positions)
        assert html.count('id="risks"') == 1 and 'tabindex="-1"' in risks
    conn.close()


def test_report_without_recorded_risks_still_renders_risk_and_answer_sections(tmp_path):
    conn = db.connect(tmp_path / "legacy-layout.db")
    load(conn)
    record = next(r['report'] for r in RECORDS if r['report']['id'] == 39)
    body = json.loads(json.dumps(record['body']))
    body.pop('risks')
    html = render("report.html", report=record, body=body, feedback=None, feedback_label="", replay_of=None,
                  rows={r['id']: r for r in body['table']}, price=None, followups=[],
                  comparison=reading.comparison(conn, conn.execute('SELECT * FROM reports WHERE id=39').fetchone()),
                  market=[], fin=[r for r in body['table'] if r['id'].startswith('T.')])
    assert html.count('id="risks"') == 1 and '本报告未记录可列出的风险。' in html
    assert '<section id="answer" tabindex="-1">' in html
    conn.close()


def test_report_empty_risks_and_missing_summary_keep_unknown_explicit():
    record = next(r['report'] for r in RECORDS if r['report']['id'] == 39)
    body = json.loads(json.dumps(record['body']))
    body['risks'] = []
    body.pop('summary')
    body.pop('gaps')
    html = render('report.html', report=record, body=body, feedback=None, feedback_label='', replay_of=None,
                  rows={r['id']: r for r in body['table']}, price=None, followups=[],
                  comparison={}, market=[], fin=[])
    assert '本报告未记录可列出的风险。' in html
    assert '本报告未记录一句话结论' in html
    assert '未记录明确的证据缺口。' in html
    assert '研究判断：需要谨慎' in html


def test_checkpoint_finding_keeps_other_reviewed_facts_but_never_the_checkpoints():
    facts = _verified_facts(report(), [{"where": "checkpoints[0]", "problem": "阈值无出处"}], reviewed=True)
    assert facts["risks"] and facts["business"] and facts["financial"]
    assert "checkpoints" not in facts


def test_since_prior_separates_backfilled_comparison_periods_from_new_disclosures():
    tables = json.loads((ROOT / "tests/data/since-prior-600519.json").read_text())["tables"]
    change = since_prior(tables["5"], tables["8"])
    # Report 8 added the prior-year half as a comparison period; nothing newer was disclosed in between.
    assert change["backfilled"] == ["2025-06-30"] and change["disclosed"] == []
    assert change["revised"] == [] and change["uncollected"] == []
    assert {v["label"]: (v["before"], v["after"]) for v in change["valuation"]}["市盈率TTM"] == (
        ("2026-10-22", "20.24"), ("2026-10-22", "20.24"))
    # Reversed, the half year is no longer new and an absent row is never read as zero.
    assert since_prior(tables["8"], tables["5"])["backfilled"] == []


def test_since_prior_treats_a_later_valuation_date_as_a_move_not_a_gap():
    change = since_prior(BODY[8]["table"], BODY[11]["table"])  # market data 2026-10-22 → 2026-10-23
    assert change["uncollected"] == []
    assert {v["label"]: v["unchanged"] for v in change["valuation"]}["市盈率TTM"] is False


def test_since_prior_lists_indicators_the_earlier_report_never_collected():
    tables = json.loads((ROOT / "tests/data/since-prior-600900.json").read_text())["tables"]
    change = since_prior(tables["4"], tables["10"])
    # Report 4 predates the growth and margin rows; report 10 has them for periods report 4 already covered.
    assert change["uncollected"] == ["营业收入同比", "归母净利同比", "扣非净利同比", "毛利率", "加权ROE", "资产负债率"]
    assert change["backfilled"] == ["2025-06-30"] and change["revised"] == []


def test_table_comparison_counts_only_compared_rows_on_real_continuations():
    # Production K2 pairs: 五粮液 6→40 (half year → Q3), 40→42 and 万华 41→44 (same-period reruns).
    wly = json.loads((ROOT / "tests/data/since-prior-000858.json").read_text())["tables"]
    wh = json.loads((ROOT / "tests/data/since-prior-600309.json").read_text())["tables"]
    cases = {(6, 40): (wly["6"], wly["40"], ["2026-06-30"]), (40, 42): (wly["40"], wly["42"], ["2025-09-30"]),
             (41, 44): (wh["41"], wh["44"], [])}
    for pair, (old, new, dropped) in cases.items():
        result = reading.table_comparison(old, new)
        # Valuation rows are keyed by market date and C.* rows are numbered per report: neither is "missing".
        assert result["missing"] == [] and result["excluded"] == [], pair
        # A T.* period the new table no longer keeps is one period, not every indicator sharing it.
        assert result["dropped"] == dropped, pair
        assert result["diff"]["revised"] == [], pair
        assert {v["label"] for v in result["diff"]["valuation"]} == {"市盈率TTM", "市净率", "股息率TTM"}, pair
    # The noise was real: per-report calculations reuse ids with different labels across reports.
    assert {r["label"] for r in wly["40"] if r["id"] == "C.1"} != {r["label"] for r in wly["42"] if r["id"] == "C.1"}
    # Mutation: a genuinely absent statement row and a changed same-period value still surface.
    new = [dict(r) for r in wly["42"] if r["id"] != "T.inventory"]
    row = next(r for r in new if r["id"] == "T.revenue")
    period = next(p for p in row["values"] if p in next(r for r in wly["40"] if r["id"] == "T.revenue")["values"])
    row["values"] = row["values"] | {period: "1"}
    result = reading.table_comparison(wly["40"], new)
    assert result["missing"] == ["存货"] and [r["period"] for r in result["diff"]["revised"]] == [period]
    # Mutation: a T.* row whose unit changed is excluded rather than compared.
    new = [dict(r, unit="万元") if r["id"] == "T.revenue" else r for r in wly["42"]]
    assert reading.table_comparison(wly["40"], new)["excluded"] == ["营业总收入"]


def test_comparison_page_does_not_announce_valuation_moves_as_changes(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    for report_id in (9, 11, 39):
        comparison = reading.comparison(conn, conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())
        assert comparison["missing"] == [], report_id  # was M.*, peer and C.* labels before
        assert comparison["diff"]["valuation"] and not comparison["diff"]["revised"]
        html = render("_comparison.html", comparison=comparison)
        assert "项同口径数值修订" not in html and "项变动" not in html and "<details open" not in html
        assert "估值快照" in html
    conn.close()


def test_continuation_answer_is_never_folded_and_company_card_links_to_it(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    record = next(r["report"] for r in RECORDS if r["report"]["id"] == 39)
    comparison = reading.comparison(conn, conn.execute("SELECT * FROM reports WHERE id=39").fetchone())
    conn.close()

    def answer_section(body):
        html = render("report.html", report=record, body=body, feedback=None, feedback_label="", replay_of=None,
                      rows={r["id"]: r for r in body["table"]}, price=None, followups=[], comparison=comparison,
                      market=[], fin=[r for r in body["table"] if r["id"].startswith("T.")])
        start = html.index('<section id="answer"')
        return html[start:html.index('</section>', start)]
    # 39 continues 37 with 8 answer items; the old-agenda statuses at the end stay visible.
    body = record["body"]
    assert len(body["answer"]) > 5 and body["prior"]["report_id"] == 37
    assert "展开其余" not in answer_section(body)
    # Mutation: the same answer without a continuation still folds after four items.
    plain = {k: v for k, v in body.items() if k != "prior"}
    assert "展开其余 4 条" in answer_section(plain)
    # The company card links to the continuation answer only when the report continued an earlier one.
    assert reading.card(record, body)["prior"]["report_id"] == 37
    assert reading.card(record, plain)["prior"] is None
    assert reading.card(record | {"passed": 0}, body)["prior"] is None
    card = reading.card(record, body) | {"coverage": "2026-06-30", "created_at": record["created_at"]}
    meta = {**record, "replay_of": None, "question": body.get("question"), "parent_report_id": None,
            "feedback_text": None, "prior_report_id": 37}
    stock = render("stock.html", reports=[meta], code=record["code"], vitals=None, vitals_report=None,
                   comparison=comparison, price=None, track=None, due=None, card=card)
    assert f'href="/reports/39#answer">相对上次（资料至 {body["prior"]["period"]}）的风险与检查点复核' in stock
