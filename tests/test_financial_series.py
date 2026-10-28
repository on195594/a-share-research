"""Exact financial history checked against captured TuShare single-quarter statements."""

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from a_share_research.research import Material, financial_series, financial_table, since_prior
from a_share_research.research_tools import calculate
from test_check import MATERIAL

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = json.loads((ROOT / "tests/data/financial-series-captures.json").read_text())["companies"]


def frames(captured):
    return {key: pd.DataFrame(rows) for key, rows in captured["financials"].items()}


def build(code="000858"):
    captured = CAPTURES[code]
    return financial_series(frames(captured), captured["code"], captured["retrieved_at"])


def cell(series, kind, key, period):
    return next(row for row in series[kind]["rows"] if row["id"] == key)["values"][period]


def test_q3_and_q4_match_real_single_quarter_statements_without_rounding_inputs():
    fields = {"income": {"total_revenue": "revenue", "n_income_attr_p": "net_profit"},
              "cashflow": {"n_cashflow_act": "op_cash", "c_pay_acq_const_fiolta": "capex"}}
    for code, captured in CAPTURES.items():
        series = build(code)
        assert series["annual"]["periods"] == [f"{year}-12-31" for year in range(2021, 2026)]
        assert series["quarterly"]["periods"] == ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30",
                                                   "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
        for statement, rows in captured["quarter_type2"].items():
            for row in rows:
                p = row["end_date"]
                p = f"{p[:4]}-{p[4:6]}-{p[6:]}"
                for field, key in fields[statement].items():
                    assert Decimal(cell(series, "quarterly", key, p)["value"]) == Decimal(str(row[field]))
        assert {r["id"] for r in series["balance"]["rows"]} == {
            "cash", "interest_debt", "receivables", "inventory", "contract_liab", "goodwill"}
    assert cell(build(), "quarterly", "revenue", "2026-09-30")["display"] == "44.42"
    assert cell(build(), "quarterly", "net_profit", "2026-09-30")["display"] == "9.58"


def test_ttm_has_exact_inputs_and_annual_ttm_uses_the_full_year():
    series = build()
    ttm = cell(series, "ttm", "revenue", "2026-09-30")
    assert ttm["value"] == "42749256079.60"
    assert [(i["period"], i["weight"]) for i in ttm["inputs"]] == [
        ("2026-09-30", 1), ("2025-12-31", 1), ("2025-09-30", -1)]
    assert sum(Decimal(i["value"]) * i["weight"] for i in ttm["inputs"]) == Decimal(ttm["value"])
    assert cell(series, "ttm", "fcf", "2026-09-30")["value"] == "368330585.55"
    captured = CAPTURES["000858"]
    fin = {k: frame[frame.end_date <= "2025-12-31"] for k, frame in frames(captured).items()}
    annual = financial_series(fin, captured["code"], captured["retrieved_at"])
    assert cell(annual, "ttm", "revenue", "2025-12-31")["value"] == cell(annual, "annual", "revenue", "2025-12-31")["value"]
    assert len(cell(annual, "ttm", "revenue", "2025-12-31")["inputs"]) == 1


def test_corrections_propagate_but_same_value_duplicates_do_not_imply_a_correction():
    series = build()
    quarter = cell(series, "quarterly", "revenue", "2025-09-30")
    assert quarter["corrected"] and all(i["corrected"] for i in quarter["inputs"])
    assert cell(series, "ttm", "revenue", "2026-09-30")["corrected"]
    assert cell(series, "quarterly", "core_profit", "2025-06-30")["corrected"]
    assert all(i["f_ann_date"] is None for i in cell(series, "quarterly", "core_profit", "2025-06-30")["inputs"])
    # Same-value cashflow rows still carry a later actual announcement date for H1 2025.
    assert cell(series, "quarterly", "fcf", "2025-06-30")["corrected"]
    # H1 2026 has identical duplicate values with update_flag 0/1 and no later actual date.
    for key in ("revenue", "op_cash", "core_profit"):
        value = cell(series, "quarterly", key, "2026-06-30")
        assert not value["corrected"]
        assert all(i["retrieved_at"] == CAPTURES["000858"]["retrieved_at"] for i in value["inputs"])
    latest = cell(series, "quarterly", "revenue", "2026-09-30")
    assert not latest["corrected"] and latest["yoy"]["base"]["corrected"]


def test_missing_periods_and_nonpositive_bases_remain_missing():
    captured = json.loads((ROOT / "tests/data/600519-three-statements.json").read_text())
    fin = {k: frame[frame.end_date <= "2023-06-30"] for k, frame in frames(captured).items()}
    series = financial_series(fin, "600519.SH", "2026-10-26")
    # The actual capture starts at FY2021; it has no Q3 needed to derive Q4.
    assert len(series["quarterly"]["periods"]) == 8
    missing = cell(series, "quarterly", "revenue", "2021-12-31")
    assert missing["value"] is None and missing["missing"] == ["2021-09-30"]
    negative = cell(series, "quarterly", "op_cash", "2023-03-31")
    assert negative["yoy"]["base"]["value"] == "-6876059093.79" and negative["yoy"]["value"] is None
    assert cell(build(), "balance", "interest_debt", "2025-12-31")["value"] is None
    assert cell(build(), "balance", "contract_liab", "2025-03-31")["value"] is None


def test_preceding_cumulative_periods_support_the_existing_calculator_and_backfill():
    table = financial_table(frames(CAPTURES["000858"]), {})
    cash = next(row for row in table if row["id"] == "T.op_cash")
    assert {"2025-03-31", "2025-06-30", "2025-09-30", "2026-03-31", "2026-06-30", "2026-09-30"} <= cash["values"].keys()
    calculated = calculate(table, "Q3经营现金流", "difference", {"ref": "T.op_cash", "period": "2026-09-30"},
                           {"ref": "T.op_cash", "period": "2026-06-30"})
    # T.* retains its established two-decimal precision; the series calculates from raw yuan.
    assert calculated["values"] == {"result": "22.01"}
    assert cell(build(), "quarterly", "op_cash", "2026-09-30")["display"] == "22.00"
    captured = json.loads((ROOT / "tests/data/600519-three-statements.json").read_text())
    old = json.loads((ROOT / "tests/data/since-prior-600519.json").read_text())["tables"]["8"]
    diff = since_prior(old, financial_table(frames(captured), captured["market"]))
    assert diff["backfilled"] == ["2025-03-31", "2026-03-31"] and diff["disclosed"] == []


def test_series_survives_snapshot_but_stays_outside_model_evidence_and_renders_latest_first():
    material = Material.from_snapshot(json.loads(json.dumps(MATERIAL.snapshot())))
    material.extra["gaps"] = []
    prompt = material.prompt_block()
    captured = json.loads((ROOT / "tests/data/600519-three-statements.json").read_text())
    series = financial_series(frames(captured), material.code, "2026-10-26")
    material.extra["series"] = series
    restored = Material.from_snapshot(json.loads(json.dumps(material.snapshot())))
    assert restored.extra["series"] == series and restored.prompt_block() == prompt
    assert "series" not in Material.from_snapshot(MATERIAL.snapshot()).extra
    env = Environment(loader=FileSystemLoader(ROOT / "a_share_research/templates"), autoescape=select_autoescape())
    template = env.get_template("_series.html")
    html = template.render(body={"series": build()})
    assert html.count('<details open>') == 4
    assert html.index('2026 Q3') < html.index('2026 Q2')
    assert '44.42' in html and '同比 -37.68%' in html and '基期含更正' in html
    assert '本报告未记录经营序列' in template.render(body={})
