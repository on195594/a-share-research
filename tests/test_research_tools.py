"""New calculations and task boundaries using captured Moutai data and disclosed excerpts."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import db, service
from a_share_research.research import Material, check, financial_table, output_schema, _research_context, _verified_facts
from a_share_research.research_tools import calculate
from test_check import MATERIAL, report
from test_financial_table import DATA


def test_same_period_growth_and_calculation_inputs_are_preserved():
    table = financial_table({k: pd.DataFrame(v) for k, v in DATA["financials"].items()}, DATA["market"])
    left, right = {"ref": "T.op_cash", "period": "2025-12-31"}, {"ref": "T.op_cash", "period": "2024-12-31"}
    row = calculate(table, "经营现金流同比", "growth_pct", left, right)
    values = next(r["values"] for r in table if r["id"] == "T.op_cash")
    expected = ((Decimal(values[left["period"]]) / Decimal(values[right["period"]]) - 1) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert row["values"] == {"result": str(expected)}
    assert row["calculation"]["inputs"][1]["value"] == values["2024-12-31"]
    with pytest.raises(ValueError, match="同月日"):
        calculate(table, "不可比", "growth_pct", left | {"period": "2026-06-30"}, right)
    with pytest.raises(ValueError, match="单位"):
        calculate(table, "不同单位", "ratio", left, {"ref": "T.roe", "period": "2025-12-31"})
    with pytest.raises(KeyError):
        calculate(table, "缺失", "ratio", left, right | {"period": "1900-06-30"})


def test_captured_half_year_comparison_uses_prior_half_year():
    table = json.loads((Path(__file__).parent / "data/600519-comparison.json").read_text())["table"]
    result = calculate(table, "现金流同比", "growth_pct", {"ref": "T.op_cash", "period": "2026-06-30"},
                       {"ref": "T.op_cash", "period": "2025-06-30"})
    assert result["calculation"]["inputs"][1]["value"] == "131.19"
    assert result["values"]["result"] == "438.84"


def test_output_contract_only_requests_feedback_for_a_real_feedback_task():
    material = Material.from_snapshot(MATERIAL.snapshot())
    assert '"answer"' not in output_schema(material)
    material.extra["question"] = "最新财报改变了什么，利润与现金流是否一致？"
    assert '"answer"' in output_schema(material) and "纠错" not in output_schema(material)
    material.extra["feedback"] = {"location": "risks[2]", "text": "销售模式改变的引文截断，需补齐续行"}
    assert "纠错" in output_schema(material)


def test_combined_review_location_excludes_the_flagged_fact():
    captured = json.loads((Path(__file__).parent / "data/603606-review.json").read_text())
    facts = _verified_facts(captured["report"], captured["problems"])
    assert facts["risks"] == captured["report"]["risks"][:2]


def test_multisource_and_calculation_replay_keeps_real_evidence():
    material = Material.from_snapshot(MATERIAL.snapshot())
    material.extra = {"sources": {"D1": {"document": {"title": MATERIAL.doc.title}, "pages": MATERIAL.pages,
                                       "raw_pages": MATERIAL.raw_pages}}}
    calc = calculate(material.table, "经营现金流差额", "difference",
                     {"ref": "T.op_cash", "period": "2025-12-31"}, {"ref": "T.op_cash", "period": "2024-12-31"})
    material.table.append(calc)
    restored = Material.from_snapshot(json.loads(json.dumps(material.snapshot())))
    assert restored.snapshot()["sha256"] == material.snapshot()["sha256"]
    result = report()
    result["business"][0]["cites"][0]["source"] = "D1"
    result["financial"] = [{"text": "经营现金流减少309.42亿元。", "cites": [{"ref": calc["id"], "period": "result"}]}]
    assert check(result, restored) == []
    # Optional answers are exported to API consumers even without an explicit question.
    result["answer"] = [{"text": "经营现金流减少309.42亿元。", "cites": []}]
    assert any(p["where"] == "answer[0]" for p in check(result, restored))
    result.pop("answer")
    result["business"][0]["cites"][0]["source"] = "D99"
    assert any(p["where"] == "business[0]" for p in check(result, restored))


def test_idempotency_and_feedback_reference_real_report(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: service.submit(conn, MATERIAL.code, "same", question="现金流为何减少？"), range(2)))
    assert ids[0] == ids[1]
    with pytest.raises(service.RequestError, match="不同内容"):
        service.submit(conn, MATERIAL.code, "same", question="营业收入为何减少？")
    run_id = ids[0]
    body = report() | {"table": MATERIAL.table}
    db.save_report(conn, run_id, MATERIAL.code, MATERIAL.name, MATERIAL.doc.period, True, body["verdict"], body)
    report_id = conn.execute("SELECT id FROM reports").fetchone()[0]
    with pytest.raises(service.RequestError, match="尚未就绪"):
        service.report_view(conn, report_id)
    db.update_run(conn, run_id, status="done", material=json.dumps(MATERIAL.snapshot()))
    before = conn.total_changes
    assert service.report_view(conn, report_id)["passed"]
    assert conn.total_changes == before
    with pytest.raises(service.RequestError, match="有效段落"):
        service.submit(conn, MATERIAL.code, "bad-location", parent_report_id=report_id,
                       feedback={"location": "financial[50]", "text": "核查现金流口径"})
    with pytest.raises(service.RequestError, match="公司不一致"):
        service.submit(conn, "600900", "wrong-company", parent_report_id=report_id)
    feedback_id = service.submit(conn, MATERIAL.code, "feedback", parent_report_id=report_id,
                                 feedback={"location": "risks[0]", "text": "现金流是否受到财务公司成员存款影响？"})
    assert feedback_id != run_id
    conn.close()


def test_failed_report_feedback_uses_the_visible_fact_positions(tmp_path):
    captured = json.loads((Path(__file__).parent / "data/603606-review.json").read_text())
    facts = _verified_facts(captured["report"], captured["problems"])
    conn = db.connect(tmp_path / "app.db")
    run_id = db.create_run(conn, "603606.SH", status="failed")
    db.save_report(conn, run_id, "603606.SH", "东方电缆", "2026-06-30", False, None,
                   {"facts": facts, "problems": captured["problems"]})
    report_id = conn.execute("SELECT id FROM reports").fetchone()[0]
    service.submit(conn, "603606", "failed-feedback", parent_report_id=report_id,
                   feedback={"location": "risks[1]", "text": "负债率上升能否全部解释为银行借款增加？"})
    context = _research_context(conn, "603606.SH", report_id)
    assert context[0]["basis"] == "facts"
    assert context[0]["claims_to_recheck"]["risks"][1] == facts["risks"][1]
    assert context[0]["claims_to_recheck"]["risks"] == captured["report"]["risks"][:2]
    conn.close()


def test_api_coverage_separates_subject_periods_and_preserves_report_gaps(tmp_path):
    captured = json.loads((Path(__file__).parent / "data/600519-comparison.json").read_text())["coverage_capture"]
    conn = db.connect(tmp_path / "app.db")
    run_id = db.create_run(conn, MATERIAL.code, status="done")
    body = report() | {"table": MATERIAL.table + captured["rows"], "gaps": captured["gaps"]}
    db.save_report(conn, run_id, MATERIAL.code, MATERIAL.name, MATERIAL.doc.period, True, body["verdict"], body)
    report_id = conn.execute("SELECT id FROM reports").fetchone()[0]
    view = service.report_view(conn, report_id)
    assert view["coverage"]["periods"] == ["2024-12-31", "2025-12-31"]
    assert view["coverage"]["missing"] == captured["gaps"]
    assert view["evidence"]["table"] == body["table"]
    conn.close()
