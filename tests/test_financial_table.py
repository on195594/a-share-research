"""Financial rows from the real 2026-10-22 TuShare response for 贵州茅台."""

import json
from pathlib import Path

import pandas as pd

from a_share_research.research import Material, financial_table
from test_check import MATERIAL


DATA = json.loads((Path(__file__).parent / "data" / "600519-financials.json").read_text())


def test_indicators_survive_the_providers_null_statement_type():
    frames = {name: pd.DataFrame(rows) for name, rows in DATA["financials"].items()}
    rows = {row["id"]: row for row in financial_table(frames, DATA["market"])}
    expected = {
        "T.revenue_yoy": "-1.21", "T.profit_yoy": "-4.53", "T.core_profit_yoy": "-4.58",
        "T.gross_margin": "91.18", "T.roe": "32.53", "T.debt_ratio": "16.42",
    }
    for key, value in expected.items():
        assert rows[key]["unit"] == "%"
        assert rows[key]["values"]["2025-12-31"] == value
    assert rows["T.revenue"]["label"] == "营业总收入"
    assert rows["T.revenue_yoy"]["label"] == "营业收入同比"
    assert rows["T.revenue"]["values"]["2025-12-31"] == "1720.54"
    assert rows["T.net_profit"]["values"]["2025-12-31"] == "823.20"
    assert rows["T.op_cash"]["values"]["2025-12-31"] == "615.22"
    assert list(rows["T.roe"]["values"]) == ["2023-12-31", "2024-12-31", "2025-12-31", "2026-06-30"]


def test_missing_indicator_rows_stay_missing_in_material():
    frames = {name: pd.DataFrame(rows) for name, rows in DATA["financials"].items()}
    frames["indicator"] = frames["indicator"].iloc[:0]
    table = financial_table(frames, DATA["market"])
    indicator = next(row for row in table if row["id"] == "T.roe")
    assert indicator["values"] == {}
    material = Material(MATERIAL.code, MATERIAL.name, MATERIAL.doc, MATERIAL.pages,
                        MATERIAL.raw_pages, table, DATA["market"]["trade_date"])
    assert "[T.roe] 加权ROE（%）：缺失" in material.prompt_block()
    assert Material.from_snapshot(material.snapshot()).table == table
