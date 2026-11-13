"""
Edge cases and race conditions not covered by existing tests.

Coverage gaps addressed:
  holdings — argument validation, display, closing lots and quote prefixes
  monitor — flag edge cases and concurrent writes
"""

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from a_share_research.policy.v0 import commands_holdings, commands_monitor, db, domain, market_quotes


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_edge.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
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
