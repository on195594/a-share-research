"""Read-only holdings views, stop checks, watchlist output and framework routing."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from a_share_research.policy.v0 import commands_admin, commands_holdings, db, domain, market_quotes
from policy_v0.helpers import (
    mock_fixed_quote,
    seed_event,
    seed_position,
    set_valid_fundamentals,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_PY = PROJECT_ROOT / "a_share_research" / "policy" / "v0" / "cache.py"


def test_cli_valid_command_smoke(isolated_cache_database):
    """Subprocess smoke test for the real CLI dispatch path."""
    isolated_db = str(isolated_cache_database)
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


def test_cli_invalid_command_fails(isolated_cache_database):
    """Unknown CLI commands should fail visibly."""
    isolated_db = str(isolated_cache_database)
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


# ── 框架推断：confident 标志区分真实判断与数据缺失兜底 ─────────────────────────


@pytest.mark.parametrize("industry", [None, "", "未知", "未知（新浪fallback）"])
def test_infer_framework_unknown_industry_not_confident(industry):
    """industry 缺失/未知时，confident 必须为 False，跟真实判断区分开"""
    framework, confident = domain.infer_framework(industry)
    assert framework == "A通用"
    assert confident is False


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


@pytest.mark.parametrize("closed,sale,pnl,percentage", [
    (False, "0.00", "+200.00", "+10.00%"),
    (True, "1800.00", "-200.00", "-10.00%"),
])
def test_position_return_uses_latest_lifecycle(capsys, closed, sale, pnl, percentage):
    first = seed_position("600036", 10.0, 100, day="2025-01-01")
    seed_event(first, "sell", "2025-02-01", shares=100, price=12.0, close=True)
    latest = seed_position("600036", 20.0, 100, day="2026-06-01")
    if closed:
        seed_event(latest, "sell", "2026-07-01", shares=100, price=18.0, close=True)
    commands_holdings.cmd_position_return(["600036"] if closed else ["600036", "22.0"])
    out = capsys.readouterr().out
    assert "投入:2000.00" in out
    assert f"卖出回款:{sale}" in out and f"盈亏:{pnl}元" in out
    assert f"总回报：{percentage}" in out


def test_bse_920_uses_the_beijing_quote_prefix():
    assert [market_quotes.sina_query_prefix(c) for c in ("920189", "430570", "872925", "600036", "000001", "300750")] == ["bj", "bj", "bj", "sh", "sz", "sz"]


def test_portfolio_risk_no_holdings(capsys):
    """真实空仓不等于缺失数据库：先建立空 fixture schema。"""
    db.get_db().close()
    commands_holdings.cmd_portfolio_risk()
    out = capsys.readouterr().out
    assert "暂无持仓" in out


def test_portfolio_risk_with_holdings(capsys, monkeypatch):
    """有持仓时正常输出持仓明细和框架分布"""
    seed_position("600036", 45.0, 100, notes="测试招行")
    mock_fixed_quote(monkeypatch, 50.0, domain.cst_today(), "15:00:00", "fixture")
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
    mock_fixed_quote(monkeypatch, 50.0, domain.cst_today(), "15:00:00", "fixture")

    commands_holdings.cmd_portfolio_risk(["--portfolio-value", "10000"])

    out = capsys.readouterr().out
    assert "股票总仓位：50.0%" in out
    assert "非账户总仓位" not in out


def test_portfolio_risk_labels_partial_quote_weight_as_lower_bound(capsys, monkeypatch):
    seed_position("600036", 45.0, 100, notes="测试招行")
    seed_position("600900", 25.0, 100, notes="测试长电")
    mock_fixed_quote(monkeypatch, 50.0, domain.cst_today(), "15:00:00", "fixture", codes={"600036"})

    commands_holdings.cmd_portfolio_risk(["--portfolio-value", "10000"])

    out = capsys.readouterr().out
    assert "已取价股票仓位（下限）：50.0%" in out
    assert "股票总仓位：" not in out
    assert "行情覆盖：1/2只" in out


def test_portfolio_risk_rejects_quote_without_date(capsys, monkeypatch):
    seed_position("600036", 45.0, 100, notes="测试招行")
    mock_fixed_quote(monkeypatch, 50.0, None, None)
    commands_holdings.cmd_portfolio_risk()
    out = capsys.readouterr().out
    assert "无法计算：所有持仓均缺少股数或实时价格" in out
    assert "+11.1%" not in out


# ── check-holdings 止损预警（P3-4，逐股现价查询）────────────────────────────────


def test_check_holdings_skips_closed_positions(capsys, monkeypatch):
    """已平仓持仓不参与止损检查"""
    holding = seed_position("600036", 40.0, 100, notes="测试")
    seed_event(holding, "sell", "2026-01-06", shares=100, price=50.0, close=True)
    mock_fixed_quote(monkeypatch, 31.0, domain.cst_today(), "15:00:00")
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


def test_check_holdings_unknown_quote_timestamp_is_not_actionable(capsys, monkeypatch):
    seed_position("600036", 40.0, 100, notes="测试")
    mock_fixed_quote(monkeypatch, price=31.0, quote_date=None, quote_time=None)
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "行情时间戳不可验证" in out
    assert "无预警；1 只持仓无当日有效报价，未判定止损" in out
    assert "所有持仓价格在止损线之上" not in out
    assert "建议立即止损" not in out


def test_check_holdings_mixed_unpriced_and_stale_quotes_lists_both(capsys, monkeypatch):
    seed_position("600036", 40.0, 100, notes="测试")
    seed_position("600900", 30.0, 100, notes="测试")
    _FixedDatetime._fixed = datetime(2026, 7, 2, 1, 30)
    monkeypatch.setattr(domain, "datetime", _FixedDatetime)
    mock_fixed_quote(
        monkeypatch, price=31.0, quote_date="2026-07-01", quote_time="15:00:00", codes={"600036"}
    )
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "无实时价格" in out and "无当日有效报价" in out
    assert "2026-07-01收盘31.00" in out and "未判定止损" in out
    assert "所有持仓价格在止损线之上" not in out


def test_check_holdings_after_hours_breach_uses_close_price_wording(
    capsys, monkeypatch
):
    """收盘后跌破止损线，文案用"收盘价"而不是"现价"，图标沿用历史 🔴/⚠️"""
    seed_position("600036", 40.0, 100, notes="测试")  # 止损15%=34.0 20%=32.0
    _FixedDatetime._fixed = datetime(2026, 7, 2, 16, 0)  # 周四收盘后
    monkeypatch.setattr(domain, "datetime", _FixedDatetime)
    mock_fixed_quote(monkeypatch, price=31.0, quote_date="2026-07-02", quote_time="15:00:00")
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "跌破20%止损线" in out and "收盘价31.00" in out
    assert "暂不可执行" in out
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


@pytest.mark.parametrize("breakdown,score,labels", [
    ({"fundamentals": {"subtotal": 51}, "timing": {"subtotal": 18}, "total": 69},
     69, ("51/60", "18/20", "69/80")),
    ({"roe": 10, "valuation": 8, "total": 55}, 55, ("合计 55",)),
])
def test_watchlist_breakdown_reads_saved_schemas(capsys, breakdown, score, labels):
    set_valid_fundamentals("600036", "招商银行", "银行", {"pe_ttm": 5.5, "pb": 0.8})
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO analysis_results
               (code, date, name, result, created_at, score, score_breakdown)
               VALUES ('600036', ?, '招商银行', '分析结论', ?, ?, ?)""",
            (domain.cst_today(), datetime.now().isoformat(), score, json.dumps(breakdown)),
        )
        conn.commit()
    commands_admin.cmd_watchlist([])
    compact = capsys.readouterr().out
    assert "招商银行(600036)" in compact and "分项" not in compact
    commands_admin.cmd_watchlist(["--breakdown"])
    detail = capsys.readouterr().out
    assert "招商银行(600036)" in detail and all(value in detail for value in labels)


@pytest.mark.parametrize(
    "keywords,framework,confident",
    [
        (("保险", "券商"), "A通用", False),
        (("银行",), "B银行", True),
        (("煤炭", "石油", "天然气", "有色金属", "钢铁", "采矿"), "C资源", True),
        (("水电", "电网", "水务", "燃气", "高速", "公用事业"), "D公用", True),
        (("白酒", "消费", "食品", "零售", "饮料"), "E消费", True),
        (("互联网", "软件", "科技", "游戏", "通信"), "F科技", True),
    ],
)
def test_infer_framework_exhaustive_keyword_coverage(keywords, framework, confident):
    for keyword in keywords:
        assert domain.infer_framework(keyword) == (framework, confident), keyword


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
