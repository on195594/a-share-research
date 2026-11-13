"""
补充测试覆盖：标记、持仓移除、缓存清理、行情前缀与持仓检查边界、观察名单排序。
"""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from a_share_research.policy.v0 import commands_admin, commands_holdings, commands_monitor, db, domain, market_quotes, store
from policy_v0.helpers import (
    mock_price_quotes,
    record_valid_quote,
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


class TestSetAndClearFlag:
    def test_set_red_flag_stored(self, capsys):
        _insert_analysis("000001")
        commands_monitor.cmd_set_flag(["000001", "red", "大股东减持超5%"])
        out = capsys.readouterr().out
        assert "🔴" in out
        assert "大股东减持超5%" in out

    def test_set_yellow_flag_stored(self, capsys):
        _insert_analysis("000001")
        commands_monitor.cmd_set_flag(["000001", "yellow", "短期涨幅过大"])
        out = capsys.readouterr().out
        assert "⚠️" in out

    def test_invalid_level_exits(self):
        _insert_analysis("000001")
        with pytest.raises(SystemExit) as exc:
            commands_monitor.cmd_set_flag(["000001", "green", "理由"])
        assert exc.value.code == 1

    def test_no_analysis_record_exits(self):
        with pytest.raises(SystemExit) as exc:
            commands_monitor.cmd_set_flag(["999999", "red", "理由"])
        assert exc.value.code == 1

    def test_multiple_flags_accumulate(self, capsys):
        """set-flag must append, not overwrite — two calls yield two entries."""
        _insert_analysis("600036")
        commands_monitor.cmd_set_flag(["600036", "yellow", "第一条"])
        commands_monitor.cmd_set_flag(["600036", "red", "第二条"])
        capsys.readouterr()

        today = domain.cst_today()
        conn = db.get_db()
        row = conn.execute(
            "SELECT flags FROM analysis_results WHERE code=? AND date=?",
            ("600036", today),
        ).fetchone()
        conn.close()
        flags = json.loads(row[0])
        assert len(flags) == 2
        assert flags[0]["level"] == "yellow"
        assert flags[1]["level"] == "red"

    def test_clear_flag_removes_all(self, capsys):
        _insert_analysis("600036")
        commands_monitor.cmd_set_flag(["600036", "red", "某原因"])
        capsys.readouterr()

        commands_monitor.cmd_clear_flag(["600036"])
        today = domain.cst_today()
        conn = db.get_db()
        row = conn.execute(
            "SELECT flags FROM analysis_results WHERE code=? AND date=?",
            ("600036", today),
        ).fetchone()
        conn.close()
        assert row[0] is None


# ═══════════════════════════════════════════════════════════════════


class TestRemoveHolding:
    def test_removes_existing_holding(self, capsys):
        commands_holdings.cmd_add_holding(["600519", "1800"])
        capsys.readouterr()
        commands_holdings.cmd_remove_holding(["600519"])
        out = capsys.readouterr().out
        assert "600519" in out

        conn = db.get_db()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM holdings WHERE code='600519'"
        ).fetchone()[0]
        conn.close()
        assert cnt == 0

    def test_nonexistent_holding_exits(self):
        with pytest.raises(SystemExit) as exc:
            commands_holdings.cmd_remove_holding(["999888"])
        assert exc.value.code == 1


# ═══════════════════════════════════════════════════════════════════


class TestCleanupAndClear:
    def test_cleanup_removes_expired_fundamentals(self, capsys, monkeypatch):
        set_valid_fundamentals("600000", "浦发银行", "银行", {"pe": 5})
        monkeypatch.setattr(domain, "is_expired", lambda *_: True)
        commands_admin.cmd_cleanup()
        out = capsys.readouterr().out
        assert "浦发银行" in out

        conn = db.get_db()
        row = conn.execute(
            "SELECT code FROM stock_fundamentals WHERE code='600000'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_cleanup_retains_scheduler_window_and_removes_only_older_analysis(
        self, capsys, monkeypatch
    ):
        now = domain.utc_now()
        monkeypatch.setattr(domain, "utc_now", lambda: now)
        conn = db.get_db()
        conn.executemany(
            "INSERT INTO analysis_results (code, date, result, created_at) VALUES (?,?,?,?)",
            [
                (
                    "RECENT",
                    (now - timedelta(days=1)).date().isoformat(),
                    "调度窗口内",
                    (now - timedelta(days=1)).isoformat(),
                ),
                (
                    "BOUNDARY",
                    (now - timedelta(days=14)).date().isoformat(),
                    "调度窗口边界",
                    (now - timedelta(days=14)).isoformat(),
                ),
                (
                    "OLD",
                    (now - timedelta(days=14, microseconds=1)).date().isoformat(),
                    "调度窗口外",
                    (now - timedelta(days=14, microseconds=1)).isoformat(),
                ),
            ],
        )
        conn.commit()
        conn.close()

        commands_admin.cmd_cleanup()
        out = capsys.readouterr().out
        assert "1 条历史分析结论" in out
        conn = db.get_db()
        remaining = {
            row[0] for row in conn.execute("SELECT code FROM analysis_results")
        }
        conn.close()
        assert remaining == {"RECENT", "BOUNDARY"}

    def test_cleanup_no_expired_reports_clean(self, capsys):
        set_valid_fundamentals("000001", "平安银行", "银行", {"pb": 0.7})
        commands_admin.cmd_cleanup()
        out = capsys.readouterr().out
        assert "无过期" in out

    def test_clear_all_removes_everything(self, capsys):
        set_valid_fundamentals("600036", "招商银行", "银行", {"roe": 15})
        _insert_analysis("600036")
        record_valid_quote("600036")
        store.set_market_indicator_snapshot("bond_yield_10y", 1.7, "2026-07-14", "test")
        store.update_qualitative_only_security("601318", "中国平安", "保险")
        commands_admin.cmd_clear([])
        capsys.readouterr()

        conn = db.get_db()
        f = conn.execute("SELECT COUNT(*) FROM stock_fundamentals").fetchone()[0]
        a = conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0]
        q = conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM market_indicator_snapshots").fetchone()[
            0
        ]
        qo = conn.execute(
            "SELECT COUNT(*) FROM qualitative_only_securities"
        ).fetchone()[0]
        conn.close()
        assert f == 0
        assert a == 0
        assert q == 0
        assert m == 0
        assert qo == 0

    def test_clear_specific_code_leaves_others(self, capsys):
        set_valid_fundamentals("600036", "招商银行", "银行", {"roe": 15})
        set_valid_fundamentals("601318", "中国平安", "保险", {"pb": 1.2})
        record_valid_quote("600036")
        record_valid_quote("601318")
        store.update_qualitative_only_security("600036", "招商银行", "证券")
        store.update_qualitative_only_security("601318", "中国平安", "保险")
        commands_admin.cmd_clear(["600036"])
        capsys.readouterr()

        conn = db.get_db()
        cnt = conn.execute("SELECT COUNT(*) FROM stock_fundamentals").fetchone()[0]
        row = conn.execute(
            "SELECT code FROM stock_fundamentals WHERE code='601318'"
        ).fetchone()
        quotes = {item[0] for item in conn.execute("SELECT code FROM quote_snapshots")}
        qualitative = {
            item[0]
            for item in conn.execute("SELECT code FROM qualitative_only_securities")
        }
        conn.close()
        assert cnt == 1
        assert row is not None
        assert quotes == {"601318"}
        assert qualitative == {"601318"}


# ═══════════════════════════════════════════════════════════════════


class TestCheckHoldingsBoundary:
    """价格恰好等于止损线时的行为（<=运算符的边界条件）"""

    def _add_holding_cost40(self):
        # cost=40 → sl15=34.000, sl20=32.000
        commands_holdings.cmd_add_holding(["600036", "40.0", "100", "--notes", "测试"])

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
        commands_monitor.cmd_set_flag(["600036", "red", "大股东减持"])
        capsys.readouterr()
        commands_admin.cmd_watchlist(["--json"])
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["flags"][0]["level"] == "red"


# ═══════════════════════════════════════════════════════════════════


class TestFetchCurrentPrice:
    def test_sh_prefix_for_6xx_codes(self, monkeypatch):
        captured = {}

        def mock_get(url, **kwargs):
            captured["url"] = url
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x="招商银行,40.0,40.1,41.50,42.0,39.8"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        price = market_quotes.fetch_current_price("600036")
        assert "sh600036" in captured["url"]
        assert price == 41.50

    def test_sz_prefix_for_0xx_codes(self, monkeypatch):
        captured = {}

        def mock_get(url, **kwargs):
            captured["url"] = url
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x="平安银行,10.0,10.1,10.20,10.5,9.9"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        market_quotes.fetch_current_price("000001")
        assert "sz000001" in captured["url"]

    def test_bj_prefix_for_4xx_codes(self, monkeypatch):
        captured = {}

        def mock_get(url, **kwargs):
            captured["url"] = url
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x="某北交所,5.0,5.1,5.20,5.5,4.9"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        market_quotes.fetch_current_price("430570")
        assert "bj430570" in captured["url"]

    def test_bj_prefix_for_8xx_codes(self, monkeypatch):
        captured = {}

        def mock_get(url, **kwargs):
            captured["url"] = url
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x="某北交所,1.0,1.0,1.10,1.2,0.9"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        market_quotes.fetch_current_price("872925")
        assert "bj872925" in captured["url"]

    def test_malformed_response_returns_none(self, monkeypatch):
        """Fewer than 4 fields in the response body → None, no crash."""

        def mock_get(url, **kwargs):
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x="only,three"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        assert market_quotes.fetch_current_price("600036") is None

    def test_empty_quotes_returns_none(self, monkeypatch):
        def mock_get(url, **kwargs):
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x=""'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        assert market_quotes.fetch_current_price("600036") is None

    def test_request_exception_returns_none(self, monkeypatch):
        import requests as req

        def mock_get(url, **kwargs):
            raise req.RequestException("network error")

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        assert market_quotes.fetch_current_price("600036") is None

    def test_fetch_current_price_quotes_batch_success(self, monkeypatch):
        captured = {}

        def mock_get(url, **kwargs):
            captured["url"] = url
            r = MagicMock()
            r.encoding = "gbk"
            # Sina batch format: multiple lines of var hq_str_sh600036="..."
            r.text = (
                'var hq_str_sh600036="招商银行,40.0,40.1,41.50,42.0,39.8"\n'
                'var hq_str_sz000001="平安银行,10.0,10.1,10.20,10.5,9.9"\n'
            )
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        res = market_quotes.fetch_current_price_quotes(["600036", "000001"])
        assert "sh600036,sz000001" in captured["url"]
        assert res["600036"].price == 41.50
        assert res["000001"].price == 10.20

    def test_fetch_current_price_quotes_parses_date_and_time(self, monkeypatch):
        """真实新浪返回32字段（含日期/时间）时，fetch_current_price_quotes 能正确提取 quote_date/quote_time（BUG-006）"""

        def mock_get(url, **kwargs):
            r = MagicMock()
            r.encoding = "gbk"
            fields = (
                [
                    "招商银行",
                    "40.0",
                    "40.1",
                    "41.50",
                    "42.0",
                    "39.8",
                    "41.49",
                    "41.51",
                    "123456",
                    "987654321",
                ]
                + [str(x) for x in range(100, 120)]
                + ["2026-07-02", "15:00:00"]
            )
            r.text = f'var hq_str_sh600036="{",".join(fields)}"\n'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        res = market_quotes.fetch_current_price_quotes(["600036"])
        quote = res["600036"]
        assert quote.price == 41.50
        assert quote.quote_date == "2026-07-02"
        assert quote.quote_time == "15:00:00"
        assert quote.previous_close == 40.1

    def test_fetch_current_price_quotes_short_fields_degrade_to_none(self, monkeypatch):
        """字段不足32个（旧格式/异常返回）时 quote_date/quote_time 降级为 None，不报错"""

        def mock_get(url, **kwargs):
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var hq_str_sh600036="招商银行,40.0,40.1,41.50,42.0,39.8"\n'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        res = market_quotes.fetch_current_price_quotes(["600036"])
        quote = res["600036"]
        assert quote.price == 41.50
        assert quote.quote_date is None
        assert quote.quote_time is None

    def test_fetch_current_price_quotes_empty(self, monkeypatch):
        request = MagicMock(side_effect=AssertionError("empty batch must not fetch"))
        monkeypatch.setattr(market_quotes.requests, "get", request)
        res = market_quotes.fetch_current_price_quotes([])
        assert res == {}
        request.assert_not_called()

    def test_batch_quotes_preserve_missing_symbols_and_network_failures(
        self, monkeypatch
    ):
        request = MagicMock(
            return_value=MagicMock(text='var hq_str_sh600036="fixture,40,40.1,41.5";')
        )
        monkeypatch.setattr(market_quotes.requests, "get", request)
        codes = ["600036", "000001"]
        quotes = market_quotes.fetch_current_price_quotes(codes)
        assert quotes["600036"].price == 41.5
        assert quotes["600036"].previous_close == 40.1
        assert quotes["000001"] is None
        assert request.call_count == 1
        request.side_effect = market_quotes.requests.RequestException("fixture")
        assert market_quotes.fetch_current_price_quotes(codes) == dict.fromkeys(codes)


# ═══════════════════════════════════════════════════════════════════


class TestCloseHoldingCustomDate:
    def test_custom_exit_date_stored(self, capsys):
        commands_holdings.cmd_add_holding(
            ["000001", "10.0", "100", "--date", "2025-12-01"]
        )
        capsys.readouterr()
        commands_holdings.cmd_close_holding(["000001", "12.0", "2025-12-31"])
        capsys.readouterr()

        conn = db.get_db()
        row = conn.execute(
            "SELECT exit_date FROM holdings WHERE code='000001'"
        ).fetchone()
        conn.close()
        assert row[0] == "2025-12-31"
