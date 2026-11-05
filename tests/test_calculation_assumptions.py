"""Real failed telecom/Gree inputs; mutations probe the same evidence boundary."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
import pytest

from a_share_research import checking, valuation
from a_share_research.material import Material
from a_share_research.research_tools import calculate
from a_share_research.sources import Document

DATA = json.loads((Path(__file__).parent / "data/research-calculations-20261105.json").read_text())


def material(code):
    sample = DATA[code]
    base = sample["sources"].get("D0", {})
    return Material(code, sample["document"]["name"], Document(**sample["document"]),
                    base.get("pages", {}), base.get("raw_pages", {}), deepcopy(sample["table"]),
                    sample["market_date"], {"sources": {k: v for k, v in sample["sources"].items() if k != "D0"}})


def stress():
    m = material("000651")
    request = DATA["000651"]["requests"][0]
    row = calculate(m.table, request["label"], "multiply", request["left"],
                    {"assumption": "利润下降20%，保留80%", "value": "0.8", "unit": "倍"})
    m.table.append(row)
    return m, row


def test_real_gree_twenty_percent_stress_and_chain_preserve_assumptions():
    m, row = stress()
    base = next(r for r in m.table if r["id"] == "T.net_profit")["exact_values"]["2025-12-31"]
    assert Decimal(row["exact_values"]["result"]) == Decimal(base) * Decimal("0.8")
    assert row["unit"] == "亿元"
    second = calculate(m.table, "连续两次下降20%", "multiply", {"ref": row["id"]},
                       {"assumption": "次年再下降20%", "value": "0.8", "unit": "倍"})
    assert Decimal(second["exact_values"]["result"]) == Decimal(base) * Decimal("0.64")
    assert len(second["calculation"]["assumptions"]) == 2
    # The original rejected division now works only as a clearly marked dimensionless assumption.
    divided = calculate(m.table, "利润除以1.25", "ratio", DATA["000651"]["requests"][0]["left"],
                        {"assumption": "除以1.25，利润保留80%", "value": "1.25", "unit": "倍"})
    assert divided["exact_values"] == row["exact_values"] and divided["unit"] == "亿元"
    m.table.append(second)
    restored = Material.from_snapshot(json.loads(json.dumps(m.snapshot())))
    assert restored.table[-1]["calculation"]["assumptions"] == second["calculation"]["assumptions"]
    second["calculation"]["assumptions"][0]["value"] = "0"
    assert row["calculation"]["assumptions"][0]["value"] == "0.8"


@pytest.mark.parametrize("mutation", [
    {"assumption": ""}, {"assumption": " "}, {"value": True}, {"value": "NaN"}, {"value": "Infinity"},
    {"unit": ""}, {"ref": "T.net_profit"}, {"source": "D0"}, {"quote": "0.8"}, {"page": 1},
])
def test_assumption_cannot_impersonate_a_disclosure(mutation):
    m = material("000651")
    operand = {"assumption": "利润保留系数", "value": "0.8", "unit": "倍"} | mutation
    with pytest.raises(ValueError, match="假设输入"):
        calculate(m.table, "压力测试", "multiply", DATA["000651"]["requests"][0]["left"], operand)


def test_unattributed_literal_and_mismatched_units_are_still_rejected():
    m = material("000651")
    with pytest.raises(ValueError, match="计算输入须为"):
        calculate(m.table, **DATA["000651"]["requests"][0])
    with pytest.raises(ValueError, match="无量纲"):
        calculate(m.table, "不能把金额当系数", "multiply", DATA["000651"]["requests"][0]["left"],
                  {"assumption": "错误量纲", "value": "0.8", "unit": "亿元"})


@pytest.mark.parametrize("case", DATA["601728"]["requests"])
def test_real_telecom_parenthesized_lease_outflow_keeps_its_negative_sign(case):
    m = material("601728")
    row = calculate(m.table, **case, material=m)
    assert Decimal(row["calculation"]["inputs"][1]["value"]) < 0
    assert "assumptions" not in row["calculation"]
    for mutation in ({"value": str(abs(Decimal(str(case["right"]["value"]))))},
                     {"value": "-1"}, {"unit": "张"}, {"quote": "(1,234.56)"}):
        with pytest.raises(ValueError):
            calculate(m.table, **(case | {"right": case["right"] | mutation}), material=m)


@pytest.mark.parametrize("code", DATA)
def test_scenario_tool_exposes_each_downside_and_matches_existing_pricing(code):
    sample = DATA[code]
    rows = valuation.program_rows(sample["valuation"], sample["table"], sample["market_date"])
    calc = valuation.compute(sample["valuation"] | {"rationale": [{"text": "复算捕获的工具输入", "cites": []}]},
                             sample["table"], sample["market_date"])
    assert len(rows) == 8 and len({r["id"] for r in rows}) == 8
    assert Decimal(rows[0]["exact_values"]["result"]) == Decimal(str(calc["v2"]["support_below"]))
    for name in valuation.SCENARIOS:
        annual = next(r for r in rows if r["calculation"].get("scenario") == name and r["calculation"]["metric"] == "annual_return")
        assert Decimal(annual["exact_values"]["result"]) == Decimal(str(calc["scenarios"][name]["annual_return"])) * 100
        assert annual["calculation"]["valuation"] == valuation.pricing_inputs(sample["valuation"])
        assert annual["calculation"]["horizon_years"] == calc["horizon"]


@pytest.mark.parametrize("code", DATA)
@pytest.mark.parametrize("years", [3, 5])
def test_explicit_long_horizon_and_conservative_loss_are_computed_not_just_formulas(code, years):
    # Reuse actual captured scenario inputs, explicitly holding its terminal profit/PE constant.
    # Dividend assumption is an explicit extension, not a historical financial observation.
    m = material(code)
    sample = DATA[code]
    rows = []
    for name, scenario in sample["valuation"]["scenarios"].items():
        exit_cap = calculate(m.table, f"{name}正常化退出市值", "multiply",
                             {"assumption": f"{name}正常化退出利润", "value": scenario["profit"][-1], "unit": "亿元"},
                             {"assumption": f"{name}退出PE", "value": scenario["exit_pe"], "unit": "倍"})
        m.table.append(exit_cap)
        dividends = calculate(m.table, f"{name}累计现金分红", "multiply",
                              {"assumption": "补充测算假设每年分红按末年利润及既定派息率保持，非披露承诺",
                               "value": str(Decimal(str(scenario["profit"][-1])) * Decimal(str(sample["valuation"]["payout_pct"])) / 100), "unit": "亿元"},
                              {"assumption": "持有期间分红次数", "value": years, "unit": "倍"})
        m.table.append(dividends)
        total = calculate(m.table, f"{name}退出价值加累计分红", "sum", {"ref": exit_cap["id"]}, {"ref": dividends["id"]})
        m.table.append(total)
        row = calculate(m.table, f"{name}补充年化回报", "annual_return", {"ref": total["id"]},
                        {"ref": "M.total_mv", "period": m.market_date}, holding_years=years)
        m.table.append(row)
        with localcontext() as context:
            context.prec = 40
            inputs = row["calculation"]["normalized_inputs"]
            expected = ((Decimal(inputs[0]["value"]) / Decimal(inputs[1]["value"])) ** (Decimal(1) / years) - 1) * 100
        assert Decimal(row["exact_values"]["result"]) == expected
        assert row["unit"] == "%" and "假设" in row["calculation"]["precision"]
        assert row["calculation"]["assumptions"][-1]["value"] == str(years)
        assert float(years) in checking._candidates([{"ref": row["id"]}], m)
        rows.append(row)
    assert len(rows) == 3
    if code == "601728" and years == 3:
        assert Decimal(rows[0]["exact_values"]["result"]) < 0  # actual captured conservative assumptions


@pytest.mark.parametrize("years", [None, 0, -1, True, "NaN", "Infinity", 51])
def test_invalid_holding_horizon_is_not_silently_replaced(years):
    m = material("000651")
    with pytest.raises(ValueError, match="holding_years"):
        calculate(m.table, "期限错误", "annual_return", {"ref": "M.total_mv", "period": m.market_date},
                  {"ref": "M.total_mv", "period": m.market_date}, holding_years=years)


def test_assumptions_and_mixed_units_are_visible_in_the_existing_citation_ui():
    m, row = stress()
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "a_share_research/templates"), autoescape=True)
    html = env.from_string('{% from "_macros.html" import cites %}{{ cites(item, rows) }}').render(
        item={"cites": [{"ref": row["id"]}]}, rows={r["id"]: r for r in m.table})
    assert "假设测算，不是披露事实" in html and "利润下降20%" in html
    assert "输入单位分别为亿元、倍" in html and "计算输入统一为亿元" not in html
