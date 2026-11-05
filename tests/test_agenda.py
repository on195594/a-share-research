"""K5 agenda ledger and V5 grouping: real reports from production and the 贵州茅台 2025 annual report."""
import json
from copy import deepcopy
from datetime import datetime

import pytest
from markupsafe import escape

from a_share_research import agenda, db, reading, service, validation
from a_share_research.checking import check
from a_share_research.material import Material
from a_share_research.prompts import output_schema
from a_share_research.research import _research_context, _verified_facts, merge_repair_patch, open_questions, published
from a_share_research.reviewing import target_exists
from support.continuation import BODY, load
from support.reports import CASH, MATERIAL, report
from support.templates import render

PRIOR = {"report_id": 37, "researched": "2026-10-20", "period": "2025-12-31",
         "risks": ["经营现金流可能继续下降。", "销售费用可能继续上升。"], "checkpoints": ["下一期经营现金流是否恢复。"]}
CASH_ROW = {"ref": "T.op_cash", "period": "2025-12-31"}


def material(prior=PRIOR):
    copy = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    if prior is not None:
        copy.extra["prior"] = prior
    return copy


def statuses(**changes):
    items = [{"ref": "risks[0]", "status": "已发生", "text": "经营现金流降至615.22亿元。", "cites": [CASH_ROW]},
             {"ref": "risks[1]", "status": "无法判断", "text": "本次资料没有下一期销售费用，缺最新定期报告。", "cites": []},
             {"ref": "checkpoints[0]", "status": "未到期", "text": "要等2026年年报披露才能对照。", "cites": []}]
    for position, change in changes.items():
        items[int(position[1:])].update(change)
    return items


def where(problems, location="prior_status"):
    return [p["problem"] for p in problems if p["where"].startswith(location)]


def test_a_complete_ledger_passes_and_undecided_states_need_no_source():
    assert not where(check(report(prior_status=statuses()), material()))


@pytest.mark.parametrize("change,expected", [
    (lambda items: items[:2], "checkpoints[0]"),  # the error names what is missing
    (lambda items: [*items, items[0]], "重复"),
    (lambda items: [*items, {**items[0], "ref": "risks[9]"}], "不在旧议程"),
    (lambda items: [{**items[0], "status": "已触发"}, *items[1:]], "可用"),
    (lambda items: [items[0], items[1], {**items[2], "status": "已发生"}], "可用"),
])
def test_the_ledger_must_match_the_agenda_one_to_one(change, expected):
    assert any(expected in p for p in where(check(report(prior_status=change(statuses())), material())))


def test_a_continuation_must_state_the_ledger_and_a_fresh_report_must_not():
    assert "prior_status" in " ".join(p["where"] for p in check(report(), material()))
    assert where(check(report(prior_status=[]), material(prior=None)))
    assert not where(check(report(), material(prior=None)))


def test_an_empty_or_short_agenda_is_legal_including_frozen_history_without_a_section():
    assert not where(check(report(prior_status=[]), material({**PRIOR, "risks": [], "checkpoints": []})))
    older = {k: v for k, v in PRIOR.items() if k != "checkpoints"}
    assert not where(check(report(prior_status=statuses()[:2]), material(older)))
    assert where(check(report(prior_status=statuses()), material(older)))


# Mutations of the relaxation: only an admitted lack of evidence may go without a source.
@pytest.mark.parametrize("status", ["已发生", "仍存在", "已缓解", "已证伪"])
def test_any_decided_risk_status_without_a_source_is_rejected(status):
    problems = where(check(report(prior_status=statuses(i0={"status": status, "text": "现金流下降。", "cites": []})), material()))
    assert "没有出处" in problems


@pytest.mark.parametrize("status", ["已触发", "未触发"])
def test_any_decided_checkpoint_status_without_a_source_is_rejected(status):
    problems = where(check(report(prior_status=statuses(i2={"status": status, "text": "现金流未恢复。"})), material()))
    assert "没有出处" in problems


def test_a_forged_figure_is_caught_with_or_without_a_source():
    # 512.34 appears nowhere in the 2025 report: an unknown or a real citation can't launder it.
    unknown = where(check(report(prior_status=statuses(i1={"text": "销售费用约512.34亿元，无法判断。"})), material()))
    assert any("512.34" in p for p in unknown)
    decided = where(check(report(prior_status=statuses(i0={"text": "经营现金流降至512.34亿元。"})), material()))
    assert any("512.34" in p for p in decided)


def test_a_blank_reason_is_rejected_even_when_no_source_is_needed():
    assert where(check(report(prior_status=statuses(i1={"text": "  "})), material()))


def test_prompt_states_the_schema_only_for_continuations():
    assert "prior_status" in output_schema(material())
    assert "prior_status" not in output_schema(material(prior=None))


def test_repair_replaces_adds_and_rechecks_the_ledger():
    draft = report(prior_status=statuses()[:2])
    fixed = merge_repair_patch(draft, {"ops": [
        {"op": "add", "where": "prior_status", "value": statuses()[2]},
        {"op": "replace", "where": "prior_status[1]", "value": {**statuses()[1], "text": "缺下一期定期报告。"}},
    ]}, [], material())
    assert not where(check(fixed, material()))
    assert fixed["prior_status"][1]["text"] == "缺下一期定期报告。" and len(draft["prior_status"]) == 2
    lost_ref = merge_repair_patch(draft, {"ops": [{"op": "replace", "where": "prior_status[0]", "value": {
        "text": "经营现金流降至615.22亿元。", "cites": [CASH_ROW]}}]}, [], material())
    assert where(check(lost_ref, material()))


def test_deleting_every_status_is_reported_rather_than_dropping_the_field():
    draft = report(prior_status=statuses()[:1])
    merged = merge_repair_patch(draft, {"ops": [{"op": "delete", "where": "prior_status[0]"}]}, [], material())
    assert merged["prior_status"] == [] and where(check(merged, material()))


def test_published_keeps_the_ledger_and_review_can_target_it():
    assert published(report(prior_status=statuses()))["prior_status"] == statuses()
    draft = report(prior_status=statuses())
    assert target_exists("prior_status[2]", draft) and not target_exists("prior_status[3]", draft)


def test_a_true_gap_ships_named_but_a_disputed_status_blocks():
    gap = [{"id": "I1", "type": "question", "where": "prior_status[1]", "problem": "缺下一期销售费用"}]
    assert open_questions(gap, reviewed=True) == gap
    assert open_questions([{**gap[0], "type": "claim"}], reviewed=True) is None
    assert open_questions([{**gap[0], "type": "omission"}], reviewed=True) is None


def test_a_flagged_status_does_not_erase_verified_facts():
    draft = report(prior_status=statuses())
    facts = _verified_facts(draft, [{"where": "prior_status[0]", "problem": "状态不被支持", "type": "claim"}], reviewed=True)
    assert facts["risks"] == draft["risks"] and "prior_status" not in facts


def test_status_citations_count_on_the_report_page():
    rows = {"T.op_cash": {"calculation": None}}
    content = {"prior_status": [{"ref": "risks[0]", "status": "已发生", "text": "x", "cites": [CASH]}]}
    assert reading.evidence_ledger(content, rows, {})["quotes"] == 1


# ---------- real reports ----------

def continuation_body(entries):
    return {**deepcopy(BODY[39]), "prior_status": entries}


def test_status_entries_resolve_the_old_wording_against_the_prior_report(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    last = len(BODY[37]["risks"]) - 1
    resolved = reading.prior_agenda(conn, continuation_body([
        {"ref": f"risks[{last}]", "status": "仍存在", "text": "x", "cites": []},
        {"ref": "checkpoints[99]", "status": "无法判断", "text": "y", "cites": []}]))
    assert resolved[0]["previous"] == BODY[37]["risks"][last]["text"]
    assert resolved[0]["previous_url"] == f"/reports/37?claim=risks:{last}#claim-risks-{last}"
    assert resolved[1]["previous"] is None  # a position the old report lacks is shown without wording
    assert reading.prior_agenda(conn, BODY[39]) == []
    conn.close()


def test_status_entries_can_be_corrected_and_compact_readers_can_explain_them(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    body = continuation_body([{"ref": "risks[0]", "status": "仍存在", "text": "风险条件还在。", "cites": []}])
    conn.execute("UPDATE reports SET body = ? WHERE id = 39", (json.dumps(body, ensure_ascii=False),))
    view = service.report_view(conn, 39, compact=True)
    assert view["report"]["prior_status"] == body["prior_status"] and view["report"]["prior"]["report_id"] == 37
    assert view["continuation"]["previous_risks"] == BODY[37]["risks"]
    queued = service.submit(conn, "603606", "agenda-feedback", parent_report_id=39,
                            feedback={"location": "prior_status[0]", "text": "状态判断有误"})
    assert queued
    with pytest.raises(service.RequestError):
        service.submit(conn, "603606", "agenda-feedback-2", parent_report_id=39,
                       feedback={"location": "prior_status[1]", "text": "越界"})
    target = _research_context(conn, "603606.SH", 39)[0]
    assert target["claims_to_recheck"]["prior_status"] == body["prior_status"]
    # risks[0] names the prior report's item, which this report's own risks[0] is not.
    agenda_shown = target["prior_agenda"]
    assert agenda_shown["report_id"] == 37
    assert agenda_shown["period"] == conn.execute("SELECT coverage FROM reports WHERE id = 37").fetchone()[0]
    assert agenda_shown["risks"] == [item["text"] for item in BODY[37]["risks"]]
    assert agenda_shown["checkpoints"] == [item["text"] for item in BODY[37].get("checkpoints", [])]
    assert agenda_shown["risks"][0] != target["claims_to_recheck"]["risks"][0]["text"]
    # A report that continued nothing carries no old agenda.
    assert "prior_agenda" not in _research_context(conn, "603606.SH", 37)[0]
    conn.close()


@pytest.mark.parametrize("status", [["无法判断"], {"value": "无法判断"}, 1, None])
def test_a_malformed_status_is_a_repairable_problem_not_a_crash(status):
    items = statuses(i1={"status": status})
    found = where(check(report(prior_status=items), material()), "prior_status[1]")
    assert any("没有出处" in p for p in found) and any("可用" in p for p in found)


def test_the_report_page_shows_old_wording_status_and_sources_and_older_reports_keep_their_answer(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    row = dict(conn.execute("SELECT * FROM reports WHERE id = 39").fetchone())
    rows = {r["id"]: r for r in BODY[39]["table"]}
    page = lambda body, entries: render("report.html", report=row, body=body, feedback=None, feedback_label="", replay_of=None,
                                        rows=rows, price=None, followups=[], prior_agenda=entries, comparison=reading.comparison(conn, conn.execute("SELECT * FROM reports WHERE id = 39").fetchone()),
                                        market=[], fin=[])
    body = continuation_body([{"ref": "risks[0]", "status": "仍存在", "text": "风险条件还在，结果尚未体现。", "cites": BODY[39]["risks"][0]["cites"]}])
    html = page(body, reading.prior_agenda(conn, body))
    section = html[html.index('<section id="prior-status"'):]
    section = section[:section.index("</section>")]
    assert 'id="claim-prior_status-0"' in section and "仍存在" in section and "旧风险第 1 条" in section
    assert str(escape(BODY[37]["risks"][0]["text"])) in section and 'class="source"' in section
    assert 'href="#prior-status"' in html and 'value="prior_status[0]"' in html
    assert 'id="prior-status"' not in page(BODY[39], [])
    conn.close()


def scored_row(report_id, code, period, items, completed="2027-04-01T10:00:00+08:00", prior_period="2026-09-30"):
    return {"id": report_id, "code": code, "name": code, "source": "active", "period": period,
            "prior_period": prior_period, "prior_status": items, "completed": datetime.fromisoformat(completed)}


def test_groups_never_read_an_unknown_as_nothing_having_happened():
    decided = {"ref": "risks[0]", "status": "仍存在"}
    assert agenda.group([decided, {"ref": "checkpoints[0]", "status": "已触发"}]) == "存在已发生或已触发"
    assert agenda.group([decided, {"ref": "checkpoints[0]", "status": "未触发"}]) == "全部判定且无此类事项"
    assert agenda.group([decided, {"ref": "risks[1]", "status": "无法判断"}]) == "未判明"
    assert agenda.group([{"ref": "checkpoints[0]", "status": "未到期"}]) == "未判明"
    assert agenda.group([]) == "未判明"


def test_sample_filters_before_deduplicating():
    item = [{"ref": "risks[0]", "status": "已发生"}]
    split = agenda.sample([
        scored_row(1, "A", "2026-12-31", None, "2027-04-01T10:00:00+08:00"),  # earliest, but unrecorded
        scored_row(2, "A", "2026-09-30", item, "2027-04-02T10:00:00+08:00"),  # same period as its prior
        scored_row(3, "A", "2026-12-31", item, "2027-04-03T10:00:00+08:00"),
        scored_row(4, "A", "2026-12-31", item, "2027-04-04T10:00:00+08:00"),  # a rerun of 3
    ])
    assert [r["id"] for r in split["picked"]] == [3]
    assert [r["id"] for r in split["unrecorded"]] == [1] and [r["id"] for r in split["reruns"]] == [2]


def test_tally_keeps_undecided_out_of_the_shares_and_names_each_reports_unknowns():
    result = agenda.tally([scored_row(1, "A", "2026-12-31", [
        {"ref": "risks[0]", "status": "已发生"}, {"ref": "risks[1]", "status": "仍存在"},
        {"ref": "risks[2]", "status": "无法判断"}, {"ref": "checkpoints[0]", "status": "未到期"},
        {"ref": "checkpoints[1]", "status": "已触发"}, {"ref": "checkpoints[2]", "status": "未触发"}])])
    assert result["occurred"] == 0.5 and result["triggered"] == 0.5
    assert result["per_report"][0]["unknown"] == pytest.approx(1 / 6)
    assert agenda.tally([])["occurred"] is None


def test_agenda_cli_reads_real_history_without_market_data(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    load(conn)
    conn.close()
    loaded = validation.load_agenda(str(tmp_path / "app.db"))
    assert 39 in {r["id"] for r in loaded} and 37 not in {r["id"] for r in loaded}
    result = validation.score_agenda(loaded)  # no recorded status yet, so no provider is touched
    assert result["rows"] == [] and result["unrecorded"] == len(loaded)
    assert "入样 0 份" in validation.agenda_lines(result)[0]
