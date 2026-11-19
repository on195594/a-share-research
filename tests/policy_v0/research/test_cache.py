"""
cache.py 单元测试
覆盖只读持仓视图、止损检查、WAL模式与框架推断。
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from a_share_research.policy.v0 import commands_admin, commands_holdings, db, domain, market_quotes
from policy_v0.helpers import (
    mock_price_quotes,
    record_valid_quote,
    seed_event,
    seed_position,
    set_valid_fundamentals,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_PY = PROJECT_ROOT / "a_share_research" / "policy" / "v0" / "cache.py"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每个测试用独立临时数据库，不影响生产 cache.db"""
    db_file = tmp_path / "test_cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
    yield str(db_file)


# ── WAL 模式 ──────────────────────────────────────────────────────────────────


def test_wal_mode_enabled():
    conn = db.get_db()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


# ── CLI 分派 ───────────────────────────────────────────────────────────────


def test_cli_valid_command_smoke(isolated_db):
    """Subprocess smoke test for the real CLI dispatch path."""
    db.get_db().close()
    result = subprocess.run(
        [sys.executable, str(CACHE_PY), "list"],
        env={**os.environ, "POLICY_V0_DB": isolated_db},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "=== 基本面缓存 (0有效 / 0条) ===" in result.stdout
    assert "[a-stock-cache] 操作数据库:" in result.stderr
    assert isolated_db in result.stderr


def test_default_db_path_is_the_application_database_without_creating_state(tmp_path):
    """默认数据库是数据目录下的应用库，解析路径不创建任何目录。"""
    tmp_home = tmp_path / "home"
    expected_db = tmp_path / "data" / "app.db"
    env = os.environ.copy()
    env.pop("POLICY_V0_DB", None)
    env["HOME"] = str(tmp_home)
    env["DATA_DIR"] = str(expected_db.parent)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from a_share_research.policy.v0.paths import cache_db_path; print(cache_db_path())",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == str(expected_db)
    assert not tmp_home.exists() and not expected_db.parent.exists(), "路径解析不应创建状态目录"


def test_cli_invalid_command_fails(isolated_db):
    """Unknown CLI commands should fail visibly."""
    result = subprocess.run(
        [sys.executable, str(CACHE_PY), "not-a-command"],
        env={**os.environ, "POLICY_V0_DB": isolated_db},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "未知命令 not-a-command" in result.stderr
    assert result.stdout == ""


# ── 建仓入口：同代码单一在仓生命周期 ──────────────────────────────────────


# ── FIFO 平仓 ────────────────────────────────────────────────────────────────


# ── cmd_holdings 显示：只只/笔数 ──────────────────────────────────────────────


def test_holdings_display_multi_lot(capsys):
    """兼容旧库同一股票两笔在仓：显示 '1 只，2 笔'。"""
    conn = db.get_db()
    conn.executemany(
        """INSERT INTO holdings
           (code, cost_price, buy_date, stop_loss_15, stop_loss_20)
           VALUES ('600519', ?, '2025-01-01', ?, ?)""",
        [(1800.0, 1530.0, 1440.0), (1850.0, 1572.5, 1480.0)],
    )
    conn.commit()
    conn.close()

    commands_holdings.cmd_holdings()
    out = capsys.readouterr().out
    assert "1 只" in out
    assert "2 笔" in out


def test_holdings_display_single_lot(capsys):
    """单笔在仓：只显示 '1 只'，不显示 '笔'"""
    seed_position("000858", 100)

    commands_holdings.cmd_holdings()
    out = capsys.readouterr().out
    assert "1 只" in out
    assert "笔" not in out


def test_holdings_display_two_stocks(capsys):
    """两只不同股票各一笔：显示 '2 只'，不显示 '笔'"""
    seed_position("600519", 1800)
    seed_position("000858", 100)

    commands_holdings.cmd_holdings()
    out = capsys.readouterr().out
    assert "2 只" in out
    assert "笔" not in out


# ── 框架推断：confident 标志区分真实判断与数据缺失兜底 ─────────────────────────


@pytest.mark.parametrize("industry", [None, "", "未知", "未知（新浪fallback）"])
def test_infer_framework_unknown_industry_not_confident(industry):
    """industry 缺失/未知时，confident 必须为 False，跟真实判断区分开"""
    framework, confident = domain.infer_framework(industry)
    assert framework == "A通用"
    assert confident is False


def test_infer_framework_known_industry_is_confident():
    """industry 是真实值（哪怕落到默认A通用）时，confident 应为 True"""
    framework, confident = domain.infer_framework("煤炭开采")
    assert framework == "C资源"
    assert confident is True


@pytest.mark.parametrize("industry", ["元器件", "连接器", "精密制造", "电子组装"])
def test_ems_and_precision_manufacturing_route_to_a(industry):
    framework, confident = domain.infer_framework(industry)
    assert framework == "A通用"
    assert confident is True


@pytest.mark.parametrize("industry", ["半导体", "软件服务", "互联网平台"])
def test_product_and_platform_technology_still_route_to_f(industry):
    framework, confident = domain.infer_framework(industry)
    assert framework == "F科技"
    assert confident is True


# ── 旧表迁移：无 id 列时触发 AUTOINCREMENT 迁移 ──────────────────────────────


# ── 结构化持仓账本与状态 ───────────────────────────────────────────────────────


def test_position_return_includes_fees_tax_dividend_and_open_value(capsys):
    holding = seed_position("600036", 10.0, 500)
    seed_event(holding, "sell", "2026-01-06", shares=200, price=12.0, fees=1, tax=2)
    seed_event(holding, "dividend", "2026-01-07", cash=100)
    commands_holdings.cmd_position_return(["600036", "11"])
    out = capsys.readouterr().out
    # 2397 sale cash + 100 dividend + 3300 open value - 5000 invested = 797.
    assert "盈亏:+797.00元" in out
    assert "总回报：+15.94%" in out


def test_position_return_isolates_reopened_position_lifecycle(capsys):
    first = seed_position("600036", 10.0, 100, day="2025-01-01")
    seed_event(first, "sell", "2025-02-01", shares=100, price=12.0, close=True)
    seed_position("600036", 20.0, 100, day="2026-07-01")
    commands_holdings.cmd_position_return(["600036", "22.0"])
    out = capsys.readouterr().out

    assert "投入:2000.00" in out
    assert "卖出回款:0.00" in out
    assert "盈亏:+200.00元" in out
    assert "总回报：+10.00%" in out


def test_position_return_uses_latest_closed_lifecycle(capsys):
    first = seed_position("600036", 10.0, 100, day="2025-01-01")
    seed_event(first, "sell", "2025-02-01", shares=100, price=12.0, close=True)
    second = seed_position("600036", 20.0, 100, day="2026-06-01")
    seed_event(second, "sell", "2026-07-01", shares=100, price=18.0, close=True)
    commands_holdings.cmd_position_return(["600036"])
    out = capsys.readouterr().out

    assert "投入:2000.00" in out
    assert "卖出回款:1800.00" in out
    assert "盈亏:-200.00元" in out
    assert "总回报：-10.00%" in out


def test_bse_920_uses_the_beijing_quote_prefix():
    assert [market_quotes.sina_query_prefix(c) for c in ("920189", "430570", "872925", "600036", "000001", "300750")] == ["bj", "bj", "bj", "sh", "sz", "sz"]


def test_portfolio_risk_no_holdings(capsys):
    """真实空仓不等于缺失数据库：先建立空 fixture schema。"""
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone() == (0,)
    commands_holdings.cmd_portfolio_risk()
    out = capsys.readouterr().out
    assert "暂无持仓" in out


def test_portfolio_risk_with_holdings(capsys, monkeypatch):
    """有持仓时正常输出持仓明细和框架分布"""
    seed_position("600036", 45.0, 100, notes="测试招行")
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (
                market_quotes.PriceQuote(
                    50.0, domain.cst_today(), "15:00:00", "fixture"
                )
            )
            for code in codes
        },
    )
    commands_holdings.cmd_portfolio_risk()
    out = capsys.readouterr().out
    assert "600036" in out
    assert "框架分布" in out
    assert "+11.1%" in out
    assert "非账户总仓位" in out
    assert "股票总仓位" not in out
    assert "不判定预算超限" in out
    assert "超预算" not in out


def test_portfolio_risk_labels_explicit_account_weight(capsys, monkeypatch):
    seed_position("600036", 45.0, 100, notes="测试招行")
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (
                market_quotes.PriceQuote(
                    50.0, domain.cst_today(), "15:00:00", "fixture"
                )
            )
            for code in codes
        },
    )

    commands_holdings.cmd_portfolio_risk(["--portfolio-value", "10000"])

    out = capsys.readouterr().out
    assert "股票总仓位：50.0%" in out
    assert "非账户总仓位" not in out


def test_portfolio_risk_labels_partial_quote_weight_as_lower_bound(capsys, monkeypatch):
    seed_position("600036", 45.0, 100, notes="测试招行")
    seed_position("600900", 25.0, 100, notes="测试长电")
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (
                market_quotes.PriceQuote(
                    50.0, domain.cst_today(), "15:00:00", "fixture"
                )
                if code == "600036"
                else None
            )
            for code in codes
        },
    )

    commands_holdings.cmd_portfolio_risk(["--portfolio-value", "10000"])

    out = capsys.readouterr().out
    assert "已取价股票仓位（下限）：50.0%" in out
    assert "股票总仓位：" not in out
    assert "行情覆盖：1/2只" in out


def test_portfolio_risk_rejects_quote_without_date(capsys, monkeypatch):
    seed_position("600036", 45.0, 100, notes="测试招行")
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (market_quotes.PriceQuote(50.0, None, None)) for code in codes
        },
    )
    commands_holdings.cmd_portfolio_risk()
    out = capsys.readouterr().out
    assert "无法计算：所有持仓均缺少股数或实时价格" in out
    assert "+11.1%" not in out


# ── check-holdings 止损预警（P3-4，逐股现价查询）────────────────────────────────


def test_check_holdings_no_holdings(capsys):
    """无持仓时输出'暂无持仓'不报错"""
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "暂无持仓" in out


def test_check_holdings_price_fetch_fails(capsys, monkeypatch):
    """实时取价失败时显示'无实时价格'，不崩溃"""
    seed_position("600036", 40.0, 100, notes="测试")
    mock_price_quotes(monkeypatch, lambda codes: {code: (None) for code in codes})
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "无实时价格" in out


def test_check_holdings_normal(capsys, monkeypatch):
    """现价高于止损线时显示✅正常，不计入预警"""
    seed_position("600036", 40.0, 100, notes="测试")  # 止损15%=34.0 20%=32.0
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (market_quotes.PriceQuote(38.0, domain.cst_today(), "15:00:00"))
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "✅ 正常" in out
    assert "无预警" in out


def test_check_holdings_warns_below_15pct(capsys, monkeypatch):
    """现价跌破15%止损线但未到20%时触发⚠️黄色预警"""
    seed_position("600036", 40.0, 100, notes="测试")  # 止损15%=34.0 20%=32.0
    monkeypatch.setattr(domain, "is_a_share_trading_hours", lambda _now: False)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (market_quotes.PriceQuote(33.0, domain.cst_today(), "15:00:00"))
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "⚠️ 已跌破15%止损线" in out
    assert "共 1 项预警" in out


def test_check_holdings_alerts_below_20pct(capsys, monkeypatch):
    """现价跌破20%止损线时触发🔴红色预警"""
    seed_position("600036", 40.0, 100, notes="测试")  # 止损15%=34.0 20%=32.0
    monkeypatch.setattr(domain, "is_a_share_trading_hours", lambda _now: False)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (market_quotes.PriceQuote(31.0, domain.cst_today(), "15:00:00"))
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "🔴 已跌破20%止损线" in out
    assert "P3:incomplete(market_snapshot_incomplete)" in out
    assert "建议立即止损" not in out


def test_check_holdings_skips_closed_positions(capsys, monkeypatch):
    """已平仓持仓不参与止损检查"""
    holding = seed_position("600036", 40.0, 100, notes="测试")
    seed_event(holding, "sell", "2026-01-06", shares=100, price=50.0, close=True)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (market_quotes.PriceQuote(31.0, domain.cst_today(), "15:00:00"))
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "暂无持仓" in out


# ── check-holdings 行情新鲜度校验（BUG-006 回归）────────────────────────────


class _FixedDatetime(datetime):
    """monkeypatch cache.datetime 用，固定 now() 返回值以控制交易时段判断。"""

    _fixed = datetime(2026, 7, 2, 10, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def test_check_holdings_stale_quote_is_observation_only(capsys, monkeypatch):
    """非交易时段拿到上一交易日收盘价时，只输出观察提醒，不计入预警"""
    seed_position("600036", 40.0, 100, notes="测试")  # 止损15%=34.0 20%=32.0
    _FixedDatetime._fixed = datetime(2026, 7, 2, 1, 30)  # 周四凌晨，非交易时段
    monkeypatch.setattr(domain, "datetime", _FixedDatetime)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (
                market_quotes.PriceQuote(
                    price=31.0, quote_date="2026-07-01", quote_time="15:00:00"
                )
            )
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "📋 上一交易日收盘价观察提醒（2026-07-01收盘31.00" in out
    assert "无预警" in out
    assert "🔴" not in out
    assert "⚠️" not in out


def test_check_holdings_unknown_quote_timestamp_is_not_actionable(capsys, monkeypatch):
    seed_position("600036", 40.0, 100, notes="测试")
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (
                market_quotes.PriceQuote(price=31.0, quote_date=None, quote_time=None)
            )
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "行情时间戳不可验证" in out
    assert "无预警" in out
    assert "建议立即止损" not in out


def test_check_holdings_intraday_breach_uses_alarm_prefix(capsys, monkeypatch):
    """交易时段内跌破止损线，文案带 🚨 盘中已跌破 前缀，仍用"现价" """
    seed_position("600036", 40.0, 100, notes="测试")  # 止损15%=34.0 20%=32.0
    _FixedDatetime._fixed = datetime(2026, 7, 2, 10, 0)  # 周四盘中
    monkeypatch.setattr(domain, "datetime", _FixedDatetime)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (
                market_quotes.PriceQuote(
                    price=31.0, quote_date="2026-07-02", quote_time="10:00:00"
                )
            )
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "🚨 盘中已跌破20%止损线" in out
    assert "现价31.00" in out
    assert "共 1 项预警" in out


def test_check_holdings_after_hours_breach_uses_close_price_wording(
    capsys, monkeypatch
):
    """收盘后跌破止损线，文案用"收盘价"而不是"现价"，图标沿用历史 🔴/⚠️"""
    seed_position("600036", 40.0, 100, notes="测试")  # 止损15%=34.0 20%=32.0
    _FixedDatetime._fixed = datetime(2026, 7, 2, 16, 0)  # 周四收盘后
    monkeypatch.setattr(domain, "datetime", _FixedDatetime)
    mock_price_quotes(
        monkeypatch,
        lambda codes: {
            code: (
                market_quotes.PriceQuote(
                    price=31.0, quote_date="2026-07-02", quote_time="15:00:00"
                )
            )
            for code in codes
        },
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "🔴 已跌破20%止损线（32.000），收盘价31.00，暂不可执行" in out
    assert "P3:incomplete(market_snapshot_incomplete)" in out
    assert "现价31.00" not in out
    assert "共 1 项预警" in out


def test_is_a_share_trading_hours_boundaries():
    """A股交易时段边界：开盘前/开盘/午休/收盘/收盘后/周末"""

    def thu(h, m):
        return datetime(2026, 7, 2, h, m)  # 2026-07-02 是周四

    assert domain.is_a_share_trading_hours(thu(9, 29)) is False
    assert domain.is_a_share_trading_hours(thu(9, 30)) is True
    assert domain.is_a_share_trading_hours(thu(11, 30)) is True
    assert domain.is_a_share_trading_hours(thu(12, 0)) is False
    assert domain.is_a_share_trading_hours(thu(13, 0)) is True
    assert domain.is_a_share_trading_hours(thu(15, 0)) is True
    assert domain.is_a_share_trading_hours(thu(15, 1)) is False
    assert domain.is_a_share_trading_hours(datetime(2026, 7, 4, 10, 0)) is False  # 周六


def test_watchlist_json_marks_pending_refresh(capsys, monkeypatch):
    """Structured watchlist output exposes refresh state without table parsing."""
    set_valid_fundamentals("600036", "招商银行", "银行", {"pe_ttm": 5.5, "pb": 0.8})
    record_valid_quote("600036")
    commands_admin.cmd_watchlist(["--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["code"] == "600036"
    assert rows[0]["pe_static"] == rows[0]["pe_ttm"] == 5.5
    assert rows[0]["needs_refresh"] is True


def test_watchlist_breakdown_nested_schema(capsys):
    set_valid_fundamentals("600036", "招商银行", "银行", {"pe_ttm": 5.5, "pb": 0.8})
    today = domain.cst_today()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO analysis_results "
        "(code, date, name, result, created_at, score, score_breakdown) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "600036",
            today,
            "招商银行",
            "分析结论",
            datetime.now().isoformat(),
            69,
            json.dumps(
                {
                    "fundamentals": {
                        "roe_3y": 8,
                        "growth": 6,
                        "debt": 10,
                        "dividend": 13,
                        "moat": 9,
                        "position": 5,
                        "subtotal": 51,
                    },
                    "timing": {
                        "valuation": 14,
                        "sentiment": 4,
                        "subtotal": 18,
                    },
                    "total": 69,
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    commands_admin.cmd_watchlist(["--breakdown"])
    out = capsys.readouterr().out

    assert "招商银行(600036)" in out
    assert "  分项: 基本面 51/60 | 择时 18/20 | 合计 69/80" in out


def test_watchlist_breakdown_flat_schema(capsys):
    set_valid_fundamentals("600519", "贵州茅台", "白酒", {"pe_ttm": 22.1, "pb": 6.2})
    today = domain.cst_today()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO analysis_results "
        "(code, date, name, result, created_at, score, score_breakdown) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "600519",
            today,
            "贵州茅台",
            "分析结论",
            datetime.now().isoformat(),
            55,
            json.dumps({"roe": 10, "valuation": 8, "total": 55}),
        ),
    )
    conn.commit()
    conn.close()

    commands_admin.cmd_watchlist(["--breakdown"])
    out = capsys.readouterr().out

    assert "贵州茅台(600519)" in out
    assert "  分项: 合计 55" in out


def test_watchlist_no_breakdown_unchanged(capsys):
    set_valid_fundamentals("600036", "招商银行", "银行", {"pe_ttm": 5.5, "pb": 0.8})
    today = domain.cst_today()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO analysis_results "
        "(code, date, name, result, created_at, score, score_breakdown) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "600036",
            today,
            "招商银行",
            "分析结论",
            datetime.now().isoformat(),
            62,
            json.dumps({"fundamentals": 48, "timing": 14, "total": 62}),
        ),
    )
    conn.commit()
    conn.close()

    commands_admin.cmd_watchlist([])
    out = capsys.readouterr().out

    assert "招商银行(600036)" in out
    assert "  分项:" not in out


@pytest.mark.parametrize(
    "keyword,expected_framework",
    [
        ("银行", "B银行"),
        ("保险", "A通用"),
        ("券商", "A通用"),
        ("煤炭", "C资源"),
        ("石油", "C资源"),
        ("天然气", "C资源"),
        ("有色金属", "C资源"),
        ("铜", "C资源"),
        ("钢铁", "C资源"),
        ("采矿", "C资源"),
        ("水电", "D公用"),
        ("电网", "D公用"),
        ("水务", "D公用"),
        ("燃气", "D公用"),
        ("高速", "D公用"),
        ("公用事业", "D公用"),
        ("白酒", "E消费"),
        ("消费", "E消费"),
        ("食品", "E消费"),
        ("零售", "E消费"),
        ("饮料", "E消费"),
        ("互联网", "F科技"),
        ("软件", "F科技"),
        ("科技", "F科技"),
        ("半导体", "F科技"),
        ("游戏", "F科技"),
        ("通信", "F科技"),
    ],
)
def test_infer_framework_exhaustive_keyword_coverage(keyword, expected_framework):
    """穷举现有全部26个行业关键词，逐一核对infer_framework()改读registry前后
    输出完全一致——这是真实持仓止损生产路径，不能只抽样验证几个。"""
    framework, confident = domain.infer_framework(keyword)
    assert framework == expected_framework
    assert confident is (keyword not in {"保险", "券商"})


def test_infer_framework_zijin_mining_actual_sample_maps_to_c():
    """紫金矿业(601899)经 fetcher 实测返回 industry='铜'，必须路由 C资源。"""
    framework, confident = domain.infer_framework("铜")
    assert framework == "C资源"
    assert confident is True
    assert domain.get_stop_loss_pct("C资源") == (0.82, 0.75)


def test_infer_framework_changjiang_dianli_real_holding_maps_to_d():
    """长江电力(600900)真实持仓的industry字段实际值是'水力发电'，必须能匹配到
    D公用框架——这是2026-06-24发现的真实bug：原D公用关键词只有'水电'，不是
    '水力发电'的连续子串，导致静默落到A通用默认止损系数（应该是12%/18%更紧，
    被静默算成15%/20%），且confident=True看起来像判断对了，实际是错的。"""
    framework, confident = domain.infer_framework("水力发电")
    assert framework == "D公用"
    assert confident is True
    assert domain.get_stop_loss_pct("D公用") == (0.88, 0.82)


@pytest.mark.parametrize(
    "framework,expected",
    [
        ("B银行", (0.88, 0.82)),
        ("C资源", (0.82, 0.75)),
        ("D公用", (0.88, 0.82)),
        ("F科技", (0.80, 0.72)),
        ("A通用", (0.85, 0.80)),
        ("E消费", (0.85, 0.80)),
    ],
)
def test_get_stop_loss_pct_all_six_frameworks(framework, expected):
    """穷举全部6个框架（4个有专属止损系数+2个走默认值），逐一核对
    get_stop_loss_pct()改读显式routing catalog前后输出完全一致。"""
    assert domain.get_stop_loss_pct(framework) == expected


def test_infer_framework_works_in_subprocess_without_checklist_preimported():
    """生产CLI入口（add-holding/portfolio-risk）调cache.infer_framework()时，
    那个进程里checklist.py从未被import过（cmd_checklist()那次懒加载没有被触发）。
    必须用子进程隔离验证infer_framework()不依赖checklist导入副作用，不能依赖
    测试套件里某个其他文件先import了checklist。"""
    script = (
        "from a_share_research.policy.v0 import domain; "
        "fw, confident = domain.infer_framework('煤炭开采'); "
        "assert fw == 'C资源' and confident is True, (fw, confident); "
        "sl = domain.get_stop_loss_pct('F科技'); "
        "assert sl == (0.80, 0.72), sl"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
