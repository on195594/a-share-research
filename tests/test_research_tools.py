"""New calculations and task boundaries using captured Moutai data and disclosed excerpts."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
import pytest
from jinja2 import Environment, FileSystemLoader

from a_share_research import db, service
from a_share_research.research import Material, check, financial_table, _research_context, _verified_facts, _review_issues
from a_share_research.research_tools import calculate
from a_share_research.sources import Document
from support.reports import MATERIAL, report
from test_financial_table import DATA


def test_same_period_growth_and_calculation_inputs_are_preserved():
    table = financial_table({k: pd.DataFrame(v) for k, v in DATA["financials"].items()}, DATA["market"])
    left, right = {"ref": "T.op_cash", "period": "2025-12-31"}, {"ref": "T.op_cash", "period": "2024-12-31"}
    row = calculate(table, "经营现金流同比", "growth_pct", left, right)
    values = next(r["exact_values"] for r in table if r["id"] == "T.op_cash")
    expected = ((Decimal(values[left["period"]]) / Decimal(values[right["period"]]) - 1) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert row["values"] == {"result": str(expected)}
    assert row["calculation"]["inputs"][1]["value"] == values["2024-12-31"]
    assert row["label"] == "相对变动率" and row["calculation"]["requested_label"] == "经营现金流同比"
    with pytest.raises(ValueError, match="同月日"):
        calculate(table, "不可比", "growth_pct", left | {"period": "2026-06-30"}, right)
    # Cross-period / sequential relative change and chained C.n references without explicit period.
    seq = calculate(table, "半年较上年全年相对变动", "change_pct", left | {"period": "2026-06-30"}, right)
    assert seq["unit"] == "%" and seq["calculation"]["operation"] == "change_pct"
    chained = calculate(table + [row], "同比增速占ROE比", "ratio", {"ref": row["id"]}, {"ref": "T.roe", "period": "2025-12-31"})
    assert chained["calculation"]["inputs"][0]["period"] == "result"
    with pytest.raises(ValueError, match="单位"):
        calculate(table, "不同单位", "ratio", left, {"ref": "T.roe", "period": "2025-12-31"})
    with pytest.raises(ValueError, match="可用期间：.*2024-12-31"):
        calculate(table, "缺失", "ratio", left, right | {"period": "1900-06-30"})
    with pytest.raises(ValueError, match="表中没有 T.nope"):
        calculate(table, "缺失", "ratio", left, {"ref": "T.nope", "period": "2025-12-31"})

    # Direct calculations on verified PDF disclosure figures (exact quote token + unit conversion).
    pdf_2025 = {"source": "D0", "page": 9, "quote": "销售费用 7,253,499,600.68 5,639,300,059.49 28.62",
                "value": "7,253,499,600.68", "unit": "元", "period": "2025-12-31", "label": "销售费用"}
    pdf_2024 = pdf_2025 | {"value": "5,639,300,059.49", "period": "2024-12-31"}
    share = calculate(MATERIAL.table, "销售费用占营收比", "ratio_pct",
                      pdf_2025, {"ref": "T.revenue", "period": "2025-12-31"}, material=MATERIAL)
    assert share["values"] == {"result": "4.22"}
    assert share["calculation"]["normalized_inputs"] == [
        {"value": "72.5349960068", "unit": "亿元"}, {"value": "1720.54", "unit": "亿元"}]
    assert share["calculation"]["inputs"][0]["value"] == "7253499600.68"
    assert share["calculation"]["inputs"][0]["unit"] == "元"
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "a_share_research/templates"),
                      autoescape=True)
    render = env.from_string('{% from "_macros.html" import cites %}{{ cites(item, rows) }}')
    def show_calculation(row):
        return render.render(item={"cites": [{"ref": row["id"]}]},
                             rows={r["id"]: r for r in MATERIAL.table + [row]})
    html = show_calculation(share)
    assert "72.5349960068 / 1720.54 * 100" in html
    assert "7253499600.68 / 1720.54" not in html
    assert "7253499600.68 元" in html  # Preserve exact original source input.
    # Historical rows lack normalized inputs: never invent or show a wrong numeric equation.
    share["calculation"].pop("normalized_inputs")
    html = show_calculation(share)
    assert "故不展示代入算式" in html
    assert "7253499600.68 / 1720.54" not in html
    yoy = calculate(MATERIAL.table, "销售费用同比", "growth_pct", pdf_2025, pdf_2024, material=MATERIAL)
    assert yoy["values"] == {"result": "28.62"}
    yoy["calculation"].pop("normalized_inputs")
    assert "(7253499600.68 / 5639300059.49 - 1) * 100" in show_calculation(yoy)
    with pytest.raises(ValueError, match="原数"):
        calculate(MATERIAL.table, "缩放或符号不符", "ratio_pct",
                  pdf_2025 | {"value": "72.53"}, {"ref": "T.revenue", "period": "2025-12-31"}, material=MATERIAL)
    with pytest.raises(ValueError, match="未见单位"):
        calculate(MATERIAL.table, "伪造单位", "ratio_pct",
                  pdf_2025 | {"unit": "亿元"}, {"ref": "T.revenue", "period": "2025-12-31"}, material=MATERIAL)
    with pytest.raises(ValueError, match="同一指标"):
        calculate(MATERIAL.table, "不同科目同比", "growth_pct",
                  pdf_2025, pdf_2024 | {"label": "管理费用"}, material=MATERIAL)


def test_huadong_calculations_keep_source_precision_and_legacy_material_limits():
    captured = json.loads((Path(__file__).parent / "data/calculation-precision.json").read_text())["000963"]
    table = financial_table({k: pd.DataFrame(v) for k, v in captured["financials"].items()}, {})
    current = {"ref": "T.op_cash", "period": "2026-09-30"}
    half = {"ref": "T.op_cash", "period": "2026-06-30"}
    profit = {"ref": "T.net_profit", "period": "2026-09-30"}
    cash = next(row for row in table if row["id"] == "T.op_cash")
    assert cash["values"]["2026-09-30"] == "18.61"
    quarter = calculate(table, "单季经营现金流", "difference", current, half)
    assert quarter["values"]["result"] == "-6.39"
    assert Decimal(quarter["exact_values"]["result"]) * Decimal("1e8") == Decimal("-638637273.00")
    assert quarter["calculation"]["inputs"][0]["value"] == "18.6078225763"
    assert calculate(table, "现金与利润比", "ratio_pct", current, profit)["values"]["result"] == "67.13"
    # Reusing the computed quarter must not reintroduce its rounded display value.
    combined = calculate(table + [quarter], "累计经营现金流", "sum",
                         {"ref": quarter["id"], "period": "result"}, half)
    assert Decimal(combined["exact_values"]["result"]) == Decimal("18.6078225763")
    historical = calculate(captured["saved_table"], "单季经营现金流", "difference", current, half)
    assert historical["values"]["result"] == "-6.38"
    assert "精度受该表值限制" in historical["calculation"]["precision"]
    assert historical["calculation"]["inputs"][0]["value_field"] == "values"


def test_real_pdf_ratio_does_not_promote_the_requested_metric_name():
    captured = json.loads((Path(__file__).parent / "data/calculation-precision.json").read_text())["603606"]
    doc = Document(**captured["document"])
    material = Material(doc.code, doc.name, doc, {}, {}, [], "2026-10-29",
                        {"sources": {"D1": {k: captured[k] for k in ("document", "pages", "raw_pages")}}})
    row = calculate(material.table, **captured["request"], material=material)
    assert row["values"]["result"] == "11.13"
    assert row["label"] == "比值（百分比）"
    assert row["calculation"]["requested_label"] == "三季度营业利润率"
    material.table.append(row)
    assert Material.from_snapshot(json.loads(json.dumps(material.snapshot()))).table == material.table
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "a_share_research/templates"), autoescape=True)
    template = env.from_string('{% from "_macros.html" import cites %}{{ cites(item, rows) }}')
    html = template.render(item={"cites": [{"ref": row["id"]}]}, rows={row["id"]: row})
    assert "比值（百分比）" in html and "三季度营业利润率" not in html
    assert "归母净利润" in html and "营业收入" in html


def test_multisource_and_calculation_replay_keeps_real_evidence():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
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
    peer_rows = [
        {"id": "M.close", "label": "收盘价", "unit": "元", "values": {"2026-10-22": "1318.49"},
         "source": {"provider": "TuShare", "code": "600519.SH", "retrieved_at": "2026-10-23T15:15:32+08:00"}},
        {"id": "P000858.T.revenue", "label": "五粮液·营业总收入", "unit": "亿元", "values": {"2026-06-30": "284.17"},
         "source": {"provider": "TuShare", "code": "000858", "retrieved_at": "2026-10-23T15:15:49+08:00"}},
    ]
    gaps = ["尚未完成剔除财务公司影响后的酒业经营现金流与利润勾稽，不能给出调整后现金转化率。"]
    body = report() | {"table": MATERIAL.table + peer_rows, "gaps": gaps}
    db.save_report(conn, run_id, MATERIAL.code, MATERIAL.name, MATERIAL.doc.period, True, body["verdict"], body)
    report_id = conn.execute("SELECT id FROM reports").fetchone()[0]
    with pytest.raises(service.RequestError, match="尚未就绪"):
        service.report_view(conn, report_id)
    with pytest.raises(service.RequestError, match="尚未就绪"):
        service.submit(conn, "", "unready-parent", parent_report_id=report_id,
                       feedback={"location": "risks[0]", "text": "核查现金流"})
    db.update_run(conn, run_id, status="done", material=json.dumps(MATERIAL.snapshot()))
    before = conn.total_changes
    view = service.report_view(conn, report_id)
    assert view["passed"]
    assert view["coverage"]["periods"] == ["2024-12-31", "2025-12-31"]
    assert view["coverage"]["missing"] == gaps
    assert view["evidence"]["table"] == body["table"]
    assert conn.total_changes == before
    with pytest.raises(service.RequestError, match="有效段落"):
        service.submit(conn, MATERIAL.code, "bad-location", parent_report_id=report_id,
                       feedback={"location": "financial[50]", "text": "核查现金流口径"})
    with pytest.raises(service.RequestError, match="公司不一致"):
        service.submit(conn, "600900", "wrong-company", parent_report_id=report_id)
    feedback_id = service.submit(conn, "", "feedback", parent_report_id=report_id,
                                 feedback={"location": "risks[0]", "text": "现金流是否受到财务公司成员存款影响？"})
    assert feedback_id != run_id
    conn.close()


def test_failed_report_feedback_uses_the_visible_fact_positions(tmp_path):
    captured = json.loads((Path(__file__).parent / "data/603606-review.json").read_text())
    facts = _verified_facts(captured["report"], captured["problems"], reviewed=True)
    conn = db.connect(tmp_path / "app.db")
    run_id = db.create_run(conn, "603606.SH", status="failed")
    db.save_report(conn, run_id, "603606.SH", "东方电缆", "2026-06-30", False, None,
                   {"facts": facts, "facts_reviewed": True, "problems": captured["problems"]})
    report_id = conn.execute("SELECT id FROM reports").fetchone()[0]
    assert service.report_view(conn, report_id)["report"]["facts_reviewed"] is True
    service.submit(conn, "603606", "failed-feedback", parent_report_id=report_id,
                   feedback={"location": "risks[1]", "text": "负债率上升能否全部解释为银行借款增加？"})
    context = _research_context(conn, "603606.SH", report_id)
    assert context[0]["basis"] == "facts"
    assert context[0]["claims_to_recheck"]["risks"][1] == facts["risks"][1]
    assert context[0]["claims_to_recheck"]["risks"] == captured["report"]["risks"][:2]
    conn.close()


def test_failed_review_does_not_endorse_unreviewed_claims():
    captured = json.loads((Path(__file__).parent / "data/603606-review.json").read_text())
    issues = _review_issues(json.dumps({"issues": captured["problems"]}, ensure_ascii=False))
    # A completed semantic review preserves the two unflagged captured claims.
    assert _verified_facts(captured["report"], issues, reviewed=True)["risks"] == captured["report"]["risks"][:2]
    # Mechanical failure skips review, and a repaired report cannot inherit prior endorsement.
    assert _verified_facts(captured["report"], issues) == {}
    # Truncated or structurally incomplete review output cannot endorse any claims.
    response = json.dumps({"issues": captured["problems"]}, ensure_ascii=False)
    for text in (response[:-1], json.dumps({"issues": [{"where": issues[0]["where"]}]})):
        failures = _review_issues(text)
        assert failures[0]["where"] == "review"
        assert _verified_facts(captured["report"], failures, reviewed=True) == {}


def test_failed_report_api_preserves_review_status_without_inference(tmp_path):
    captured = json.loads((Path(__file__).parent / "data/603606-review.json").read_text())
    conn = db.connect(tmp_path / "app.db")
    for reviewed in (None, False, True):
        run_id = db.create_run(conn, "603606.SH", status="failed")
        body = {"facts": captured["report"], "problems": captured["problems"]}
        if reviewed is not None:
            body["facts_reviewed"] = reviewed
        db.save_report(conn, run_id, "603606.SH", "东方电缆", "2026-06-30", False, None, body)
        report_id = conn.execute("SELECT max(id) FROM reports").fetchone()[0]
        assert service.report_view(conn, report_id)["report"]["facts_reviewed"] is reviewed
    conn.close()


@pytest.mark.parametrize("where", [
    "report", "global", "unknown", "summary", "missing", "risks[3]", "business[0]",
    "risks[2] / global", "risks[2] / summary", "risks[2] / risks[3]", "risks[-1]", "risks[2] trailing text",
])
def test_unlocatable_review_findings_preserve_no_claims(where):
    # Structural boundary cases, not captured model findings or synthetic financial evidence.
    captured = json.loads((Path(__file__).parent / "data/603606-review.json").read_text())
    issue = captured["problems"][0] | {"where": where}
    assert _verified_facts(captured["report"], [issue], reviewed=True) == {}


def test_review_targets_are_canonical_and_empty_facts_are_not_endorsed():
    captured = json.loads((Path(__file__).parent / "data/603606-review.json").read_text())
    # Structural variations of the captured location, using its unchanged real claims.
    issue = captured["problems"][0] | {"where": "risks[02] / missing"}
    assert _verified_facts(captured["report"], [issue], reviewed=True)["risks"] == captured["report"]["risks"][:2]
    issue = issue | {"where": "risks"}
    facts = _verified_facts(captured["report"], [issue], reviewed=True)
    assert facts == {}
    # Render the actual failure-notice fragment without inventing report metadata.
    template = (Path(__file__).parents[1] / "a_share_research/templates/report.html").read_text()
    notice = template[template.index("{% if report.passed %}"):template.index("{% if market %}")]
    html = Environment(autoescape=True).from_string(notice).render(
        report={"passed": False}, body={"facts": facts, "facts_reviewed": True})
    assert "也未保留可单独展示的事实条目" in html
    assert "仅保留本次" not in html
