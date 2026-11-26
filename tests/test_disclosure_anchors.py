"""Forecast/express figures and market ratio notes registered as program facts, on captured TuShare rows.

Mutations below are explicitly marked variants of the captures; they never add financial facts.
"""
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import checking, db, disclosure_checks, evidence, market, research
from a_share_research.material import Material, financial_table
from a_share_research.research_tools import calculate
from a_share_research.sources import Document
from support.financials import FINANCIAL_DATA

ROWS = json.loads((Path(__file__).parent / "data/disclosure-checks.json").read_text())["tushare"]
FORECAST, EXPRESS = "603960.SH", "605016.SH"


class Replay:
    """The captured rows of each endpoint, answered like the TuShare client."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, api, **params):
        self.calls.append((api, params))
        rows = self.rows[api].get(params["ts_code"], [])
        if "period" in params:
            rows = [r for r in rows if r["end_date"] == params["period"]]
        return pd.DataFrame(rows)


def material(code):
    # An identity-only shell: the announcement rows are the only figures under test.
    doc = Document(code, code, "2025年年度报告", "2025-12-31", "2026-04-24", "")
    return Material(code, code, doc, {}, {}, [], "2026-04-30", {"catalog": {"D0": doc.info()}, "gaps": []})


def event(code, announced, title="2025年度业绩预告"):
    # Title is a marked variant carrying the captured rows' period; the date is the captured ann_date.
    return {"id": 1, "code": code[:6], "title": title, "announced": announced, "kind": "forecast",
            "url": f"https://static.cninfo.com.cn/finalpage/{announced}/1.PDF"}


def listed(tmp_path, monkeypatch, code, rows=ROWS, announced="2026-01-23", title="2025年度业绩预告"):
    client = Replay(rows)
    monkeypatch.setattr(market, "client", lambda: client)
    held = material(code)
    with closing(db.connect(tmp_path / "app.db")) as conn:
        # Scan verdicts must never feed the research material: reading that table fails the test.
        conn.set_authorizer(lambda action, table, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_READ and table == "disclosure_checks" else sqlite3.SQLITE_OK)
        result, = research.scan_events(conn, held, [event(code, announced, title)])
    return held, result, client


def test_forecast_bounds_become_citable_rows(tmp_path, monkeypatch):
    held, item, client = listed(tmp_path, monkeypatch, FORECAST)
    facts = item["disclosure_facts"]
    assert "reason" not in facts and [f["value"] for f in facts["rows"]] == ["0.2691", "0.3341"]
    low, high = held.table
    assert [r["id"] for r in held.table] == [f["ref"] for f in facts["rows"]]
    assert low["id"] == "X.forecast.603960.2025-12-31.2026-01-23.net_profit_min"
    assert low["values"] == low["exact_values"] == {"2025-12-31": "0.2691"} and low["unit"] == "亿元"
    assert low["source"]["api"] == "forecast" and low["source"]["announced"] == "2026-01-23"
    assert "±0.0001 亿元" in low["note"] and "预减" in high["label"]
    width = calculate(held.table, "预告区间宽度", "difference", *({"ref": r["id"], "period": "2025-12-31"}
                                                            for r in (high, low)))
    assert Decimal(width["exact_values"]["result"]) == Decimal("0.0650")
    cites = [{"ref": r["id"], "period": "2025-12-31"} for r in (low, high)]
    supported = checking._candidates(cites, held)
    assert all(checking._supported(value, decimals, supported)
               for value, decimals in checking._checked_figures("预告2,691万元至3,341万元。", held, cites))
    assert low["note"] in held.prompt_block()
    # The year has no income query without an express row, and no scan judgment travels.
    assert [api for api, _ in client.calls] == ["forecast", "express"]
    assert not {"deviated", "missed", "inside"} & set(json.dumps(held.snapshot()).split('"'))
    assert Material.from_snapshot(json.loads(json.dumps(held.snapshot()))).table == held.table


@pytest.mark.parametrize("variant,note", [(None, "口径：与营业总收入一致"), ("mismatch", "口径不符"),
                                          ("no-income", "口径未能核实")])
def test_express_points_carry_the_revenue_basis(tmp_path, monkeypatch, variant, note):
    rows = deepcopy(ROWS)
    if variant == "mismatch":  # Marked variant: operating revenue differs from total revenue.
        for row in rows["income"][EXPRESS]:
            row["revenue"] = row["total_revenue"] - 1
    if variant == "no-income":
        rows["income"][EXPRESS] = []
    held, item, _ = listed(tmp_path, monkeypatch, EXPRESS, rows, title="2025年度业绩快报")
    table = {r["source"]["field"]: r for r in held.table}
    assert table["n_income"]["exact_values"] == {"2025-12-31": "3.657832"}
    assert table["revenue"]["exact_values"] == {"2025-12-31": "13.790537"}
    assert note in table["revenue"]["note"] and note in item["disclosure_facts"]["rows"][1]["note"]


def test_same_identity_forecast_type_conflict_in_either_order(tmp_path, monkeypatch):
    rows = deepcopy(ROWS)
    variants = rows["forecast"][FORECAST]
    variants.append(variants[0] | {"type": "预增"})  # Same bounds, opposite classification.
    for _ in range(2):
        observation, = disclosure_checks.normalize_forecast(variants, FORECAST[:6])
        assert observation["type"] is None and observation["type_conflict"]
        held, item, _ = listed(tmp_path, monkeypatch, FORECAST, rows)
        assert not held.table and not item["disclosure_facts"]["rows"]
        assert disclosure_checks.IDENTITY_CONFLICT_REASON in item["disclosure_facts"]["reason"]
        variants.reverse()


def _conflict(rows):  # Marked variant: a second, different value under the same disclosure identity.
    rows["forecast"][FORECAST].append(rows["forecast"][FORECAST][0] | {"net_profit_min": 2692.0})


@pytest.mark.parametrize("code,announced,mutate,reason", [
    (FORECAST, "2026-01-24", None, "没有公告日 2026-01-24"),
    (FORECAST, "2026-01-23", _conflict, "同一披露版本内容冲突"),
    ("688082.SH", "2026-01-23", None, "未给出可用数值"),
])
def test_unmatched_or_unusable_estimates_add_no_figures(tmp_path, monkeypatch, code, announced, mutate, reason):
    rows = deepcopy(ROWS)
    if mutate:
        mutate(rows)
    held, item, _ = listed(tmp_path, monkeypatch, code, rows, announced)
    facts = item["disclosure_facts"]
    assert held.table == [] and facts["rows"] == []
    assert reason in facts["reason"] and "read_disclosure" in facts["reason"]
    assert held.extra["gaps"] == [] and item["financial_checks"]


@pytest.mark.parametrize("fault", ["network", "foreign-company"])
def test_unavailable_estimates_leave_a_gap_and_do_not_block(tmp_path, monkeypatch, fault):
    rows = deepcopy(ROWS)
    if fault == "network":
        rows = {}  # Any query raises, as an upstream failure would.
    else:  # Marked variant: the endpoint answers with another company's row.
        rows["forecast"][FORECAST] = ROWS["forecast"]["300024.SZ"]
    held, item, _ = listed(tmp_path, monkeypatch, FORECAST, rows)
    assert held.table == [] and "read_disclosure" in item["disclosure_facts"]["reason"]
    assert held.extra["gaps"] == ["603960 2025-12-31：未能取得结构化预告/快报数据，请调用 read_disclosure 核对原文"]


@pytest.mark.parametrize("code,title", [(FORECAST, "2025年度业绩快报"), (EXPRESS, "2025年度业绩预增")])
def test_same_day_wrong_disclosure_kind_is_a_gap(tmp_path, monkeypatch, code, title):
    held, item, _ = listed(tmp_path, monkeypatch, code, title=title)
    assert not held.table and "read_disclosure" in item["disclosure_facts"]["reason"]


def test_title_selects_its_kind_when_both_kinds_match(tmp_path, monkeypatch):
    rows = deepcopy(ROWS)
    # Same-day express variant alongside the captured forecast.
    rows["express"][FORECAST] = [rows["express"][EXPRESS][0] | {"ts_code": FORECAST}]
    for title, kind in [("2025年度业绩预告", "forecast"), ("2025年度业绩快报", "express")]:
        held, item, _ = listed(tmp_path, monkeypatch, FORECAST, rows, title=title)
        assert item["disclosure_facts"]["rows"] and {r["source"]["api"] for r in held.table} == {kind}
    held, item, _ = listed(tmp_path, monkeypatch, FORECAST, rows, title="2025年度业绩修正")
    assert not held.table and "匹配到 2 条" in item["disclosure_facts"]["reason"]


def test_unresolved_period_links_to_original_without_fetching(tmp_path, monkeypatch):
    held, item, client = listed(tmp_path, monkeypatch, FORECAST, title="2025年业绩预告")
    facts = item["disclosure_facts"]
    assert not held.table and not client.calls and facts["period"] is None
    assert facts["rows"] == [] and f"read_disclosure(source={item['source']})" in facts["reason"]


def test_revenue_basis_is_company_and_period_specific(tmp_path, monkeypatch):
    rows = deepcopy(ROWS)
    target = rows["income"][EXPRESS][0]
    # Later-period mismatch and foreign-company variants of the captured statement.
    later = target | {"end_date": "20260630", "revenue": target["total_revenue"] - 1}
    rows["income"][EXPRESS].append(later)
    assert disclosure_checks.revenue_basis_matches(rows["income"][EXPRESS], code=EXPRESS[:6], period="2025-12-31") is True
    held, _, client = listed(tmp_path, monkeypatch, EXPRESS, rows, title="2025年度业绩快报")
    assert "按 2025-12-31 合并利润表核对" in held.table[-1]["note"]
    assert ("income", {"ts_code": EXPRESS, "period": "20251231", "report_type": "1"}) in client.calls
    rows["income"][EXPRESS] = [later]
    assert disclosure_checks.revenue_basis_matches([later], code=EXPRESS[:6], period="2025-12-31") is None
    held, _, client = listed(tmp_path, monkeypatch, EXPRESS, rows, title="2025年度业绩快报")
    assert client.calls[-1] == ("income", {"ts_code": EXPRESS, "report_type": "1"})
    assert "目标期尚未核实" in held.table[-1]["note"] and "2026-06-30" in held.table[-1]["note"]
    rows["income"][EXPRESS] = [target | {"ts_code": FORECAST}]
    held, item, _ = listed(tmp_path, monkeypatch, EXPRESS, rows, title="2025年度业绩快报")
    assert not held.table and "read_disclosure" in item["disclosure_facts"]["reason"]


def market_table():
    fin = {k: pd.DataFrame(v) for k, v in FINANCIAL_DATA["financials"].items()}
    return financial_table(fin, FINANCIAL_DATA["market"])


def test_market_ratio_rows_state_their_basis_and_stay_citable():
    table = market_table()
    rows = {r["id"]: r for r in table}
    day = FINANCIAL_DATA["market"]["trade_date"]
    latest = max(p for r in table if r["id"] == "T.revenue" for p in r["values"])
    assert {r["id"] for r in table if "basis_meta" in r} == {"M.pb", "M.dv_ttm"}
    assert rows["M.pb"]["basis_meta"] == {"ratio": "pb", "latest_period": latest}
    held = material("600519.SH")
    held.table = table
    block = held.prompt_block()
    assert f"本表最新财报期 {latest}" in block and "BPS" in block
    assert "不等于单个年报期的静态股息率" in block and "逐笔实施分红" in block
    for key in ("M.pb", "M.dv_ttm"):  # Quoting the market fact alone stays valid.
        cites = [{"ref": key, "period": day}]
        value = rows[key]["values"][day]
        assert evidence.citation_errors(cites, held.source_registry(), table) == []
        assert all(checking._supported(v, d, checking._candidates(cites, held))
                   for v, d in checking._checked_figures(f"当前为{value}。", held, cites))


@pytest.mark.parametrize("ratio", ["M.pb", "M.dv_ttm"])
@pytest.mark.parametrize("operation", ["difference", "sum", "change_pct", "growth_pct"])
def test_market_ratios_cannot_mix_with_amounts(ratio, operation):
    table = market_table()
    day = FINANCIAL_DATA["market"]["trade_date"]
    rows = table + [{**r, "id": f"P600519.{r['id']}"} for r in table if r["id"] == ratio]  # peer-prefixed copy
    for ref in (ratio, f"P600519.{ratio}"):
        with pytest.raises(ValueError, match=rf"单位不同.*{ratio} 是.*(ratio|ratio_pct)"):
            calculate(rows, "混用", operation, {"ref": ref, "period": day}, {"ref": "M.total_mv", "period": day})


def test_compatible_market_ratio_arithmetic_is_kept():
    table = market_table()
    day = FINANCIAL_DATA["market"]["trade_date"]
    book = calculate(table, "市值除以市净率", "ratio", {"ref": "M.total_mv", "period": day}, {"ref": "M.pb", "period": day})
    assert book["unit"] == "亿元"
    spread = calculate(table, "市盈率减市净率", "difference", {"ref": "M.pe_ttm", "period": day}, {"ref": "M.pb", "period": day})
    assert spread["unit"] == "倍"
    with pytest.raises(ValueError) as error:  # Unrelated unit mismatches carry no market guidance.
        calculate(table, "无关", "difference", {"ref": "M.pe_ttm", "period": day}, {"ref": "M.close", "period": day})
    assert "M." not in str(error.value)
