"""Same-company continuation, batch reuse and report pages on real runs and reports.

Records come from the production database and the isolated K2 validation copies (see the data file).
"""

import importlib
import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

import a_share_research
from a_share_research import db, screening, reading
from a_share_research.research import _prior_report, _research_context, _verified_facts, prior_question, since_prior
from test_check import report

ROOT = Path(__file__).resolve().parents[1]
RECORDS = json.loads((ROOT / "tests/data/continuation-records.json").read_text())["records"]
BODY = {r["report"]["id"]: r["report"]["body"] for r in RECORDS}


def load(conn) -> None:
    for record in RECORDS:
        run, rep = record["run"], record["report"]
        conn.execute("INSERT INTO runs(id,request_id,kind,code,status,created_at,identity,request) VALUES (?,?,?,?,?,?,?,?)",
                     (run["id"], run["request_id"], run["kind"], rep["code"], run["status"], rep["created_at"],
                      json.dumps(run["identity"]) if run["identity"] else None, json.dumps(run["request"], ensure_ascii=False)))
        conn.execute("INSERT INTO reports(id,run_id,code,name,period,passed,verdict,body,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (rep["id"], run["id"], rep["code"], rep["name"], rep["period"], rep["passed"], rep["verdict"],
                      json.dumps(rep["body"], ensure_ascii=False), rep["created_at"]))


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
        companies, batch_only = web.latest_reports()
        assert [(c["code"], c["id"]) for c in companies] == [("603606.SH", 39), ("600519.SH", 12)]
        assert batch_only == 1  # 格力: a batch report plus a replay, never researched on its own
        assert companies[0]["checkpoint"] == BODY[39]["checkpoints"][0]["text"]
    finally:
        web.conn.close()


def render(name: str, **context) -> str:
    env = Environment(loader=FileSystemLoader(ROOT / "a_share_research/templates"), autoescape=select_autoescape())
    env.globals["asset_version"] = "test"
    if name == 'report.html':
        context['evidence'] = reading.evidence_context(context['body'], context['report'].get('period'))
    return env.get_template(name).render(csrf="", steps={}, request_id="r", **context)


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
    assert html.count('id="risks"') == 1 and '本报告未记录可列出的风险，不代表没有风险' in html
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
    assert '本报告未记录可列出的风险，不代表没有风险' in html
    assert '本报告未记录一句话结论' in html
    assert '不代表资料充分或没有风险' in html
    assert '研究流程：已完成并通过复核' in html


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
