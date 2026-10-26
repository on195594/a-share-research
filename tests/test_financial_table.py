"""Financial rows from the real 2026-10-22 TuShare response for 贵州茅台."""

import json
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import sources
from a_share_research.research import Material, financial_table
from test_check import MATERIAL

DATA = json.loads((Path(__file__).parent / "data" / "600519-financials.json").read_text())


def test_missing_financial_token_is_a_source_error(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(sources.SourceError, match="TuShare fina_indicator.*TUSHARE_TOKEN"):
        sources.financials("002648.SZ")


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
    # This captured response only contains annual rows and the latest half year.
    assert list(rows["T.roe"]["values"]) == ["2023-12-31", "2024-12-31", "2025-12-31", "2026-06-30"]

    frames["indicator"] = frames["indicator"].iloc[:0]
    table = financial_table(frames, DATA["market"])
    material = Material(MATERIAL.code, MATERIAL.name, MATERIAL.doc, MATERIAL.pages,
                        MATERIAL.raw_pages, table, DATA["market"]["trade_date"])
    assert "[T.roe] 加权ROE（%）：缺失" in material.prompt_block()


def test_three_statement_rows_and_missing_components_from_real_disclosures():
    # FY2025 annual-report values; raw TuShare captures retain units in yuan.
    expected = {
        '002648': {'core_profit': '62.92', 'capex': '26.60', 'fcf': '69.47', 'cash': '74.60',
                   'receivables': '6.63', 'inventory': '51.99', 'contract_liab': '7.11', 'goodwill': '0.44'},
        '600519': {'core_profit': '822.93', 'capex': '31.28', 'fcf': '583.95', 'cash': '516.91',
                   'receivables': '0.03', 'inventory': '614.27', 'contract_liab': '80.07'},
    }
    for code, values in expected.items():
        captured = json.loads((Path(__file__).parent / 'data' / f'{code}-three-statements.json').read_text())
        frames = {k: pd.DataFrame(v) for k, v in captured['financials'].items()}
        table = {r['id']: r for r in financial_table(frames, captured['market'])}
        for key, value in values.items():
            assert table[f'T.{key}']['values']['2025-12-31'] == value
        # Real absent bond/loan components must not become zero or an incomplete total.
        assert '2025-12-31' not in table['T.interest_debt']['values']
        if code == '600519':
            assert '2025-12-31' not in table['T.goodwill']['values']
        else:
            # Satellite's 2021 report has all four debt components, unlike FY2025.
            old = {k: v[v['end_date'] <= '2021-12-31'] for k, v in frames.items()}
            total = next(r for r in financial_table(old, {}) if r['id'] == 'T.interest_debt')
            assert total['values'] == {'2021-12-31': '164.15'}
