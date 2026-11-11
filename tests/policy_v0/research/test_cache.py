"""
cache.py 单元测试
覆盖本次修复的核心场景：持仓生命周期、FIFO平仓、WAL模式、TTL推断、缓存命中检测、分数显示。
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from a_share_research.policy.v0 import (
    commands_admin,
    commands_analysis,
    commands_holdings,
    commands_monitor,
    db,
    domain,
    market_quotes,
    store,
)
from policy_v0.helpers import (
    mock_price_quotes,
    record_valid_quote,
    set_valid_analysis,
    set_valid_fundamentals,
    valid_decision_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_PY = PROJECT_ROOT / "a_share_research" / "policy" / "v0" / "cache.py"
LEGACY_CYCLE_STAGE_TAG = '周期位置[阶段=上行期；依据="煤价中枢回升且供给侧收缩"]'


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每个测试用独立临时数据库，不影响生产 cache.db"""
    db_file = tmp_path / "test_cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(db_file))
    monkeypatch.setattr(
        store,
        "get_latest_quote_snapshot",
        lambda *args, **kwargs: {
            "price": 10.0,
            "quote_as_of": f"{domain.cst_today()}T10:00:00",
            "source": "sina",
        },
    )
    monkeypatch.setattr(
        store,
        "get_market_indicator_snapshot",
        lambda *args, **kwargs: {
            "value": 1.8,
            "as_of": domain.cst_today(),
            "source": "fixture",
            "status": "ok",
        },
    )
    yield str(db_file)


# ── WAL 模式 ──────────────────────────────────────────────────────────────────


def test_wal_mode_enabled():
    conn = db.get_db()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


# ── 行业 TTL 推断 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "industry,expected_ttl",
    [
        ("国有大行", 72),
        ("城商行", 72),
        ("保险", 72),
        ("水电", 72),
        ("公用事业", 72),
        ("高速公路", 72),
        ("白酒", 12),
        ("消费品", 12),
        ("食品饮料", 12),
        ("零售", 12),
        ("半导体", 24),  # 默认
        ("医药", 24),  # 默认
        ("新能源", 24),  # 默认
    ],
)
def test_industry_ttl(industry, expected_ttl):
    assert domain.get_industry_ttl(industry) == expected_ttl


# ── 缓存过期逻辑 ───────────────────────────────────────────────────────────────


def test_is_expired_fresh():
    updated = (datetime.now() - timedelta(hours=1)).isoformat()
    assert not domain.is_expired(updated, 24)


def test_is_expired_stale():
    updated = (datetime.now() - timedelta(hours=25)).isoformat()
    assert domain.is_expired(updated, 24)


def test_is_expired_boundary():
    # 恰好等于 TTL 已过期
    updated = (datetime.now() - timedelta(hours=24, minutes=1)).isoformat()
    assert domain.is_expired(updated, 24)


# ── 基本面缓存：get/set + 过期返回 None ──────────────────────────────────────


def test_set_and_get_fundamentals():
    msg = set_valid_fundamentals("600000", "浦发银行", "国有大行", {"pe": 5.2})
    assert "浦发银行" in msg
    assert "TTL:72h" in msg

    result = store.get_fundamentals("600000")
    assert result is not None
    assert result["pe"] == 5.2
    assert result["_cache_meta"]["code"] == "600000"


def test_get_fundamentals_miss():
    assert store.get_fundamentals("000001") is None


def test_get_fundamentals_expired(monkeypatch):
    set_valid_fundamentals("000002", "平安银行", "股份制银行", {"pb": 0.6})
    # 模拟缓存已过期
    monkeypatch.setattr(domain, "is_expired", lambda *_: True)
    assert store.get_fundamentals("000002") is None


# ── cmd_check：三种命中状态 ──────────────────────────────────────────────────


def test_cmd_check_full_miss(capsys):
    commands_analysis.cmd_check(["999999"])
    out = capsys.readouterr().out
    assert out.strip() == "FULL_MISS"


def test_cmd_check_analysis_hit(capsys, monkeypatch):
    code = "600036"
    set_valid_analysis(monkeypatch, code=code, narrative="结论：买入")
    capsys.readouterr()

    commands_analysis.cmd_check([code])
    out = capsys.readouterr().out
    assert out.startswith("ANALYSIS_HIT")
    assert "结论：买入" in out


def test_cmd_check_analysis_requires_fresh_quote(capsys, monkeypatch):
    code = "600036"
    set_valid_analysis(monkeypatch, code=code)
    monkeypatch.setattr(
        store, "get_latest_quote_snapshot", lambda *args, **kwargs: None
    )
    capsys.readouterr()

    commands_analysis.cmd_check([code])

    out = capsys.readouterr().out
    assert "ANALYSIS_HIT" not in out
    assert "ANALYSIS_PRICE_STALE" in out


def test_cmd_check_fundamentals_hit(capsys):
    set_valid_fundamentals("601318", "中国平安", "保险", {"roe": 15})
    commands_analysis.cmd_check(["601318"])
    out = capsys.readouterr().out
    assert out.startswith("FUNDAMENTALS_HIT")
    assert "601318" in out


def test_cmd_check_exposes_decision_and_leaf_completeness(capsys):
    set_valid_fundamentals("002475", "立讯精密", "元器件", {"roe_3y_avg": 20})

    commands_analysis.cmd_check(["002475"])

    lines = capsys.readouterr().out.splitlines()
    payload = json.loads("\n".join(lines[1:]))
    meta = payload["_decision_meta"]
    assert meta["framework_candidate"] == "A通用"
    assert meta["scoring_status"] == "incomplete"
    assert meta["timing_status"] == "incomplete"
    assert meta["action_eligible"] is False
    assert meta["data_completeness"] == {
        "top_level_present": 1,
        "top_level_total": 1,
        "risk_gates_complete": 0,
        "risk_gates_total": 3,
    }


def test_newer_bps_only_blocks_pb_percentile_frameworks() -> None:
    clear = {"status": "clear", "action_eligible": True, "reason_code": "clear"}
    data = {
        "regulatory_gate": clear,
        "roe_structural_gate": clear,
        "cash_flow_gate": clear,
        "valuation_compatibility": {
            "status": "latest_report_update",
            "timing_eligible": True,
            "pb_percentile_eligible": False,
        },
    }

    general = commands_analysis._decision_meta(data, "元器件")
    bank = commands_analysis._decision_meta(data, "银行")

    assert general["timing_status"] == "not_evaluated"
    assert bank["timing_status"] == "incomplete"
    assert "valuation_compatibility:pb_percentile_ineligible" in bank["reason_codes"]

    data["valuation_compatibility"] = {
        "status": "incomplete",
        "timing_eligible": False,
        "pb_percentile_eligible": False,
        "reason_code": "same_period_value_mismatch",
    }
    conflict = commands_analysis._decision_meta(data, "元器件")
    assert conflict["timing_status"] == "incomplete"
    assert (
        "valuation_compatibility:same_period_value_mismatch" in conflict["reason_codes"]
    )


@pytest.mark.parametrize(
    ("dps", "eps", "expects_pb_requirement"),
    [
        (0.39, 1.0, True),
        (0.40, 1.0, True),
        (0.41, 1.0, False),
        (-0.1, 1.0, False),
        (None, 1.0, False),
        (float("nan"), 1.0, False),
        (float("inf"), 1.0, False),
    ],
)
def test_resource_pb_requirement_uses_owner_strict_payout_contract(
    monkeypatch, dps, eps, expects_pb_requirement
) -> None:
    clear = {"status": "clear", "action_eligible": True, "reason_code": "clear"}
    data = {
        "dps": dps,
        "eps": eps,
        "regulatory_gate": clear,
        "roe_structural_gate": clear,
        "cash_flow_gate": clear,
        "valuation_compatibility": {
            "timing_eligible": True,
            "pb_percentile_eligible": False,
        },
    }
    calls = []
    real_ratio = commands_analysis.framework_scoring.calculate_payout_ratio
    real_classify = commands_analysis.framework_scoring.classify_framework_rule

    def ratio(raw_dps, raw_eps):
        calls.append(("ratio", raw_dps, raw_eps))
        return real_ratio(raw_dps, raw_eps)

    def classify(framework, rule_id, value):
        calls.append(("classify", framework, rule_id, value))
        return real_classify(framework, rule_id, value)

    monkeypatch.setattr(
        commands_analysis.framework_scoring, "calculate_payout_ratio", ratio
    )
    monkeypatch.setattr(
        commands_analysis.framework_scoring, "classify_framework_rule", classify
    )

    meta = commands_analysis._decision_meta(data, "煤炭")

    assert calls[0] == ("ratio", dps, eps)
    owner_ratio = real_ratio(dps, eps)
    if owner_ratio is not None:
        assert calls[1] == ("classify", "C", "payout_ratio", owner_ratio)
    else:
        assert len(calls) == 1
    assert (
        "valuation_compatibility:pb_percentile_ineligible" in meta["reason_codes"]
    ) is expects_pb_requirement


def test_bank_decision_meta_normalizes_cash_flow_gate_to_not_applicable() -> None:
    clear = {"status": "clear", "action_eligible": True, "reason_code": "clear"}
    data = {
        "regulatory_gate": clear,
        "roe_structural_gate": clear,
        "cash_flow_gate": {
            "status": "blocked",
            "action_eligible": False,
            "reason_code": "cfo_non_positive",
            "ttm_cfo": -1,
            "ttm_capex": 0,
            "sources": ["official annual report"],
            "as_of": "2026-09-09",
        },
        "valuation_compatibility": {
            "timing_eligible": True,
            "pb_percentile_eligible": True,
        },
    }

    meta = commands_analysis._decision_meta(data, "银行")

    assert meta["scoring_status"] == "not_evaluated"
    assert "cash_flow_gate:cfo_non_positive" not in meta["reason_codes"]
    assert meta["data_completeness"]["risk_gates_complete"] == 3


def test_cmd_check_fundamentals_expired_is_full_miss(capsys, monkeypatch):
    set_valid_fundamentals("601919", "中远海控", "航运", {"pe": 3})
    monkeypatch.setattr(domain, "is_expired", lambda *_: True)
    commands_analysis.cmd_check(["601919"])
    out = capsys.readouterr().out
    assert out.strip() == "FULL_MISS"


def test_cli_valid_command_smoke(isolated_db):
    """Subprocess smoke test for the real CLI dispatch path."""
    result = subprocess.run(
        [sys.executable, str(CACHE_PY), "check", "999999"],
        env={**os.environ, "POLICY_V0_DB": isolated_db},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "FULL_MISS"
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


def test_add_holding_rejects_second_open_position(capsys):
    """同一股票已在仓时必须改用 buy-holding，不能制造重复在仓记录。"""
    commands_holdings.cmd_add_holding(["600519", "50"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        commands_holdings.cmd_add_holding(["600519", "52"])
    err = capsys.readouterr().err

    conn = db.get_db()
    rows = conn.execute(
        "SELECT cost_price FROM holdings WHERE code='600519' AND exit_date IS NULL"
    ).fetchall()
    conn.close()

    assert exc.value.code == 1
    assert "已存在在仓持仓" in err
    assert "buy-holding" in err
    assert rows == [(50.0,)]


def test_add_holding_different_stocks(capsys):
    """不同股票各建仓一次，各有一条记录"""
    commands_holdings.cmd_add_holding(["000001", "10.5"])
    commands_holdings.cmd_add_holding(["000002", "8.2"])

    conn = db.get_db()
    cnt = conn.execute(
        "SELECT COUNT(*) FROM holdings WHERE exit_date IS NULL"
    ).fetchone()[0]
    conn.close()
    assert cnt == 2


@pytest.mark.parametrize("cost", ["0", "-1"])
def test_add_holding_rejects_nonpositive_cost_price(cost, capsys):
    """成本价必须为正数，避免后续盈亏计算除以零或失真。"""
    with pytest.raises(SystemExit) as exc:
        commands_holdings.cmd_add_holding(["600519", cost])
    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "成本价必须大于0" in err


def test_add_holding_prefers_persisted_framework_over_inference(capsys):
    """add-holding 应优先用 set-analysis 时持久化的 framework，而不是重新
    用 industry 关键词反推——用一个跟 industry 推断结果不一致的 framework
    验证确实读的是持久化值，不是巧合一致。"""
    conn = db.get_db()
    today = domain.cst_today()
    # industry='银行' 若被反推会得到 B银行，这里持久化的是故意不同的 F科技
    conn.execute(
        "INSERT INTO stock_fundamentals (code, name, industry, data, updated_at) "
        "VALUES ('601899', '紫金矿业', '银行', '{}', ?)",
        (datetime.now().isoformat(),),
    )
    conn.execute(
        "INSERT INTO analysis_results (code, date, framework) VALUES ('601899', ?, 'F科技')",
        (today,),
    )
    conn.commit()
    conn.close()

    commands_holdings.cmd_add_holding(["601899", "10"])
    out = capsys.readouterr().out

    assert "框架:F科技" in out
    assert "框架:F科技?" not in out  # 持久化值=confident，不应带"?"兜底标记

    sl15_pct, sl20_pct = domain.get_stop_loss_pct("F科技")
    conn = db.get_db()
    row = conn.execute(
        "SELECT stop_loss_15, stop_loss_20 FROM holdings WHERE code='601899'"
    ).fetchone()
    conn.close()
    assert row[0] == round(10 * sl15_pct, 3)
    assert row[1] == round(10 * sl20_pct, 3)


def test_add_holding_falls_back_to_inference_with_marker(capsys):
    """没有持久化framework时，应退化到industry反推，且在输出里用"?"标记
    这是兜底值非真实判断"""
    commands_holdings.cmd_add_holding(
        ["000333", "20"]
    )  # 无 analysis_results 记录、无 stock_fundamentals 记录
    out = capsys.readouterr().out
    assert "框架:A通用?(数据缺失，建议核实)" in out


# ── FIFO 平仓 ────────────────────────────────────────────────────────────────


def test_close_holding_fifo(capsys):
    """先买的批次先被平仓（FIFO）"""
    # 建仓两笔，手动指定不同 buy_date
    conn = db.get_db()
    conn.execute(
        "INSERT INTO holdings (code, cost_price, buy_date) VALUES ('300750', 100.0, '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO holdings (code, cost_price, buy_date) VALUES ('300750', 120.0, '2024-06-01')"
    )
    conn.commit()
    conn.close()

    commands_holdings.cmd_close_holding(["300750", "110.0"])

    conn = db.get_db()
    rows = conn.execute(
        "SELECT cost_price, exit_price, exit_date FROM holdings WHERE code='300750' ORDER BY buy_date"
    ).fetchall()
    conn.close()

    # 第一笔（buy_date 早）已平仓
    assert rows[0][1] == 110.0, "最早买入的批次应被优先平仓"
    assert rows[0][2] is not None
    # 第二笔仍在仓
    assert rows[1][1] is None
    assert rows[1][2] is None


def test_close_holding_no_open_position(capsys):
    """平仓不存在的股票应报错退出"""
    with pytest.raises(SystemExit) as exc:
        commands_holdings.cmd_close_holding(["999888", "100.0"])
    assert exc.value.code == 1


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
    commands_holdings.cmd_add_holding(["000858", "100"])

    commands_holdings.cmd_holdings()
    out = capsys.readouterr().out
    assert "1 只" in out
    assert "笔" not in out


def test_holdings_display_two_stocks(capsys):
    """两只不同股票各一笔：显示 '2 只'，不显示 '笔'"""
    commands_holdings.cmd_add_holding(["600519", "1800"])
    commands_holdings.cmd_add_holding(["000858", "100"])

    commands_holdings.cmd_holdings()
    out = capsys.readouterr().out
    assert "2 只" in out
    assert "笔" not in out


# ── cmd_set_score：确认显示 /80 ──────────────────────────────────────────────


def test_set_score_displays_80_scale(capsys):
    """分数应显示 /80，不是 /100（修复过的逻辑）"""
    code = "600036"
    today = domain.cst_today()
    conn = db.get_db()
    conn.execute(
        "INSERT INTO analysis_results (code, date, result, created_at) VALUES (?,?,?,?)",
        (code, today, "测试结论", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    commands_analysis.cmd_set_score([code, "55"])
    out = capsys.readouterr().out
    assert "55/80" in out
    assert "/100" not in out


def test_set_score_clears_breakdown_that_no_longer_matches_total(capsys, monkeypatch):
    set_valid_analysis(monkeypatch, code="600036", score=62)
    commands_analysis.cmd_set_score_breakdown(
        [
            "600036",
            '{"fundamentals": {"subtotal": 44}, "timing": {"subtotal": 18}, "total": 62}',
        ]
    )

    commands_analysis.cmd_set_score(["600036", "55"])

    conn = db.get_db()
    row = conn.execute(
        "SELECT score, score_breakdown FROM analysis_results WHERE code='600036'"
    ).fetchone()
    conn.close()
    assert row == (55, None)
    assert "已清除" in capsys.readouterr().out


def test_set_score_no_analysis_record(capsys):
    """无今日分析记录时 set-score 应报错退出"""
    with pytest.raises(SystemExit) as exc:
        commands_analysis.cmd_set_score(["888888", "60"])
    assert exc.value.code == 1


# ── set-analysis structured decision contract ───────────────────────────────


def test_set_analysis_with_score(capsys, monkeypatch):
    """set-analysis derives human output and score from decision-v1."""
    payload = set_valid_analysis(
        monkeypatch, code="600036", framework="A", score=62, narrative="买入信号明确"
    )

    conn = db.get_db()
    today = domain.cst_today()
    row = conn.execute(
        """SELECT result, decision_json, score
           FROM analysis_results WHERE code='600036' AND date=?""",
        (today,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert "买入信号明确" in row[0]
    assert json.loads(row[1]) == payload
    assert row[2] == 62


def test_set_analysis_without_score(capsys, monkeypatch):
    """A valid not-formed decision stores a NULL score."""
    set_valid_analysis(monkeypatch, code="000001", framework="A", score=None)

    conn = db.get_db()
    today = domain.cst_today()
    row = conn.execute(
        "SELECT score FROM analysis_results WHERE code='000001' AND date=?", (today,)
    ).fetchone()
    conn.close()

    assert row[0] is None


def test_set_analysis_with_framework_persists_column(monkeypatch):
    """set-analysis persists the framework from decision-v1."""
    set_valid_analysis(
        monkeypatch, code="601088", framework="C资源", score=62, cycle=True
    )

    conn = db.get_db()
    today = domain.cst_today()
    row = conn.execute(
        "SELECT framework, score FROM analysis_results WHERE code='601088' AND date=?",
        (today,),
    ).fetchone()
    conn.close()

    assert row == ("C资源", 62)


def test_set_analysis_without_framework_is_rejected(monkeypatch):
    """Missing safety-critical decision fields fail closed before write."""
    payload = valid_decision_payload("000001", score=55)
    payload.pop("framework")
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        commands_analysis.cmd_set_analysis([])

    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone() == (0,)
    conn.close()


def test_set_analysis_rerun_same_day_preserves_flags_and_score_breakdown(monkeypatch):
    """同一只股票同一天重复执行 set-analysis，不应清空已写入的 flags/score_breakdown"""
    set_valid_analysis(
        monkeypatch, code="600036", framework="A", score=62, narrative="首次分析结论"
    )
    commands_analysis.cmd_set_score_breakdown(
        [
            "600036",
            '{"fundamentals": {"subtotal": 44}, "timing": {"subtotal": 18}, "total": 62}',
        ]
    )
    commands_monitor.cmd_set_flag(["600036", "yellow", "估值偏高"])

    set_valid_analysis(
        monkeypatch,
        code="600036",
        framework="A",
        score=62,
        narrative="重写后的分析结论",
    )

    conn = db.get_db()
    today = domain.cst_today()
    row = conn.execute(
        "SELECT result, score, score_breakdown, flags FROM analysis_results "
        "WHERE code='600036' AND date=?",
        (today,),
    ).fetchone()
    conn.close()

    assert "重写后的分析结论" in row[0]
    assert "首次分析结论" not in row[0]
    assert row[1] == 62
    assert (
        row[2]
        == '{"fundamentals": {"subtotal": 44}, "timing": {"subtotal": 18}, "total": 62}'
    )
    assert row[3] is not None and "估值偏高" in row[3]


def test_set_analysis_rerun_with_new_score_overwrites_score(monkeypatch):
    """重新执行 set-analysis 时显式传入新得分，应覆盖旧得分"""
    set_valid_analysis(
        monkeypatch, code="600519", framework="A", score=50, narrative="首次分析结论"
    )
    set_valid_analysis(
        monkeypatch,
        code="600519",
        framework="A",
        score=70,
        narrative="重写后的分析结论",
    )

    conn = db.get_db()
    today = domain.cst_today()
    row = conn.execute(
        "SELECT score FROM analysis_results WHERE code='600519' AND date=?", (today,)
    ).fetchone()
    conn.close()

    assert row[0] == 70


def test_set_analysis_rescore_clears_stale_score_breakdown(monkeypatch, capsys):
    set_valid_analysis(monkeypatch, code="600519", framework="A", score=50)
    commands_analysis.cmd_set_score_breakdown(
        [
            "600519",
            '{"fundamentals": {"subtotal": 35}, "timing": {"subtotal": 15}, "total": 50}',
        ]
    )

    set_valid_analysis(monkeypatch, code="600519", framework="A", score=70)

    conn = db.get_db()
    row = conn.execute(
        "SELECT score, score_breakdown FROM analysis_results WHERE code='600519'"
    ).fetchone()
    conn.close()
    assert row == (70, None)
    assert "分析结论已缓存" in capsys.readouterr().out


def test_set_analysis_rerun_with_same_score_preserves_previous_score(monkeypatch):
    """A same-score structured rerun preserves the authoritative score."""
    set_valid_analysis(
        monkeypatch, code="601318", framework="A", score=65, narrative="首次分析结论"
    )
    set_valid_analysis(
        monkeypatch,
        code="601318",
        framework="A",
        score=65,
        narrative="重写后的分析结论",
    )

    conn = db.get_db()
    today = domain.cst_today()
    row = conn.execute(
        "SELECT result, score FROM analysis_results WHERE code='601318' AND date=?",
        (today,),
    ).fetchone()
    conn.close()

    assert "重写后的分析结论" in row[0]
    assert row[1] == 65


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


# ── decision-v1 validation and machine-state independence ─────────────────────


def test_set_analysis_accepts_valid_decision_json(monkeypatch):
    payload = set_valid_analysis(
        monkeypatch,
        code="600036",
        framework="A通用",
        score=62,
        narrative="普通分析结论，不依赖 Markdown 标签。",
    )

    conn = db.get_db()
    today = domain.cst_today()
    row = conn.execute(
        """SELECT result, decision_json, framework, score
           FROM analysis_results WHERE code='600036' AND date=?""",
        (today,),
    ).fetchone()
    conn.close()

    assert "普通分析结论，不依赖 Markdown 标签。" in row[0]
    assert json.loads(row[1]) == payload
    assert row[2:] == ("A通用", 62)


def test_set_analysis_rejects_malformed_decision_json_before_write(monkeypatch):
    monkeypatch.setattr("sys.stdin", StringIO("not-json"))

    with pytest.raises(SystemExit) as exc_info:
        commands_analysis.cmd_set_analysis([])

    assert exc_info.value.code == 1
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize(
    ("framework", "code"),
    [("B银行", "600036"), ("C资源", "601088"), ("D公用", "600900")],
)
def test_cycle_stage_is_not_inferred_from_human_narrative(framework, code, monkeypatch):
    payload = set_valid_analysis(
        monkeypatch,
        code=code,
        framework=framework,
        score=62,
        cycle=False,
        narrative=LEGACY_CYCLE_STAGE_TAG,
    )

    conn = db.get_db()
    row = conn.execute(
        "SELECT result, decision_json FROM analysis_results WHERE code=?", (code,)
    ).fetchone()
    conn.close()

    assert LEGACY_CYCLE_STAGE_TAG in row[0]
    assert json.loads(row[1]) == payload
    assert json.loads(row[1]).get("cycle_stage") is None


@pytest.mark.parametrize(
    ("framework", "code"),
    [("B银行", "600037"), ("C资源", "601088"), ("D公用", "600901")],
)
def test_structured_cycle_stage_is_persisted(framework, code, monkeypatch):
    payload = set_valid_analysis(
        monkeypatch, code=code, framework=framework, score=62, cycle=True
    )

    conn = db.get_db()
    row = conn.execute(
        "SELECT decision_json FROM analysis_results WHERE code=?", (code,)
    ).fetchone()
    conn.close()

    assert json.loads(row[0]) == payload
    assert json.loads(row[0])["cycle_stage"]["stage"] == "上行期"


def test_a_framework_without_cycle_stage_stays_valid(monkeypatch, capsys):
    set_valid_analysis(
        monkeypatch, code="000001", framework="A通用", score=55, cycle=False
    )

    conn = db.get_db()
    row = conn.execute(
        "SELECT decision_json FROM analysis_results WHERE code='000001'"
    ).fetchone()
    conn.close()

    assert json.loads(row[0]).get("cycle_stage") is None
    assert "周期位置" not in capsys.readouterr().err


# ── 旧表迁移：无 id 列时触发 AUTOINCREMENT 迁移 ──────────────────────────────


def test_update_return_writes_pct_and_days(capsys, monkeypatch):
    """update-return 写入 return_pct 和 holding_days"""
    set_valid_analysis(monkeypatch, code="600519", narrative="买入")

    import datetime as dt

    class YesterdayLocalDate(dt.date):
        @classmethod
        def today(cls):
            return cls.fromisoformat(domain.cst_today()) - timedelta(days=1)

    # A UTC host can still be on yesterday while the A-share ledger is on today.
    monkeypatch.setattr(dt, "date", YesterdayLocalDate)
    commands_holdings.cmd_update_return(["600519", "18.5"])

    today = domain.cst_today()
    conn = db.get_db()
    row = conn.execute(
        "SELECT return_pct, holding_days FROM analysis_results WHERE code=? AND date=?",
        ("600519", today),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 18.5
    assert row[1] == 0  # 同日填写，持有0天


def test_update_return_no_analysis_record():
    """无分析记录时 update-return 应以错误码退出"""
    with pytest.raises(SystemExit):
        commands_holdings.cmd_update_return(["999999", "10.0"])


@pytest.mark.parametrize("value", ["abc", "nan", "inf"])
def test_update_return_invalid_value(value):
    """非数字或非有限回报率应以错误码退出"""
    with pytest.raises(SystemExit):
        commands_holdings.cmd_update_return(["600519", value])


# ── 结构化持仓账本与状态 ───────────────────────────────────────────────────────


def test_add_holding_persists_framework_initial_shares_and_buy_event():
    commands_holdings.cmd_add_holding(
        [
            "000001",
            "10.0",
            "500",
            "--fee",
            "5",
            "--date",
            "2026-07-01",
        ]
    )
    conn = db.get_db()
    holding = conn.execute(
        """SELECT framework, framework_confident, initial_shares, cost_price, buy_date
           FROM holdings WHERE code='000001'"""
    ).fetchone()
    event = conn.execute(
        """SELECT event_type, shares, price, fees, event_date
           FROM holding_events WHERE code='000001'"""
    ).fetchone()
    conn.close()
    assert holding == ("A通用", 0, 500, 10.01, "2026-07-01")
    assert event == ("buy", 500, 10.0, 5.0, "2026-07-01")


def test_sell_holding_partial_updates_shares_and_realized_pnl():
    commands_holdings.cmd_add_holding(["000001", "10.0", "500"])
    commands_holdings.cmd_sell_holding(
        [
            "000001",
            "12.0",
            "200",
            "--fee",
            "1",
            "--tax",
            "2",
        ]
    )
    conn = db.get_db()
    holding = conn.execute(
        "SELECT shares, exit_date FROM holdings WHERE code='000001'"
    ).fetchone()
    event = conn.execute(
        """SELECT shares, fees, tax, realized_pnl FROM holding_events
           WHERE code='000001' AND event_type='sell'"""
    ).fetchone()
    conn.close()
    assert holding == (300, None)
    assert event == pytest.approx((200, 1.0, 2.0, 397.0))


def test_buy_holding_preserves_buy_date_and_updates_weighted_cost():
    commands_holdings.cmd_add_holding(["000001", "10.0", "500"])
    conn = db.get_db()
    conn.execute(
        "UPDATE holdings SET buy_date='2025-01-02', main_entry_date='2025-01-02'"
    )
    conn.execute(
        "UPDATE holding_events SET event_date='2025-01-02' "
        "WHERE code='000001' AND event_type='buy'"
    )
    conn.commit()
    conn.close()
    commands_holdings.cmd_buy_holding(
        [
            "000001",
            "12.0",
            "300",
            "--fee",
            "8",
            "--date",
            "2026-07-30",
        ]
    )
    conn = db.get_db()
    row = conn.execute(
        """SELECT shares, cost_price, reference_cost, buy_date, main_entry_date
           FROM holdings WHERE code='000001'"""
    ).fetchone()
    conn.close()
    assert row[0] == 800
    assert row[1] == pytest.approx((5000 + 3600 + 8) / 800)
    assert row[2] == pytest.approx((5000 + 3600) / 800)
    assert row[3] == "2025-01-02"
    assert row[4] == "2026-07-30"


def test_sell_holding_rejects_non_executable_odd_lot_split():
    commands_holdings.cmd_add_holding(["600036", "10.0", "500"])
    with pytest.raises(SystemExit):
        commands_holdings.cmd_sell_holding(["600036", "12.0", "167"])
    conn = db.get_db()
    assert (
        conn.execute("SELECT shares FROM holdings WHERE code='600036'").fetchone()[0]
        == 500
    )
    conn.close()


def test_sell_holding_all_closes_position():
    commands_holdings.cmd_add_holding(["600036", "10.0", "500"])
    commands_holdings.cmd_sell_holding(["600036", "12.0", "all"])
    conn = db.get_db()
    row = conn.execute(
        "SELECT shares, exit_price, exit_date FROM holdings WHERE code='600036'"
    ).fetchone()
    conn.close()
    assert row[0] == 0
    assert row[1] == 12.0
    assert row[2] == domain.cst_today()


def test_record_dividend_creates_cash_flow_event():
    commands_holdings.cmd_add_holding(["600036", "10.0", "500", "--date", "2026-06-01"])
    commands_holdings.cmd_record_dividend(["600036", "123.45", "2026-07-01"])
    conn = db.get_db()
    row = conn.execute(
        """SELECT event_type, event_date, cash_amount FROM holding_events
           WHERE code='600036' AND event_type='dividend'"""
    ).fetchone()
    conn.close()
    assert row == ("dividend", "2026-07-01", 123.45)


def test_corporate_action_separates_economic_and_reference_cost():
    commands_holdings.cmd_add_holding(["600036", "48.0", "500", "--date", "2026-06-01"])
    commands_holdings.cmd_corporate_action(["600036", "0.56", "0.2", "2026-07-01"])
    conn = db.get_db()
    row = conn.execute(
        """SELECT shares, initial_shares, cost_price, reference_cost
           FROM holdings WHERE code='600036'"""
    ).fetchone()
    dividend = conn.execute(
        """SELECT cash_amount FROM holding_events
           WHERE code='600036' AND event_type='dividend'"""
    ).fetchone()[0]
    conn.close()
    assert row[0] == 600
    assert row[1] == 600
    assert row[2] == pytest.approx(40.0)
    assert row[3] == pytest.approx((48.0 - 0.56) / 1.2)
    assert dividend == pytest.approx(280.0)


def test_position_return_includes_fees_tax_dividend_and_open_value(capsys):
    commands_holdings.cmd_add_holding(["600036", "10.0", "500"])
    commands_holdings.cmd_sell_holding(
        [
            "600036",
            "12.0",
            "200",
            "--fee",
            "1",
            "--tax",
            "2",
        ]
    )
    commands_holdings.cmd_record_dividend(["600036", "100"])
    commands_holdings.cmd_position_return(["600036", "11"])
    out = capsys.readouterr().out
    # 2397 sale cash + 100 dividend + 3300 open value - 5000 invested = 797.
    assert "盈亏:+797.00元" in out
    assert "总回报：+15.94%" in out


def test_position_return_isolates_reopened_position_lifecycle(capsys):
    commands_holdings.cmd_add_holding(
        [
            "600036",
            "10.0",
            "100",
            "--date",
            "2025-01-01",
        ]
    )
    commands_holdings.cmd_sell_holding(
        [
            "600036",
            "12.0",
            "all",
            "--date",
            "2025-02-01",
        ]
    )
    capsys.readouterr()
    commands_holdings.cmd_add_holding(
        [
            "600036",
            "20.0",
            "100",
            "--date",
            "2026-07-01",
        ]
    )
    commands_holdings.cmd_position_return(["600036", "22.0"])
    out = capsys.readouterr().out

    assert "投入:2000.00" in out
    assert "卖出回款:0.00" in out
    assert "盈亏:+200.00元" in out
    assert "总回报：+10.00%" in out


def test_position_return_uses_latest_closed_lifecycle(capsys):
    commands_holdings.cmd_add_holding(
        [
            "600036",
            "10.0",
            "100",
            "--date",
            "2025-01-01",
        ]
    )
    commands_holdings.cmd_sell_holding(
        [
            "600036",
            "12.0",
            "all",
            "--date",
            "2025-02-01",
        ]
    )
    commands_holdings.cmd_add_holding(
        [
            "600036",
            "20.0",
            "100",
            "--date",
            "2026-06-01",
        ]
    )
    commands_holdings.cmd_sell_holding(
        [
            "600036",
            "18.0",
            "all",
            "--date",
            "2026-07-01",
        ]
    )
    capsys.readouterr()
    commands_holdings.cmd_position_return(["600036"])
    out = capsys.readouterr().out

    assert "投入:2000.00" in out
    assert "卖出回款:1800.00" in out
    assert "盈亏:-200.00元" in out
    assert "总回报：-10.00%" in out


def test_close_holding_delegates_to_event_ledger(capsys):
    """The compatibility close command must create the same sell event as sell-holding all."""
    commands_holdings.cmd_add_holding(["600036", "10.0", "100", "--date", "2026-01-01"])
    commands_holdings.cmd_close_holding(["600036", "12.0", "2026-02-01"])
    conn = db.get_db()
    holding = conn.execute(
        "SELECT shares, exit_price, exit_date FROM holdings WHERE code='600036'"
    ).fetchone()
    events = conn.execute(
        "SELECT event_type, shares, price FROM holding_events WHERE code='600036' ORDER BY id"
    ).fetchall()
    conn.close()
    assert holding == (0, 12.0, "2026-02-01")
    assert events == [("buy", 100, 10.0), ("sell", 100, 12.0)]

    commands_holdings.cmd_position_return(["600036"])
    out = capsys.readouterr().out
    assert "卖出回款:1200.00" in out
    assert "在仓市值:0.00" in out


def test_close_holding_closes_only_oldest_open_lot():
    """Legacy close-holding keeps FIFO one-lot semantics even for imported duplicate lots."""
    commands_holdings.cmd_add_holding(["600036", "10.0", "100", "--date", "2026-01-01"])
    now = domain.utc_now_iso()
    conn = db.get_db()
    second_id = conn.execute(
        """INSERT INTO holdings
           (code, cost_price, shares, buy_date, updated_at, framework, initial_shares)
           VALUES ('600036', 11.0, 100, '2026-01-02', ?, 'A通用', 100)""",
        (now,),
    ).lastrowid
    conn.execute(
        """INSERT INTO holding_events
           (holding_id, code, event_type, event_date, shares, price, created_at)
           VALUES (?, '600036', 'buy', '2026-01-02', 100, 11.0, ?)""",
        (second_id, now),
    )
    conn.commit()
    conn.close()

    commands_holdings.cmd_close_holding(["600036", "12.0", "2026-02-01"])

    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, shares, exit_price, exit_date FROM holdings
           WHERE code='600036' ORDER BY buy_date, id"""
    ).fetchall()
    sell_events = conn.execute(
        """SELECT holding_id, shares FROM holding_events
           WHERE code='600036' AND event_type='sell' ORDER BY id"""
    ).fetchall()
    conn.close()
    assert rows[0][1:] == (0, 12.0, "2026-02-01")
    assert rows[1][1:] == (100, None, None)
    assert sell_events == [(rows[0][0], 100)]


def test_retro_uses_full_event_lifecycle_not_last_exit_price():
    commands_holdings.cmd_add_holding(["600036", "10.0", "500", "--date", "2026-01-01"])
    commands_holdings.cmd_sell_holding(
        ["600036", "15.0", "200", "--date", "2026-02-01"]
    )
    commands_holdings.cmd_record_dividend(["600036", "500", "2026-03-01"])
    commands_holdings.cmd_sell_holding(["600036", "8.0", "all", "--date", "2026-04-01"])
    commands_holdings.cmd_retro_add(["600036", "生命周期测试"])
    conn = db.get_db()
    actual_return = conn.execute(
        "SELECT actual_return_pct FROM retro_notes WHERE code='600036'"
    ).fetchone()[0]
    conn.close()
    assert actual_return == pytest.approx(18.0)


def test_remove_holding_cascades_all_lifecycle_children():
    commands_holdings.cmd_add_holding(["600036", "10.0", "100"])
    commands_monitor.cmd_l3_add(["600036", "original", "测试条件"])
    commands_monitor.cmd_alert_open(
        ["600036", "yellow", "unverified", "test", "none", "测试预警"]
    )
    commands_holdings.cmd_remove_holding(["600036"])
    conn = db.get_db()
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "holdings",
            "holding_events",
            "holding_l3_conditions",
            "holding_tier_state",
            "holding_alerts",
            "retro_notes",
        )
    }
    conn.close()
    assert counts == {table: 0 for table in counts}


def test_cleanup_removes_preexisting_orphan_lifecycle_records(capsys):
    """cleanup also governs orphans created before cascade deletion was available."""
    commands_holdings.cmd_add_holding(["600036", "10.0", "100"])
    now = domain.utc_now_iso()
    conn = db.get_db()
    holding_id = conn.execute("SELECT id FROM holdings WHERE code='600036'").fetchone()[
        0
    ]
    conn.execute(
        """INSERT INTO holding_l3_conditions
           (holding_id, condition_text, created_at, updated_at)
           VALUES (?, '旧孤儿条件', ?, ?)""",
        (holding_id, now, now),
    )
    conn.execute(
        """INSERT INTO holding_alerts
           (holding_id, code, level, category, reason_code, reason, opened_at, updated_at)
           VALUES (?, '600036', 'yellow', 'unverified', 'legacy', '旧孤儿预警', ?, ?)""",
        (holding_id, now, now),
    )
    conn.execute(
        """INSERT INTO retro_notes (holding_id, code, error_tags, created_at)
           VALUES (?, '600036', 'legacy', ?)""",
        (holding_id, now),
    )
    conn.execute("DELETE FROM holdings WHERE id=?", (holding_id,))
    conn.commit()
    conn.close()

    commands_admin.cmd_cleanup()
    out = capsys.readouterr().out
    conn = db.get_db()
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "holding_events",
            "holding_l3_conditions",
            "holding_tier_state",
            "holding_alerts",
            "retro_notes",
        )
    }
    conn.close()
    assert "孤儿持仓关联记录" in out
    assert counts == {table: 0 for table in counts}


def test_trade_commands_reject_non_finite_prices_and_backdated_events():
    with pytest.raises(SystemExit):
        commands_holdings.cmd_add_holding(["600036", "nan", "100"])
    commands_holdings.cmd_add_holding(["600036", "10.0", "100", "--date", "2026-02-01"])
    with pytest.raises(SystemExit):
        commands_holdings.cmd_sell_holding(
            ["600036", "12.0", "all", "--date", "2026-01-01"]
        )


def test_bse_920_uses_incremental_share_rule_and_quote_prefix():
    assert market_quotes.sina_query_prefix("920189") == "bj"
    assert commands_holdings._validate_buy_quantity("920189", 101) is None
    assert commands_holdings._validate_sell_quantity("920189", 500, 150) is None
    assert "北交所" in (commands_holdings._validate_buy_quantity("920189", 99) or "")


def test_update_return_uses_holding_buy_date_not_latest_analysis_date(monkeypatch):
    set_valid_analysis(monkeypatch, code="600519", narrative="买入")
    conn = db.get_db()
    conn.execute(
        """INSERT INTO holdings
           (code, cost_price, shares, buy_date, framework)
           VALUES ('600519', 100, 100, '2024-01-01', 'A通用')"""
    )
    conn.commit()
    conn.close()
    commands_holdings.cmd_update_return(["600519", "18.5"])
    conn = db.get_db()
    holding_days = conn.execute(
        "SELECT holding_days FROM analysis_results WHERE code=?",
        ("600519",),
    ).fetchone()[0]
    conn.close()
    assert holding_days > 365


def test_l3_condition_has_structured_status_and_evidence(capsys):
    commands_holdings.cmd_add_holding(["600036", "40.0", "100"])
    commands_monitor.cmd_l3_add(
        [
            "600036",
            "original",
            "净息差低于1.8%",
            "低于1.7%减半仓",
        ]
    )
    conn = db.get_db()
    condition_id = conn.execute("SELECT id FROM holding_l3_conditions").fetchone()[0]
    conn.close()
    commands_monitor.cmd_l3_update(
        [
            str(condition_id),
            "watch",
            "2026-07-30",
            "2026Q2净息差1.82%",
            "2026-10-31",
        ]
    )
    commands_monitor.cmd_l3_list(["600036"])
    out = capsys.readouterr().out
    assert "watch" in out
    assert "2026Q2净息差1.82%" in out
    assert "低于1.7%减半仓" in out


def test_tier_exemption_is_structured_and_entry_day_only():
    conn = db.get_db()
    conn.execute(
        """INSERT INTO analysis_results (code, date, framework)
           VALUES ('000333', ?, 'E消费')""",
        (domain.cst_today(),),
    )
    conn.commit()
    conn.close()
    commands_holdings.cmd_add_holding(["000333", "50", "100"])
    commands_monitor.cmd_tier_config(["000333", "none", "none", "E"])
    conn = db.get_db()
    row = conn.execute(
        """SELECT exemption_framework, exemption_declared_at
           FROM holding_tier_state"""
    ).fetchone()
    conn.close()
    assert row == ("E", domain.cst_today())


def test_tier_exemption_rejects_backdated_entry():
    conn = db.get_db()
    conn.execute(
        """INSERT INTO analysis_results (code, date, framework)
           VALUES ('000333', ?, 'E消费')""",
        (domain.cst_today(),),
    )
    conn.commit()
    conn.close()
    commands_holdings.cmd_add_holding(
        [
            "000333",
            "50",
            "100",
            "--date",
            "2026-07-01",
        ]
    )

    with pytest.raises(SystemExit) as exc:
        commands_monitor.cmd_tier_config(["000333", "none", "none", "E"])

    assert exc.value.code == 1
    conn = db.get_db()
    row = conn.execute(
        """SELECT exemption_framework, exemption_declared_at
           FROM holding_tier_state"""
    ).fetchone()
    conn.close()
    assert row == (None, None)


def test_holding_framework_migration_recomputes_stops_and_records_event():
    commands_holdings.cmd_add_holding(["600036", "40", "100"])
    commands_monitor.cmd_holding_framework(["600036", "B"])
    conn = db.get_db()
    row = conn.execute(
        """SELECT framework, framework_confident, stop_loss_15, stop_loss_20
           FROM holdings WHERE code='600036'"""
    ).fetchone()
    event = conn.execute(
        """SELECT notes FROM holding_events
           WHERE code='600036' AND event_type='adjustment'"""
    ).fetchone()[0]
    conn.close()
    assert row == ("B银行", 1, 35.2, 32.8)
    assert event == "framework:A通用->B银行"


def test_structured_alert_deduplicates_and_resolves():
    commands_holdings.cmd_add_holding(["600036", "40.0", "100"])
    first = [
        "600036",
        "yellow",
        "holding_deterioration",
        "nim_decline",
        "2026-08-31",
        "净息差下降",
        "2026Q2同比下降",
    ]
    commands_monitor.cmd_alert_open(first)
    commands_monitor.cmd_alert_open(first[:-1] + ["更新后的证据"])
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM holding_alerts").fetchone()[0] == 1
    conn.close()
    commands_monitor.cmd_alert_pending(["600036", "nim_decline", "等待下一季数据"])
    commands_monitor.cmd_alert_resolve(["600036", "nim_decline", "净息差恢复"])
    conn = db.get_db()
    row = conn.execute(
        "SELECT status, resolution_evidence FROM holding_alerts"
    ).fetchone()
    conn.close()
    assert row == ("resolved", "净息差恢复")


def test_portfolio_risk_no_holdings(capsys):
    """真实空仓不等于缺失数据库：先建立空 fixture schema。"""
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone() == (0,)
    commands_holdings.cmd_portfolio_risk()
    out = capsys.readouterr().out
    assert "暂无持仓" in out


def test_portfolio_risk_with_holdings(capsys, monkeypatch):
    """有持仓时正常输出持仓明细和框架分布"""
    commands_holdings.cmd_add_holding(["600036", "45.0", "100", "--notes", "测试招行"])
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
    commands_holdings.cmd_add_holding(["600036", "45.0", "100", "--notes", "测试招行"])
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
    commands_holdings.cmd_add_holding(["600036", "45.0", "100", "--notes", "测试招行"])
    commands_holdings.cmd_add_holding(["600900", "25.0", "100", "--notes", "测试长电"])
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
    commands_holdings.cmd_add_holding(["600036", "45.0", "100", "--notes", "测试招行"])
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
    commands_holdings.cmd_add_holding(["600036", "40.0", "100", "--notes", "测试"])
    mock_price_quotes(monkeypatch, lambda codes: {code: (None) for code in codes})
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "无实时价格" in out


def test_check_holdings_normal(capsys, monkeypatch):
    """现价高于止损线时显示✅正常，不计入预警"""
    commands_holdings.cmd_add_holding(
        ["600036", "40.0", "100", "--notes", "测试"]
    )  # 止损15%=34.0 20%=32.0
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
    commands_holdings.cmd_add_holding(
        ["600036", "40.0", "100", "--notes", "测试"]
    )  # 止损15%=34.0 20%=32.0
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
    commands_holdings.cmd_add_holding(
        ["600036", "40.0", "100", "--notes", "测试"]
    )  # 止损15%=34.0 20%=32.0
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
    commands_holdings.cmd_add_holding(["600036", "40.0", "100", "--notes", "测试"])
    commands_holdings.cmd_close_holding(["600036", "50.0"])
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
    commands_holdings.cmd_add_holding(
        ["600036", "40.0", "100", "--notes", "测试"]
    )  # 止损15%=34.0 20%=32.0
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
    commands_holdings.cmd_add_holding(["600036", "40.0", "100", "--notes", "测试"])
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
    commands_holdings.cmd_add_holding(
        ["600036", "40.0", "100", "--notes", "测试"]
    )  # 止损15%=34.0 20%=32.0
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
    commands_holdings.cmd_add_holding(
        ["600036", "40.0", "100", "--notes", "测试"]
    )  # 止损15%=34.0 20%=32.0
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


def test_cmd_checklist_b_framework_output_uses_owner_strict_thresholds(capsys):
    """B框架输出准确展示 owner contract 的严格阈值。"""
    set_valid_fundamentals("601988", "中国银行", "银行", {"roe_3y_avg": 14.0}, ttl=24)

    commands_admin.cmd_checklist(["601988", "B"])

    out = capsys.readouterr().out
    assert out == (
        "框架客观指标核对清单：银行框架 601988\n"
        "────────────────────────────────\n"
        "ROE加权年化: 14.0% | 优线>13% 格线>9% | 结果:达优 | 数据:完整\n"
        "\n"
        "需人工主观判断（不参与代码核对）：护城河、行业地位\n"
        "\n"
        "checklist工具无法核验（仍需按框架文档人工评分，权重不变）：\n"
        "- 净息差趋势：数据缺口：该字段无AKShare API，需人工检索公开来源后手动写入缓存"
        "（fetcher.py FIELDS注册表标注来源为web），核验对象与核验来源同源，不构成独立校验；"
        "权重不变，仍需结合公开来源人工评分\n"
        "- 不良贷款率：数据缺口：该字段无AKShare API，需人工检索公开来源后手动写入缓存"
        "（fetcher.py FIELDS注册表标注来源为web），核验对象与核验来源同源，不构成独立校验；"
        "权重不变，仍需结合公开来源人工评分\n"
        "- 拨备覆盖率：数据缺口：该字段无AKShare API，需人工检索公开来源后手动写入缓存"
        "（fetcher.py FIELDS注册表标注来源为web），核验对象与核验来源同源，不构成独立校验；"
        "权重不变，仍需结合公开来源人工评分\n"
    )


def test_cmd_checklist_a_framework_has_no_skipped_section(capsys):
    """A框架没有跳过项，输出里不应该出现"无法核验"小节（验证空list跟原来的
    None在format_checklist()的falsy判断下行为一致）。"""
    set_valid_fundamentals(
        "600036",
        "招商银行",
        "银行",
        {
            "roe_3y_avg": 18.2,
            "net_profit_growth": 16.0,
            "debt_ratio": 30.0,
            "gross_margin": 35.0,
        },
        ttl=24,
    )

    commands_admin.cmd_checklist(["600036", "A"])

    out = capsys.readouterr().out
    assert "checklist工具无法核验" not in out
    assert "框架客观指标核对清单：A通用框架 600036" in out


def test_cmd_checklist_ignores_cycle_stage_wording_in_narrative(capsys, monkeypatch):
    set_valid_fundamentals(
        "600900",
        "长江电力",
        "水电",
        {
            "eps": 1.5,
            "debt_ratio": 50.0,
        },
        ttl=24,
    )
    set_valid_analysis(
        monkeypatch,
        code="600900",
        framework="C资源",
        score=55,
        cycle=False,
        narrative=LEGACY_CYCLE_STAGE_TAG,
    )

    commands_admin.cmd_checklist(["600900", "C"])

    out = capsys.readouterr().out
    assert "框架客观指标核对清单：能源/资源框架 600900" in out
    assert "提前提示：C资源框架写入分析时必须包含有效的周期位置标签" not in out


def test_cmd_checklist_c_framework_skips_warning_when_cycle_stage_present(
    capsys, monkeypatch
):
    set_valid_fundamentals(
        "600900",
        "长江电力",
        "水电",
        {
            "eps": 1.5,
            "debt_ratio": 50.0,
        },
        ttl=24,
    )
    set_valid_analysis(
        monkeypatch, code="600900", framework="C资源", score=55, cycle=True
    )

    commands_admin.cmd_checklist(["600900", "c"])

    out = capsys.readouterr().out
    assert "框架客观指标核对清单：能源/资源框架 600900" in out
    assert "提前提示：C资源框架写入分析时必须包含有效的周期位置标签" not in out


@pytest.mark.parametrize(
    "framework,code,data",
    [
        ("B", "601988", {"roe_3y_avg": 14.0}),
        ("D", "600025", {"debt_ratio": 50.0}),
    ],
)
def test_cmd_checklist_bd_do_not_show_c_cycle_stage_warning(
    framework, code, data, capsys
):
    set_valid_fundamentals(code, code, "测试行业", data, ttl=24)

    commands_admin.cmd_checklist([code, framework])

    out = capsys.readouterr().out
    assert "提前提示：C资源框架写入分析时必须包含有效的周期位置标签" not in out


def test_cmd_checklist_unsupported_framework_exits_nonzero(capsys):
    set_valid_fundamentals("600036", "招商银行", "银行", {"roe_3y_avg": 18.2}, ttl=24)

    with pytest.raises(SystemExit) as exc_info:
        commands_admin.cmd_checklist(["600036", "Z"])

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "暂不支持框架 'Z' 的 checklist" in err


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
