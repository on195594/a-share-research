"""
Edge cases and race conditions not covered by existing tests.

Coverage gaps addressed:
  holdings — display, code filter and physical read-only access
  retro — pending, stats and outlier reads
"""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from a_share_research.policy.v0 import commands_holdings, db, domain
from policy_v0.helpers import seed_event, seed_position


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
        _h600519 = seed_position("600519", 100.0, 100)
        seed_event(_h600519, "sell", "2026-01-06", shares=100, price=120.0, close=True)
        capsys.readouterr()
        commands_holdings.cmd_holdings()
        out = capsys.readouterr().out
        assert "已平仓历史" in out
        assert "+20.0%" in out
        assert "胜率" in out
        assert "平均盈亏" in out

    def test_loss_position_shows_negative_pnl(self, capsys):
        """Loss position shows negative P&L percentage."""
        _h000001 = seed_position("000001", 10.0, 100)
        seed_event(_h000001, "sell", "2026-01-06", shares=100, price=8.0, close=True)
        capsys.readouterr()
        commands_holdings.cmd_holdings()
        out = capsys.readouterr().out
        assert "-20.0%" in out

    def test_win_rate_calculation_all_winners(self, capsys):
        """All profitable positions → 胜率 100%."""
        _h600519 = seed_position("600519", 100.0, 100)
        _h000001 = seed_position("000001", 10.0, 100)
        seed_event(_h600519, "sell", "2026-01-06", shares=100, price=110.0, close=True)
        seed_event(_h000001, "sell", "2026-01-06", shares=100, price=11.0, close=True)
        capsys.readouterr()
        commands_holdings.cmd_holdings()
        out = capsys.readouterr().out
        assert "胜率 100%" in out

    def test_code_filter_does_not_leak_other_holdings(self, capsys):
        seed_position("603606", 34.24, 500, notes="east-only")
        seed_position("600036", 39.58, 500, notes="bank-secret")
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
        seed_position("603606", 34.24, 500)
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
        _h603606 = seed_position("603606", 34.24, 500)
        seed_event(_h603606, "sell", "2026-01-06", shares=500, price=35.00, close=True)
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


# ═══════════════════════════════════════════════════════════════════


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
