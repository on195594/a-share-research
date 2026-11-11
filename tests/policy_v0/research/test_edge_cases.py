"""
Edge cases and race conditions not covered by existing tests.

Coverage gaps addressed:
  cache.py  — add_holding arg-parsing branches, set_score validation,
              holdings display, fetch_current_price market prefix,
              set_analysis negative score, clear_flag edge cases,
              re-closing an already-closed lot
  fetcher.py — _is_timed_call_error all branches, compute_pe_percentile,
               compute_pb_percentile,
               avg_of with 0.0 values
  race conds — concurrent cmd_close_holding, concurrent cmd_set_flag,
               concurrent cmd_add_holding
"""

import datetime as dt
import hashlib
import json
import threading
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest

from a_share_research.policy.v0 import (
    commands_analysis,
    commands_holdings,
    commands_monitor,
    db,
    domain,
    fetcher,
    market_quotes,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_edge.db"
    monkeypatch.setenv("CACHE_DB_PATH", str(db_file))
    yield str(db_file)


def _insert_analysis(code: str, result: str = "test_result", score: int | None = None):
    today = domain.cst_today()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO analysis_results (code, date, result, created_at, score) VALUES (?,?,?,?,?)",
        (code, today, result, datetime.now().isoformat(), score),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════
#  cache.py — cmd_add_holding arg-parsing edge cases
# ═══════════════════════════════════════════════════════════════════


class TestAddHoldingArgParsing:
    def test_nonnumeric_cost_price_exits(self, capsys):
        """Non-numeric cost price 'abc' → sys.exit(1) with error message."""
        with pytest.raises(SystemExit) as exc:
            commands_holdings.cmd_add_holding(["600519", "abc"])
        err = capsys.readouterr().err
        assert exc.value.code == 1
        assert "成本价必须为数字" in err

    def test_notes_require_explicit_option(self):
        commands_holdings.cmd_add_holding(["600519", "100", "--notes", "备注文字"])
        conn = db.get_db()
        row = conn.execute(
            "SELECT shares, notes FROM holdings WHERE code='600519'"
        ).fetchone()
        conn.close()
        assert row[0] is None
        assert row[1] == "备注文字"

    def test_text_as_third_arg_is_rejected(self, capsys):
        with pytest.raises(SystemExit) as exc:
            commands_holdings.cmd_add_holding(["600519", "100", "备注文字"])
        assert exc.value.code == 1
        assert "--notes" in capsys.readouterr().err

    def test_digit_as_third_arg_becomes_shares(self):
        """add-holding <code> <cost> <digit> — digit 3rd arg goes to shares."""
        commands_holdings.cmd_add_holding(["000001", "10.0", "500"])
        conn = db.get_db()
        row = conn.execute(
            "SELECT shares, notes FROM holdings WHERE code='000001'"
        ).fetchone()
        conn.close()
        assert row[0] == 500
        assert row[1] is None

    def test_buy_score_carried_from_latest_analysis(self, capsys):
        """add-holding copies buy_score from the most recent analysis record."""
        _insert_analysis("600036", score=68)
        capsys.readouterr()
        commands_holdings.cmd_add_holding(["600036", "45.0"])
        conn = db.get_db()
        row = conn.execute(
            "SELECT buy_score FROM holdings WHERE code='600036'"
        ).fetchone()
        conn.close()
        assert row[0] == 68

    def test_stop_loss_prices_computed_to_3dp(self):
        """Stop-loss = cost × 0.85 / 0.80, rounded to 3 decimal places."""
        commands_holdings.cmd_add_holding(["000001", "10.0"])
        conn = db.get_db()
        row = conn.execute(
            "SELECT stop_loss_15, stop_loss_20 FROM holdings WHERE code='000001'"
        ).fetchone()
        conn.close()
        assert row[0] == pytest.approx(8.5, abs=0.001)
        assert row[1] == pytest.approx(8.0, abs=0.001)

    def test_stop_loss_precision_on_fractional_cost(self):
        """cost=47.33 → stop-loss values rounded correctly to 3 d.p."""
        commands_holdings.cmd_add_holding(["601318", "47.33"])
        conn = db.get_db()
        row = conn.execute(
            "SELECT stop_loss_15, stop_loss_20 FROM holdings WHERE code='601318'"
        ).fetchone()
        conn.close()
        assert row[0] == pytest.approx(round(47.33 * 0.85, 3), abs=0.0001)
        assert row[1] == pytest.approx(round(47.33 * 0.80, 3), abs=0.0001)


# ═══════════════════════════════════════════════════════════════════
#  cache.py — cmd_holdings display edge cases
# ═══════════════════════════════════════════════════════════════════


class TestHoldingsDisplay:
    def test_empty_holdings_prints_no_record_message(self, capsys):
        """No holdings → prints '暂无持仓记录', no crash."""
        db.get_db().close()
        commands_holdings.cmd_holdings()
        out = capsys.readouterr().out
        assert "暂无持仓记录" in out

    def test_closed_position_shows_pnl_and_stats(self, capsys):
        """Closed position history shows P&L % and statistics (胜率/平均盈亏)."""
        commands_holdings.cmd_add_holding(["600519", "100.0", "100"])
        commands_holdings.cmd_close_holding(["600519", "120.0"])
        capsys.readouterr()
        commands_holdings.cmd_holdings()
        out = capsys.readouterr().out
        assert "已平仓历史" in out
        assert "+20.0%" in out
        assert "胜率" in out
        assert "平均盈亏" in out

    def test_loss_position_shows_negative_pnl(self, capsys):
        """Loss position shows negative P&L percentage."""
        commands_holdings.cmd_add_holding(["000001", "10.0", "100"])
        commands_holdings.cmd_close_holding(["000001", "8.0"])
        capsys.readouterr()
        commands_holdings.cmd_holdings()
        out = capsys.readouterr().out
        assert "-20.0%" in out

    def test_win_rate_calculation_all_winners(self, capsys):
        """All profitable positions → 胜率 100%."""
        commands_holdings.cmd_add_holding(["600519", "100.0", "100"])
        commands_holdings.cmd_add_holding(["000001", "10.0", "100"])
        commands_holdings.cmd_close_holding(["600519", "110.0"])
        commands_holdings.cmd_close_holding(["000001", "11.0"])
        capsys.readouterr()
        commands_holdings.cmd_holdings()
        out = capsys.readouterr().out
        assert "胜率 100%" in out

    def test_code_filter_does_not_leak_other_holdings(self, capsys):
        commands_holdings.cmd_add_holding(
            ["603606", "34.24", "500", "--notes", "east-only"]
        )
        commands_holdings.cmd_add_holding(
            ["600036", "39.58", "500", "--notes", "bank-secret"]
        )
        capsys.readouterr()

        commands_holdings.cmd_holdings(["603606"])

        out = capsys.readouterr().out
        assert "603606" in out
        assert "east-only" in out
        assert "600036" not in out
        assert "bank-secret" not in out

    def test_missing_code_has_stable_not_held_result(self, capsys):
        db.get_db().close()

        commands_holdings.cmd_holdings(["603606"])

        assert capsys.readouterr().out.strip() == "NOT_HELD 603606"

    def test_holdings_handler_is_physically_read_only(self, isolated_db, capsys):
        commands_holdings.cmd_add_holding(["603606", "34.24", "500"])
        conn = db.get_db()
        conn.execute("DELETE FROM holding_events")
        conn.execute(
            """UPDATE holdings
               SET initial_shares=NULL, main_entry_date=NULL,
                   main_entry_basis=NULL, reference_cost=NULL"""
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        capsys.readouterr()
        path = Path(isolated_db)
        before = hashlib.sha256(path.read_bytes()).hexdigest()

        commands_holdings.cmd_holdings(["603606"])

        after = hashlib.sha256(path.read_bytes()).hexdigest()
        assert after == before
        conn = db.get_db()
        row = conn.execute(
            "SELECT initial_shares, reference_cost FROM holdings WHERE code='603606'"
        ).fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM holding_events").fetchone()[0]
        conn.close()
        assert row == (None, None)
        assert event_count == 0

    @pytest.mark.parametrize(
        "command,args",
        [
            (commands_holdings.cmd_retro_pending, []),
            (commands_holdings.cmd_retro_outliers, []),
        ],
    )
    def test_retro_read_commands_are_physically_read_only(
        self, isolated_db, capsys, command, args
    ):
        commands_holdings.cmd_add_holding(["603606", "34.24", "500"])
        commands_holdings.cmd_close_holding(["603606", "35.00"])
        conn = db.get_db()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        capsys.readouterr()
        path = Path(isolated_db)
        before = hashlib.sha256(path.read_bytes()).hexdigest()

        command(args)

        after = hashlib.sha256(path.read_bytes()).hexdigest()
        assert after == before


# ═══════════════════════════════════════════════════════════════════
#  cache.py — cmd_close_holding defensive logic
# ═══════════════════════════════════════════════════════════════════


class TestCloseHoldingDefense:
    def test_second_close_on_same_stock_fails_when_no_open_lot_remains(self, capsys):
        """Closing after all lots are closed → sys.exit(1)."""
        commands_holdings.cmd_add_holding(["600519", "100.0"])
        commands_holdings.cmd_close_holding(["600519", "120.0"])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            commands_holdings.cmd_close_holding(["600519", "130.0"])
        assert exc.value.code == 1

    def test_close_holding_exit_price_zero_is_rejected(self):
        """exit_price=0 is rejected by positive-price validation."""
        commands_holdings.cmd_add_holding(["000001", "10.0"])
        with pytest.raises(SystemExit) as exc:
            commands_holdings.cmd_close_holding(["000001", "0.0"])
        assert exc.value.code == 1

    def test_negative_exit_price_should_be_rejected(self, capsys):
        """Negative exit price should be rejected."""
        commands_holdings.cmd_add_holding(["000001", "10.0"])
        with pytest.raises(SystemExit) as exc:
            commands_holdings.cmd_close_holding(["000001", "-5.0"])
        assert exc.value.code == 1


# ═══════════════════════════════════════════════════════════════════
#  cache.py — cmd_set_score input validation
# ═══════════════════════════════════════════════════════════════════


class TestSetScoreValidation:
    def test_nonnumeric_score_exits(self):
        """String 'abc' → ValueError → sys.exit(1)."""
        _insert_analysis("600036")
        with pytest.raises(SystemExit) as exc:
            commands_analysis.cmd_set_score(["600036", "abc"])
        assert exc.value.code == 1

    def test_float_string_score_exits(self):
        """'12.5' cannot be converted by int() → sys.exit(1). Prevents silent float acceptance."""
        _insert_analysis("600036")
        with pytest.raises(SystemExit) as exc:
            commands_analysis.cmd_set_score(["600036", "12.5"])
        assert exc.value.code == 1

    def test_negative_integer_score_rejected(self, capsys):
        """Scores outside the 0—80 contract are rejected."""
        _insert_analysis("600036")
        with pytest.raises(SystemExit):
            commands_analysis.cmd_set_score(["600036", "-3"])


# ═══════════════════════════════════════════════════════════════════
#  cache.py — cmd_set_analysis negative score argument
# ═══════════════════════════════════════════════════════════════════


class TestSetAnalysisNegativeScore:
    def test_negative_score_arg_rejected(self, capsys, monkeypatch):
        """set-analysis rejects a negative score before any write."""
        monkeypatch.setattr(
            "sys.stdin",
            StringIO(
                'analysis conclusion text\n行业地位[评级=优；证据="市占率连续5年第一";置信度=高]'
            ),
        )
        with pytest.raises(SystemExit):
            commands_analysis.cmd_set_analysis(["600036", "A", "-5"])


# ═══════════════════════════════════════════════════════════════════
#  cache.py — fetch_current_price market prefix: 3xx ChiNext
# ═══════════════════════════════════════════════════════════════════


class TestFetchCurrentPriceMarketPrefix:
    def _mock_get(self, monkeypatch, captured: dict, name: str, fields: str):
        from unittest.mock import MagicMock

        def mock_get(url, **kwargs):
            captured["url"] = url
            r = MagicMock()
            r.encoding = "gbk"
            r.text = f'var x="{fields}"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)

    def test_sz_prefix_for_3xx_chinext_codes(self, monkeypatch):
        """ChiNext (创业板) 300xxx → 'sz' prefix, not 'bj'."""
        captured = {}
        self._mock_get(
            monkeypatch, captured, "宁德时代", "宁德时代,200.0,200.1,205.50,210.0,199.0"
        )
        market_quotes.fetch_current_price("300750")
        assert "sz300750" in captured["url"]

    def test_field_index_3_is_current_price_not_open(self, monkeypatch):
        """Sina format: field[3]=current price, field[2]=open. Guard against index error."""
        captured = {}
        # name, yesterday_close, open, CURRENT, high, low
        self._mock_get(
            monkeypatch, captured, "招商银行", "招商银行,40.00,40.10,41.50,42.00,39.80"
        )
        price = market_quotes.fetch_current_price("600036")
        assert price == 41.50, "field[3] must be current price, not open (40.10)"

    def test_response_with_exactly_4_fields_is_valid(self, monkeypatch):
        """Exactly 4 fields is the boundary minimum — must succeed."""
        from unittest.mock import MagicMock

        def mock_get(url, **kwargs):
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x="name,10.0,10.1,10.5"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        price = market_quotes.fetch_current_price("000001")
        assert price == 10.5

    def test_response_with_3_fields_returns_none(self, monkeypatch):
        """Fewer than 4 fields → None (boundary: < 4 condition)."""
        from unittest.mock import MagicMock

        def mock_get(url, **kwargs):
            r = MagicMock()
            r.encoding = "gbk"
            r.text = 'var x="name,10.0,10.1"'
            return r

        monkeypatch.setattr(market_quotes.requests, "get", mock_get)
        assert market_quotes.fetch_current_price("000001") is None


# ═══════════════════════════════════════════════════════════════════
#  cache.py — cmd_clear_flag edge cases
# ═══════════════════════════════════════════════════════════════════


class TestClearFlagEdgeCases:
    def test_no_args_exits(self):
        """No arguments → sys.exit(1)."""
        with pytest.raises(SystemExit) as exc:
            commands_monitor.cmd_clear_flag([])
        assert exc.value.code == 1

    def test_nonexistent_code_succeeds_silently(self, capsys):
        """clear_flag on a code with no analysis record succeeds silently (UPDATE 0 rows)."""
        commands_monitor.cmd_clear_flag(["999999"])
        out = capsys.readouterr().out
        assert "999999" in out


# ═══════════════════════════════════════════════════════════════════
#  fetcher.py — _is_timed_call_error all branches
# ═══════════════════════════════════════════════════════════════════


class TestIsTimedCallError:
    def test_timeout_string_is_error(self):
        assert fetcher._is_timed_call_error("TIMEOUT") is True

    def test_error_two_tuple_is_error(self):
        assert fetcher._is_timed_call_error(("ERROR", "message")) is True

    def test_real_value_is_not_error(self):
        assert fetcher._is_timed_call_error(42) is False

    def test_none_is_not_error(self):
        """None means 'no result', not an error sentinel."""
        assert fetcher._is_timed_call_error(None) is False

    def test_empty_string_is_not_error(self):
        assert fetcher._is_timed_call_error("") is False

    def test_wrong_prefix_tuple_is_not_error(self):
        """('TIMEOUT', 'msg') is a 2-tuple but prefix is not 'ERROR'."""
        assert fetcher._is_timed_call_error(("TIMEOUT", "msg")) is False

    def test_single_element_error_tuple_is_not_error(self):
        """('ERROR',) has length 1 — not the expected 2-element form."""
        assert fetcher._is_timed_call_error(("ERROR",)) is False

    def test_zero_is_not_error(self):
        """0 is a valid result value — must not be confused with None or TIMEOUT."""
        assert fetcher._is_timed_call_error(0) is False


# ═══════════════════════════════════════════════════════════════════
#  fetcher.py — avg_of with 0.0 values
# ═══════════════════════════════════════════════════════════════════


class TestAvgOfZeroValues:
    def _s(self, values):
        import pandas as pd

        return pd.Series(values)

    def test_zero_float_included_in_average(self):
        """0.0 is a valid data point — must not be filtered out like None."""
        result = fetcher.avg_of(self._s([0.0, 10.0, 20.0]), 3)
        assert result == pytest.approx(10.0)

    def test_single_zero_series_returns_zero(self):
        """Series([0.0]) → avg = 0.0 (not None)."""
        result = fetcher.avg_of(self._s([0.0]), 1)
        assert result == 0.0

    def test_zero_mixed_with_nones_only_zero_counted(self):
        """[None, 0.0]: None excluded, 0.0 included → avg=0.0."""
        result = fetcher.avg_of(self._s([None, 0.0]), 2)
        assert result == 0.0


# ═══════════════════════════════════════════════════════════════════
#  fetcher.py — compute_pe_percentile
# ═══════════════════════════════════════════════════════════════════


class TestComputePePercentile:
    """Inject price_df directly to avoid AKShare network calls."""

    def _fin_df(self, eps_by_year: dict):
        import pandas as pd

        return pd.DataFrame(
            [
                {"报告期": yr, "基本每股收益": str(eps)}
                for yr, eps in eps_by_year.items()
            ]
        )

    def _price_df(self, n_days: int, price: float, start: dt.date) -> object:
        import pandas as pd

        rows = [
            {"日期": str(start + dt.timedelta(days=i)), "收盘": price}
            for i in range(n_days)
        ]
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["日期"]).astype("datetime64[us]")
        return df

    def test_none_pe_returns_none(self):
        assert fetcher.compute_pe_percentile("600036", None, None) is None

    def test_zero_pe_returns_none(self):
        assert (
            fetcher.compute_pe_percentile("600036", 0, self._fin_df({2023: 1.0}))
            is None
        )

    def test_negative_pe_returns_none(self):
        assert (
            fetcher.compute_pe_percentile("600036", -5.0, self._fin_df({2023: 1.0}))
            is None
        )

    def test_api_error_price_df_propagates(self):
        """price_df='API_ERROR' → same string returned unchanged."""
        result = fetcher.compute_pe_percentile(
            "600036", 20.0, self._fin_df({2023: 2.0}), price_df="API_ERROR"
        )
        assert result == "API_ERROR"

    def test_all_negative_eps_no_records_returns_none(self):
        """Only negative EPS → eps_records empty → None, even with 200 price rows."""
        import pandas as pd

        fin_df = pd.DataFrame([{"报告期": 2023, "基本每股收益": "-2.0"}])
        price_df = self._price_df(200, 20.0, dt.date(2015, 5, 1))
        result = fetcher.compute_pe_percentile(
            "600036", 20.0, fin_df, price_df=price_df
        )
        assert result is None

    def test_fewer_than_100_data_points_returns_none(self):
        """< 100 valid PE data points → None (insufficient statistical basis)."""
        fin_df = self._fin_df({2014: 1.0})
        price_df = self._price_df(10, 20.0, dt.date(2015, 5, 1))
        result = fetcher.compute_pe_percentile(
            "600036", 20.0, fin_df, price_df=price_df
        )
        assert result is None

    def test_current_pe_above_all_history_returns_100(self):
        """Current PE higher than all historical PE → 100.0%."""
        fin_df = self._fin_df({2014: 1.0})  # historical PE = 10 (price 10 / EPS 1)
        price_df = self._price_df(200, 10.0, dt.date(2015, 5, 1))
        result = fetcher.compute_pe_percentile(
            "600036", 1000.0, fin_df, price_df=price_df
        )
        assert result == 100.0

    def test_current_pe_below_all_history_returns_zero(self):
        """Current PE lower than all historical PE → 0.0%."""
        fin_df = self._fin_df({2014: 1.0})  # historical PE = 30 (price 30 / EPS 1)
        price_df = self._price_df(200, 30.0, dt.date(2015, 5, 1))
        result = fetcher.compute_pe_percentile("600036", 1.0, fin_df, price_df=price_df)
        assert result == 0.0

    def test_median_pe_returns_approximately_50_pct(self):
        """Current PE at historical median → ~50% (±10% tolerance)."""
        import pandas as pd

        # 200 days: 100 days at PE=5 (price=5), 100 days at PE=15 (price=15)
        fin_df = self._fin_df({2014: 1.0})
        rows = [
            {"日期": str(dt.date(2015, 5, 1) + dt.timedelta(days=i)), "收盘": 5.0}
            for i in range(100)
        ] + [
            {"日期": str(dt.date(2015, 9, 8) + dt.timedelta(days=i)), "收盘": 15.0}
            for i in range(100)
        ]
        price_df = pd.DataFrame(rows)
        price_df["date"] = pd.to_datetime(price_df["日期"]).astype("datetime64[us]")
        result = fetcher.compute_pe_percentile(
            "600036", 10.0, fin_df, price_df=price_df
        )
        assert result is not None
        assert 40.0 <= result <= 60.0, (
            f"median PE percentile should be ~50%, got {result}"
        )


# ═══════════════════════════════════════════════════════════════════
#  fetcher.py — compute_pb_percentile
# ═══════════════════════════════════════════════════════════════════


class TestComputePbPercentile:
    def _fin_df(self, bps_by_year: dict):
        import pandas as pd

        return pd.DataFrame(
            [{"报告期": yr, "每股净资产": str(bps)} for yr, bps in bps_by_year.items()]
        )

    def _price_df(self, n_days: int, price: float) -> object:
        import pandas as pd

        start = dt.date(2015, 5, 1)
        rows = [
            {"日期": str(start + dt.timedelta(days=i)), "收盘": price}
            for i in range(n_days)
        ]
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["日期"]).astype("datetime64[us]")
        return df

    def test_none_pb_returns_none(self):
        assert fetcher.compute_pb_percentile("600036", None, None) is None

    def test_zero_pb_returns_none(self):
        assert (
            fetcher.compute_pb_percentile("600036", 0, self._fin_df({2023: 5.0}))
            is None
        )

    def test_negative_pb_returns_none(self):
        assert (
            fetcher.compute_pb_percentile("600036", -1.0, self._fin_df({2023: 5.0}))
            is None
        )

    def test_api_error_price_df_propagates(self):
        result = fetcher.compute_pb_percentile(
            "600036", 1.5, self._fin_df({2023: 5.0}), price_df="API_ERROR"
        )
        assert result == "API_ERROR"

    def test_insufficient_data_points_returns_none(self):
        fin_df = self._fin_df({2014: 5.0})
        price_df = self._price_df(50, 25.0)  # 50 < 100 threshold
        result = fetcher.compute_pb_percentile("600036", 1.5, fin_df, price_df=price_df)
        assert result is None

    def test_high_pb_above_all_history_returns_100(self):
        """Current PB higher than all historical PB → 100.0%."""
        fin_df = self._fin_df({2014: 10.0})  # historical PB = 2.5 (price 25 / BPS 10)
        price_df = self._price_df(200, 25.0)
        result = fetcher.compute_pb_percentile(
            "600036", 999.0, fin_df, price_df=price_df
        )
        assert result == 100.0

    def test_all_negative_bps_returns_none(self):
        """Only negative BPS → bps_records empty → None."""
        import pandas as pd

        fin_df = pd.DataFrame([{"报告期": 2023, "每股净资产": "-5.0"}])
        price_df = self._price_df(200, 10.0)
        result = fetcher.compute_pb_percentile("600036", 1.0, fin_df, price_df=price_df)
        assert result is None


# ═══════════════════════════════════════════════════════════════════
#  Race conditions
# ═══════════════════════════════════════════════════════════════════


class TestRaceConditions:
    def test_concurrent_add_holding_allows_exactly_one_open_position(self, capsys):
        """Concurrent initial entries serialize; the loser is told to use buy-holding."""
        import sqlite3 as _sqlite3

        barrier = threading.Barrier(2)
        lock_errors: list[str] = []
        unexpected_errors: list[Exception] = []
        rejected: list[int] = []

        def add():
            try:
                barrier.wait()
                commands_holdings.cmd_add_holding(["600519", "100.0"])
            except _sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    lock_errors.append(str(e))
                else:
                    unexpected_errors.append(e)
            except SystemExit as e:
                rejected.append(e.code)
            except Exception as e:
                unexpected_errors.append(e)

        threads = [threading.Thread(target=add) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not lock_errors, (
            f"lock errors should be retried by SQLite timeout: {lock_errors}"
        )
        assert not unexpected_errors, f"unexpected exceptions: {unexpected_errors}"
        assert all(not thread.is_alive() for thread in threads)

        conn = db.get_db()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM holdings WHERE code='600519'"
        ).fetchone()[0]
        conn.close()

        assert cnt == 1
        assert rejected == [1]

    def test_sequential_close_holding_fifo_each_lot_closed_once(self, capsys):
        """Two sequential closes → FIFO order, each lot closed exactly once."""
        conn = db.get_db()
        conn.execute(
            "INSERT INTO holdings (code, cost_price, buy_date) VALUES ('600519', 100.0, '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO holdings (code, cost_price, buy_date) VALUES ('600519', 110.0, '2024-06-01')"
        )
        conn.commit()
        conn.close()
        capsys.readouterr()

        commands_holdings.cmd_close_holding(["600519", "120.0"])
        commands_holdings.cmd_close_holding(["600519", "125.0"])

        conn = db.get_db()
        rows = conn.execute(
            "SELECT cost_price, exit_price FROM holdings WHERE code='600519' ORDER BY buy_date"
        ).fetchall()
        conn.close()

        assert rows[0][1] == 120.0, "first lot (cost 100.0) should close first"
        assert rows[1][1] == 125.0, "second lot (cost 110.0) should close second"

    def test_concurrent_set_flag_both_flags_persisted(self, capsys):
        """Two concurrent set_flag calls should both persist — known race condition.

        Current implementation is not atomic: a concurrent race can drop one flag.
        """
        _insert_analysis("600036")
        capsys.readouterr()

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def set_flag(level: str, reason: str):
            try:
                barrier.wait()
                commands_monitor.cmd_set_flag(["600036", level, reason])
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=set_flag, args=("yellow", "first flag")),
            threading.Thread(target=set_flag, args=("red", "second flag")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"thread exceptions: {errors}"

        today = domain.cst_today()
        conn = db.get_db()
        row = conn.execute(
            "SELECT flags FROM analysis_results WHERE code=? AND date=?",
            ("600036", today),
        ).fetchone()
        conn.close()
        flags = json.loads(row[0]) if row[0] else []
        assert len(flags) == 2, (
            f"both flags should be stored but only {len(flags)} found — dropped by race"
        )

    def test_concurrent_close_same_stock_closes_each_lot_once(self, capsys):
        """Two concurrent close requests must consume two distinct FIFO lots."""
        conn = db.get_db()
        conn.execute(
            "INSERT INTO holdings (code, cost_price, buy_date) VALUES ('600519', 100.0, '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO holdings (code, cost_price, buy_date) VALUES ('600519', 110.0, '2024-06-01')"
        )
        conn.commit()
        conn.close()
        capsys.readouterr()

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def close_one():
            try:
                barrier.wait()
                commands_holdings.cmd_close_holding(["600519", "120.0"])
            except SystemExit as exc:
                errors.append(exc)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=close_one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"unexpected thread exceptions: {errors}"
        assert all(not thread.is_alive() for thread in threads)

        conn = db.get_db()
        closed_count = conn.execute(
            "SELECT COUNT(*) FROM holdings WHERE code='600519' AND exit_date IS NOT NULL"
        ).fetchone()[0]
        conn.close()

        assert closed_count == 2, "each concurrent close must update a distinct lot"


def _insert_closed_holding_with_events(
    conn,
    *,
    code: str,
    name: str,
    cost: float,
    shares: int,
    buy_score: int,
    exit_price: float,
) -> int:
    cursor = conn.execute(
        """INSERT INTO holdings
           (code, name, cost_price, shares, buy_date, buy_score,
            exit_price, exit_date, updated_at)
           VALUES (?, ?, ?, ?, '2026-01-01', ?, ?, '2026-06-01', '2026-06-01')""",
        (code, name, cost, shares, buy_score, exit_price),
    )
    holding_id = cursor.lastrowid
    conn.executemany(
        """INSERT INTO holding_events
           (holding_id, code, event_type, event_date, shares, price, created_at)
           VALUES (?, ?, ?, ?, ?, ?, '2026-06-01')""",
        [
            (holding_id, code, "buy", "2026-01-01", shares, cost),
            (holding_id, code, "sell", "2026-06-01", shares, exit_price),
        ],
    )
    return holding_id


class TestRetroAdd:
    def test_retro_add_happy_path(self, capsys):
        with db.db_session() as conn:
            _insert_closed_holding_with_events(
                conn,
                code="600036",
                name="招商银行",
                cost=40.0,
                shares=100,
                buy_score=70,
                exit_price=42.0,
            )
            conn.commit()
        commands_holdings.cmd_retro_add(["600036", "ROE高估"])
        out = capsys.readouterr().out
        assert "600036" in out
        assert "ROE高估" in out
        with db.db_session() as conn:
            row = conn.execute(
                "SELECT error_tags, actual_return_pct FROM retro_notes WHERE code='600036'"
            ).fetchone()
        assert row is not None
        assert row[0] == "ROE高估"
        assert abs(row[1] - 5.0) < 0.1

    def test_retro_add_no_closed_holding_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            commands_holdings.cmd_retro_add(["999999", "无错误"])
        assert exc.value.code == 1

    def test_retro_add_optional_flags(self):
        with db.db_session() as conn:
            _insert_closed_holding_with_events(
                conn,
                code="000001",
                name="平安银行",
                cost=10.0,
                shares=200,
                buy_score=60,
                exit_price=9.0,
            )
            conn.commit()
        commands_holdings.cmd_retro_add(
            [
                "000001",
                "周期顶部",
                "--note",
                "未识别周期",
                "--thesis",
                "低PB买入",
                "--gap",
                "B框架NIM阈值",
            ]
        )
        with db.db_session() as conn:
            row = conn.execute(
                "SELECT retro_text, thesis_notes, framework_gap FROM retro_notes WHERE code='000001'"
            ).fetchone()
        assert row[0] == "未识别周期"
        assert row[1] == "低PB买入"
        assert row[2] == "B框架NIM阈值"


class TestRetroPending:
    def test_retro_pending_shows_closed_without_retro(self, capsys):
        with db.db_session() as conn:
            conn.execute(
                "INSERT INTO holdings (code, name, cost_price, shares, buy_date, buy_score, exit_price, exit_date, updated_at) "
                "VALUES ('600519', '贵州茅台', 1500.0, 10, '2026-01-01', 75, 1600.0, '2026-04-01', '2026-04-01')"
            )
            conn.commit()
        commands_holdings.cmd_retro_pending()
        out = capsys.readouterr().out
        assert "600519" in out

    def test_retro_pending_empty_when_all_have_retro(self, capsys):
        with db.db_session() as conn:
            conn.execute(
                "INSERT INTO holdings (id, code, name, cost_price, shares, buy_date, buy_score, exit_price, exit_date, updated_at) "
                "VALUES (1, '600519', '贵州茅台', 1500.0, 10, '2026-01-01', 75, 1600.0, '2026-04-01', '2026-04-01')"
            )
            conn.execute(
                "INSERT INTO retro_notes (holding_id, code, error_tags, created_at) VALUES (1, '600519', '无错误', '2026-04-02')"
            )
            conn.commit()
        commands_holdings.cmd_retro_pending()
        out = capsys.readouterr().out
        assert "600519" not in out
        assert "无待复盘" in out


class TestRetroStats:
    def test_retro_stats_no_records(self, capsys):
        commands_holdings.cmd_retro_stats([])
        out = capsys.readouterr().out
        assert "暂无复盘记录" in out

    def test_retro_stats_tag_frequency(self, capsys):
        with db.db_session() as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO retro_notes (holding_id, code, error_tags, actual_return_pct, created_at) "
                    f"VALUES ({i + 1}, '60000{i}', 'ROE高估', -5.0, '2026-0{i + 1}-01')"
                )
            conn.commit()
        commands_holdings.cmd_retro_stats([])
        out = capsys.readouterr().out
        assert "ROE高估" in out
        assert "★" in out


class TestRetroOutliers:
    def test_retro_outliers_finds_large_loss(self, capsys):
        with db.db_session() as conn:
            _insert_closed_holding_with_events(
                conn,
                code="601088",
                name="神华",
                cost=30.0,
                shares=100,
                buy_score=55,
                exit_price=25.0,
            )
            conn.commit()
        commands_holdings.cmd_retro_outliers(["--loss", "-10"])
        out = capsys.readouterr().out
        assert "601088" in out

    def test_retro_outliers_positive_loss_param(self, capsys):
        with db.db_session() as conn:
            _insert_closed_holding_with_events(
                conn,
                code="601088",
                name="神华",
                cost=30.0,
                shares=100,
                buy_score=55,
                exit_price=25.0,
            )
            _insert_closed_holding_with_events(
                conn,
                code="600036",
                name="招行",
                cost=40.0,
                shares=100,
                buy_score=70,
                exit_price=38.0,
            )
            conn.commit()
        commands_holdings.cmd_retro_outliers(["--loss", "10"])
        out = capsys.readouterr().out
        assert "601088" in out
        assert "600036" not in out

    def test_retro_outliers_excludes_already_reviewed(self, capsys):
        with db.db_session() as conn:
            conn.execute(
                "INSERT INTO holdings (id, code, name, cost_price, shares, buy_date, buy_score, exit_price, exit_date, updated_at) "
                "VALUES (1, '601088', '神华', 30.0, 100, '2026-01-01', 55, 25.0, '2026-06-01', '2026-06-01')"
            )
            conn.execute(
                "INSERT INTO retro_notes (holding_id, code, error_tags, created_at) VALUES (1, '601088', '周期顶部', '2026-06-02')"
            )
            conn.commit()
        commands_holdings.cmd_retro_outliers(["--loss", "-10"])
        out = capsys.readouterr().out
        assert "601088" not in out
