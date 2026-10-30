"""Exact financial history checked against captured TuShare single-quarter statements."""

import copy
import functools
import json
import re
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from a_share_research.research import Material, financial_series, financial_table
from a_share_research.reading import since_prior
from a_share_research.research_tools import calculate
from support.reports import MATERIAL

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = json.loads((ROOT / "tests/data/financial-series-captures.json").read_text())["companies"]


def frames(captured):
    return {key: pd.DataFrame(rows) for key, rows in captured["financials"].items()}


@functools.lru_cache(maxsize=None)
def _build_cached(code="000858"):
    captured = CAPTURES[code]
    return financial_series(frames(captured), captured["code"], captured["retrieved_at"])


def build(code="000858"):
    return copy.deepcopy(_build_cached(code))


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
                    value = cell(series, "quarterly", key, p)
                    assert sum(Decimal(i["value"]) * i["weight"] for i in value["inputs"]) == Decimal(str(row[field]))
                    if code == "000858" and statement == "cashflow" and p == "2025-12-31":
                        # Matching numbers alone do not reconcile the older Q3 publication version.
                        assert value["value"] is None and value["unreconciled"] == {"2025-09-30": "2026-04-30"}
                    else:
                        assert Decimal(value["value"]) == Decimal(str(row[field]))
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
    cash = cell(series, "ttm", "fcf", "2026-09-30")
    assert cash["value"] is None and cash["unreconciled"] == {"2025-09-30": "2026-04-30"}
    assert sum(Decimal(i["value"]) * i["weight"] for i in cash["inputs"]) == Decimal("368330585.55")
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


def test_partial_restatements_cannot_mix_cumulative_versions_or_yoy_bases():
    series = build()
    for key in ("revenue", "net_profit", "op_cash", "capex", "fcf"):
        quarter = cell(series, "quarterly", key, "2025-06-30")
        assert quarter["value"] is None and quarter["missing"] == []
        assert quarter["unreconciled"] == {"2025-03-31": "2026-04-30"}
        assert all(i["value"] is not None for i in quarter["inputs"])
        current = cell(series, "quarterly", key, "2026-03-31")
        assert current["value"] is not None and current["yoy"]["value"] is None
        assert current["yoy"]["base"]["unreconciled"] == quarter["unreconciled"]
    captured = CAPTURES["000858"]
    fin = {k: frame[frame.end_date <= "2026-03-31"] for k, frame in frames(captured).items()}
    cutoff = financial_series(fin, captured["code"], captured["retrieved_at"])
    assert cell(cutoff, "ttm", "revenue", "2026-03-31")["value"] is None
    table = financial_table(frames(captured), {})
    revenue = next(r for r in table if r["id"] == "T.revenue")
    assert "2025-03-31" not in revenue["values"] and "2025-03-31" in revenue["unavailable"]
    with pytest.raises(ValueError, match="更正版本待核对"):
        calculate(table, "Q2收入", "difference", {"ref": "T.revenue", "period": "2025-06-30"},
                  {"ref": "T.revenue", "period": "2025-03-31"})
    normal = build("002648")
    assert all("unreconciled" not in value and "unreconciled" not in value.get("yoy", {}).get("base", {})
               for kind in ("annual", "quarterly", "ttm", "balance")
               for row in normal[kind]["rows"] for value in row["values"].values())


def test_preceding_cumulative_periods_support_the_existing_calculator_and_backfill():
    table = financial_table(frames(CAPTURES["000858"]), {})
    cash = next(row for row in table if row["id"] == "T.op_cash")
    assert {"2025-06-30", "2026-03-31", "2026-06-30", "2026-09-30"} <= cash["values"].keys()
    assert cash["unavailable"].keys() == {"2025-03-31", "2025-09-30"}
    calculated = calculate(table, "Q3经营现金流", "difference", {"ref": "T.op_cash", "period": "2026-09-30"},
                           {"ref": "T.op_cash", "period": "2026-06-30"})
    # Calculations and the reading series now share source precision, not rounded display inputs.
    assert calculated["values"] == {"result": "22.00"}
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
    assert '基期更正版本待核对' in html and '−134.30' not in html and '-134.30' not in html


def test_yoy_sources_show_window_outside_inputs_and_quarter_ttm_formulas():
    series = build()
    before = json.dumps(series, ensure_ascii=False)
    env = Environment(loader=FileSystemLoader(ROOT / "a_share_research/templates"), autoescape=select_autoescape())
    html = env.get_template("_series.html").render(body={"series": series})

    def source(title, period):
        group = html.split(f"<summary>{title}</summary>", 1)[1]
        return re.search(rf"<li>{period}：.*?</li>", group, re.S)[0]

    # FY2020 is outside the five annual columns, but still supplies FY2021's growth base.
    annual = source("年度", "2021-12-31")
    assert "同比基期 2020-12-31：57321059453.15 元" in annual
    assert "+ income.total_revenue（2020-12-31）：57321059453.15 元" in annual
    assert "公告 2021-04-28，实际公告 2021-04-28，更新标志 1" in annual
    quarter = source("单季", "2024-12-31")
    assert "+ income.total_revenue（2023-12-31）：83272067317.19 元" in quarter
    assert "− income.total_revenue（2023-09-30）：62536387639.06 元" in quarter
    ttm = source("TTM", "2026-09-30")
    for term in ("+ income.total_revenue（2025-09-30）：30637727654.97 元",
                 "+ income.total_revenue（2024-12-31）：89175178322.7 元",
                 "− income.total_revenue（2024-09-30）：67915580112.25 元"):
        assert term in ttm
    # Exposing the original input does not endorse a base whose version is unresolved.
    q1 = source("单季", "2026-03-31")
    assert "同比基期 2025-03-31：更正版本待核对" in q1 and "暂不计算同比" in q1
    assert "+ income.total_revenue（2025-03-31）：36940356116.35 元" in q1
    assert "同比基期" not in html.split("<summary>期末</summary>", 1)[1]
    assert json.dumps(series, ensure_ascii=False) == before


def test_yoy_source_missing_and_nonpositive_bases_are_not_replaced_or_hidden():
    captured = json.loads((ROOT / "tests/data/600519-three-statements.json").read_text())
    fin = {k: frame[frame.end_date <= "2023-06-30"] for k, frame in frames(captured).items()}
    series = financial_series(fin, "600519.SH", "2026-10-26")
    env = Environment(loader=FileSystemLoader(ROOT / "a_share_research/templates"), autoescape=select_autoescape())
    html = env.get_template("_series.html").render(body={"series": series})
    assert "基期缺少 2020-12-31 的完整输入" in html
    assert "+ cashflow.n_cashflow_act（2022-03-31）：-6876059093.79 元" in html
    assert cell(series, "quarterly", "op_cash", "2023-03-31")["yoy"]["value"] is None
