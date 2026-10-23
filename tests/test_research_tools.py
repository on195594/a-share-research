"""New calculations and task boundaries using captured Moutai data and disclosed excerpts."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import db, service
from a_share_research.research import Material, check, financial_table, _research_context, _verified_facts
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

    # Direct calculations on verified PDF disclosure figures (exact quote token + unit conversion).
    pdf_2025 = {"source": "D0", "page": 9, "quote": "销售费用 7,253,499,600.68 5,639,300,059.49 28.62",
                "value": "7,253,499,600.68", "unit": "元", "period": "2025-12-31", "label": "销售费用"}
    pdf_2024 = pdf_2025 | {"value": "5,639,300,059.49", "period": "2024-12-31"}
    share = calculate(MATERIAL.table, "销售费用占营收比", "ratio_pct",
                      pdf_2025, {"ref": "T.revenue", "period": "2025-12-31"}, material=MATERIAL)
    assert share["values"] == {"result": "4.22"}
    yoy = calculate(MATERIAL.table, "销售费用同比", "growth_pct", pdf_2025, pdf_2024, material=MATERIAL)
    assert yoy["values"] == {"result": "28.62"}
    with pytest.raises(ValueError, match="原数"):
        calculate(MATERIAL.table, "缩放或符号不符", "ratio_pct",
                  pdf_2025 | {"value": "72.53"}, {"ref": "T.revenue", "period": "2025-12-31"}, material=MATERIAL)
    with pytest.raises(ValueError, match="未见单位"):
        calculate(MATERIAL.table, "伪造单位", "ratio_pct",
                  pdf_2025 | {"unit": "亿元"}, {"ref": "T.revenue", "period": "2025-12-31"}, material=MATERIAL)
    with pytest.raises(ValueError, match="同一指标"):
        calculate(MATERIAL.table, "不同科目同比", "growth_pct",
                  pdf_2025, pdf_2024 | {"label": "管理费用"}, material=MATERIAL)


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
