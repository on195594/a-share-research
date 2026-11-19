"""
补充测试覆盖：持仓检查边界与观察名单排序。
"""

import json
from datetime import datetime

import pytest

from a_share_research.policy.v0 import commands_admin, commands_holdings, db, domain, market_quotes
from policy_v0.helpers import (
    mock_price_quotes,
    record_valid_quote,
    seed_position,
    set_valid_fundamentals,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_extra.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
    yield str(db_file)


# ── helpers ──────────────────────────────────────────────────────────────────


def _insert_analysis(code: str, result: str = "结论", score: int | None = None):
    today = domain.cst_today()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO analysis_results (code, date, result, created_at, score) VALUES (?,?,?,?,?)",
        (code, today, result, datetime.now().isoformat(), score),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════


class TestCheckHoldingsBoundary:
    """价格恰好等于止损线时的行为（<=运算符的边界条件）"""

    def _add_holding_cost40(self):
        # cost=40 → sl15=34.000, sl20=32.000
        seed_position("600036", 40.0, 100, notes="测试")

    def test_price_exactly_at_15pct_stop_triggers_yellow(self, capsys, monkeypatch):
        self._add_holding_cost40()
        monkeypatch.setattr(domain, "is_a_share_trading_hours", lambda _now: False)
        mock_price_quotes(
            monkeypatch,
            lambda codes: {
                code: (market_quotes.PriceQuote(34.0, domain.cst_today(), "15:00:00"))
                for code in codes
            },
        )
        commands_holdings.cmd_check_holdings()
        out = capsys.readouterr().out
        assert "⚠️ 已跌破15%止损线" in out
        assert "共 1 项预警" in out

    def test_price_exactly_at_20pct_stop_triggers_red(self, capsys, monkeypatch):
        self._add_holding_cost40()
        monkeypatch.setattr(domain, "is_a_share_trading_hours", lambda _now: False)
        mock_price_quotes(
            monkeypatch,
            lambda codes: {
                code: (market_quotes.PriceQuote(32.0, domain.cst_today(), "15:00:00"))
                for code in codes
            },
        )
        commands_holdings.cmd_check_holdings()
        out = capsys.readouterr().out
        assert "🔴 已跌破20%止损线" in out
        assert "P3:incomplete(market_snapshot_incomplete)" in out
        assert "建议立即止损" not in out

    def test_price_one_cent_above_15pct_stop_is_normal(self, capsys, monkeypatch):
        """34.001 > sl15=34.000，不应触发预警"""
        self._add_holding_cost40()
        mock_price_quotes(
            monkeypatch,
            lambda codes: {
                code: (market_quotes.PriceQuote(34.001, domain.cst_today(), "15:00:00"))
                for code in codes
            },
        )
        commands_holdings.cmd_check_holdings()
        out = capsys.readouterr().out
        assert "✅ 正常" in out
        assert "无预警" in out


# ═══════════════════════════════════════════════════════════════════


class TestWatchlistRows:
    def test_needs_refresh_false_when_analysed_today(self, capsys):
        set_valid_fundamentals("600036", "招商银行", "银行", {"pe_ttm": 5.5})
        _insert_analysis("600036", score=62)
        commands_admin.cmd_watchlist(["--json"])
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["code"] == "600036"
        assert rows[0]["needs_refresh"] is False

    def test_ordering_scored_before_unscored(self, capsys):
        """Pending first analysis is prioritized ahead of an already-current row."""
        set_valid_fundamentals("000001", "平安银行", "银行", {})
        set_valid_fundamentals("600036", "招商银行", "银行", {})
        record_valid_quote("000001")
        _insert_analysis("600036", score=55)  # only 600036 has analysis today

        commands_admin.cmd_watchlist(["--json"])
        rows = json.loads(capsys.readouterr().out)
        codes = [r["code"] for r in rows]
        assert codes.index("000001") < codes.index("600036")

    def test_expired_stocks_excluded(self, capsys, monkeypatch):
        set_valid_fundamentals("600036", "招商银行", "银行", {})
        monkeypatch.setattr(domain, "is_expired", lambda *_: True)
        commands_admin.cmd_watchlist(["--json"])
        rows = json.loads(capsys.readouterr().out)
        assert rows == []

    def test_watchlist_flags_shown_in_json(self, capsys):
        set_valid_fundamentals("600036", "招商银行", "银行", {})
        _insert_analysis("600036")
        conn = db.get_db()
        conn.execute("UPDATE analysis_results SET flags=? WHERE code='600036'",
                     (json.dumps([{"level": "red", "reason": "大股东减持", "date": domain.cst_today()}],
                                 ensure_ascii=False),))
        conn.commit()
        conn.close()
        commands_admin.cmd_watchlist(["--json"])
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["flags"][0]["level"] == "red"
