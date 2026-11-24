"""Post-close push brief built from the monitor-v1 snapshot."""

import json

import pytest

from a_share_research.policy.v0 import cache, commands_holdings, commands_monitor, paths
from a_share_research.policy.v0.commands_monitor import format_daily_brief


# Fixtures carry every key build_monitor_snapshot emits, so the brief needs no fallbacks.
def _holding(code, price, quote_date="2026-10-22", l3=None):
    return {
        "code": code,
        "name": f"fixture-{code}",
        "quote": {
            "price": price,
            "quote_date": quote_date,
            "daily_change_pct": -1.43,
            "relative_change_pct": None,
        },
        "stop_loss_15": 24.824,
        "stop_loss_20": 23.364,
        "stop_risk_pct": None,
        "portfolio_weight_pct": None,
        "p3_gate": {"status": "not_applicable", "reason_code": "stop_loss_20_not_triggered"},
        "alerts": [],
        "l3": {"contract": "versioned", "conditions": l3 or []},
    }


def _account(**overrides):
    return {
        "portfolio_value": None,
        "stock_weight_pct": None,
        "total_stop_risk_pct": None,
        "known_stop_risk_lower_bound": None,
        "risk_policy": {"max_position_risk_pct": 6, "max_portfolio_risk_pct": 18},
        "portfolio_value_as_of": None,
        "denominator_scope_status": "provided",
        **overrides,
    }


def _snapshot(holdings, escalations=(), review_status="cleared"):
    return {
        "as_of": "2026-10-22T15:30:05+08:00",
        "review_status": review_status,
        "valuation_status": "exact",
        "account": _account(),
        "holdings": holdings,
        "escalations": list(escalations),
        "data_gaps": [],
    }


def test_non_trading_day_or_missing_quotes_is_silent():
    stale = _snapshot([_holding("000963", 26.21, quote_date="2026-10-21")])
    assert format_daily_brief(stale, [], "2026-10-22") == ""
    assert (
        format_daily_brief(_snapshot([_holding("000963", None)]), [], "2026-10-22")
        == ""
    )


def test_brief_lists_stop_distance_triggers_due_l3_and_extras():
    l3 = [
        {"id": 35, "status": "watch", "next_review": "2026-10-31"},
        {"id": 34, "status": "not_triggered", "next_review": "2026-10-28"},
        {"id": 36, "status": "not_triggered", "next_review": "2027-04-30"},
    ]
    holding = _holding("000963", 26.21, l3=l3)
    holding["quote"]["daily_change_pct"] = -3.10
    snapshot = _snapshot(
        [holding],
        [
            {
                "code": "000963",
                "reason_code": "daily_drop",
                "detail": "daily change -3.1%",
            },
            {"code": None, "reason_code": "denominator_missing", "detail": "x"},
        ],
    )
    brief = format_daily_brief(snapshot, ["000963 疑似漏记分红"], "2026-10-22")
    assert "fixture-000963 26.21 -3.10% ｜距止损 5.3%" in brief
    # One line per holding, Chinese phrases only; the English detail stays in the snapshot.
    assert (
        "今日需处理 2 项：\n- fixture-000963：异常跌幅 -3.10%，待查归因；"
        "L3 1 条观察中，冻结新增风险，2026-10-31 复核（#35）；"
        "L3 1 条未触发，2026-10-28 例行复核（#34）\n- 000963 疑似漏记分红\n"
    ) in brief
    assert "#36" not in brief and "denominator" not in brief and "daily change" not in brief


def test_stop_breach_merges_signals_into_one_action_line_and_sorts_holdings():
    breached = _holding("000963", 24.35)
    breached["quote"]["daily_change_pct"] = -3.41
    snapshot = _snapshot(
        [_holding("600900", 26.21), breached],
        [
            {"code": "000963", "reason_code": "price_stop_1", "detail": "current price reached"},
            {"code": "000963", "reason_code": "daily_drop", "detail": "daily change -3.41%"},
        ],
    )
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert (
        "今日需处理 1 项：\n- fixture-000963：跌破第一档止损（低于止损线 1.9%），"
        "距第二档 4.0%；异常跌幅 -3.41%，待查归因\n"
    ) in brief
    assert "留意" not in brief
    assert brief.index("fixture-000963 24.35 -3.41% ｜已破第一档") < brief.index(
        "fixture-600900 26.21 -1.43% ｜距止损 5.3%"
    )


def test_calm_day_limits_no_trigger_to_program_checks():
    brief = format_daily_brief(_snapshot([_holding("000963", 26.21)]), [], "2026-10-22")
    assert "今日无需处理：程序检查" in brief
    assert "不覆盖未自动计算的基本面条件" in brief
    assert "留意" not in brief and "预警" not in brief.split("均未触发")[1]


def test_unresolved_alerts_stay_listed_before_their_review_date():
    holding = _holding(
        "000963",
        26.21,
        l3=[
            {
                "id": 9,
                "status": "not_triggered",
                "next_review": None,
                "as_of": "2026-08-30",
                "evidence_cadence": "semiannual",
            }
        ],
    )
    holding["alerts"] = [
        {
            "level": "yellow",
            "reason_code": "valuation_high",
            "reason": "估值偏高",
            "status": "active",
            "opened_at": "2026-10-01T15:00:00+08:00",
            "review_due": "2026-12-31",
            "review_due_now": False,
        },
        {
            "level": "red",
            "reason_code": "fund_occupation",
            "reason": "控股股东资金占用待核实",
            "status": "pending",
            "opened_at": "2026-10-20T15:00:00+08:00",
            "review_due": "2026-11-30",
            "review_due_now": False,
        },
    ]
    brief = format_daily_brief(_snapshot([holding]), [], "2026-10-22")
    assert (
        "今日无即时触发项，但仍有待核实或未完成核查项；不覆盖未自动计算的基本面条件\n留意：\n"
        "- fixture-000963：红色预警（待核实，复核日 2026-11-30）：控股股东资金占用待核实；"
        "L3 1 条未触发，未设复核日（#9）\n"
    ) in brief
    assert brief.endswith("黄色预警 1 条持续中（fixture-000963 1），最近复核 2026-12-31")
    assert "估值偏高" not in brief
    assert "valuation_high" not in brief and "fund_occupation" not in brief
    holding["alerts"][1].update(review_due_now=True, review_due="2026-10-22", status="active")
    due = format_daily_brief(_snapshot([holding]), [], "2026-10-22")
    assert (
        "今日需处理 1 项：\n- fixture-000963：红色预警到期复核（生效中，复核日 2026-10-22）："
        "控股股东资金占用待核实；L3 1 条未触发，未设复核日（#9）\n"
    ) in due
    assert due.count("控股股东资金占用待核实") == 1


def test_brief_uses_saved_denominator_without_repeated_screenshot_requests():
    snapshot = _snapshot([_holding("000963", 26.21)])
    snapshot["valuation_status"] = "exact"
    snapshot["holdings"][0]["portfolio_weight_pct"] = 25
    snapshot["account"] = _account(
        portfolio_value=100000,
        stock_weight_pct=75,
        total_stop_risk_pct=12.5,
        portfolio_value_source="account_snapshot",
        portfolio_value_recorded_at="2026-10-22T15:40:00+08:00",
    )
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert "仓位25.0%" in brief
    assert (
        "组合：股票仓位 75.0%（总资产 100,000 元，10-22 记录，估值时点待核）；"
        "第二档止损风险 12.5% / 预算 18%（余 5.5 点）"
    ) in brief
    assert "发资金截图" not in brief and "需当日账户总资产" not in brief
    snapshot["valuation_status"] = "priced_positions_lower_bound"
    snapshot["account"]["total_stop_risk_pct"] = None
    partial = format_daily_brief(snapshot, [], "2026-10-22")
    assert "已取价股票仓位（下限）" in partial and "风险不完整" in partial


def test_daily_brief_command_passes_saved_assets_through_shared_resolver(
    monkeypatch, capsys
):
    path = paths.cache_db_path().with_name("account-snapshot.json")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "CNY",
                "account_scope": "fixture-account",
                "portfolio_value": 100000,
                "portfolio_value_as_of": None,
                "recorded_at": "2026-10-22T15:40:00+08:00",
                "source": "user_screenshot",
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setattr(
        commands_monitor, "_load_monitor_local_snapshot", lambda: {"holdings": []}
    )
    monkeypatch.setattr(
        commands_holdings, "fetch_monitor_price_quotes", lambda _: ({}, None)
    )
    monkeypatch.setattr(
        commands_holdings, "scan_unrecorded_dividends", lambda: ([], [], [])
    )
    seen = []

    def build(local, quotes, portfolio_value, **kwargs):
        seen.append((portfolio_value, kwargs))
        return _snapshot([_holding("000963", 26.21)])

    monkeypatch.setattr(commands_monitor, "build_monitor_snapshot", build)
    monkeypatch.setattr(commands_monitor.domain, "cst_today", lambda: "2026-10-22")
    assert cache.main(["daily-brief"]) == 0
    assert seen[0][0] == 100000
    assert (
        seen[0][1]["denominator_evidence"]["portfolio_value_source"]
        == "account_snapshot"
    )
    assert seen[0][1]["denominator_evidence"]["account_scope"] == "fixture-account"
    assert "发资金截图" not in capsys.readouterr().out


def test_routine_l3_groups_by_date_without_hiding_due_or_unverified_items():
    snapshot = _snapshot(
        [
            _holding(
                "000963",
                26.21,
                l3=[
                    {"id": 1, "status": "not_triggered", "next_review": "2026-10-29"},
                    {"id": 2, "status": "not_triggered", "next_review": "2026-10-29"},
                    {"id": 3, "status": "not_triggered", "next_review": "2026-10-22"},
                    {"id": 4, "status": "not_triggered", "next_review": "2026-10-21"},
                    {"id": 5, "status": "pending", "next_review": None},
                ],
            ),
            _holding(
                "600900",
                29.83,
                l3=[
                    {"id": 6, "status": "not_triggered", "next_review": "2026-10-29"},
                    {"id": 7, "status": "triggered", "next_review": "2026-10-30", "valid_for_action": True},
                    {"id": 8, "status": "not_triggered", "next_review": "2026-10-30"},
                ],
            ),
        ]
    )
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert "今日需处理 2 项：" in brief
    assert (
        "- fixture-000963：L3 2 条到期复核（#3、#4）；"
        "L3 2 条未触发，2026-10-29 例行复核（#1、#2）；L3 1 条待核实，未设复核日（#5）"
    ) in brief
    assert (
        "- fixture-600900：L3 1 条已触发（#7）；L3 1 条未触发，2026-10-29 例行复核（#6）\n"
    ) in brief
    # Routine reviews beyond the 7-day horizon stay out of the brief.
    assert "#8" not in brief
    snapshot["holdings"][1]["l3"]["contract"] = "invalid"
    invalid = format_daily_brief(snapshot, [], "2026-10-22")
    assert "- fixture-600900：L3 1 条已登记触发，证据或契约待核实（#7）；" in invalid


def test_industry_trigger_and_evidence_gaps_use_snapshot_fields():
    holding = _holding("000963", 26.21)
    holding["quote"].update(daily_change_pct=0.08, relative_change_pct=-2.1)
    missing = _holding("600900", 25.5)
    missing["quote"]["daily_change_pct"] = None
    snapshot = _snapshot(
        [holding, missing],
        [{"code": "000963", "reason_code": "relative_underperformance", "detail": "not rendered"}],
        review_status="review_required",
    )
    snapshot["data_gaps"] = [
        {"code": "600900", "field": "previous_close"},
        {"code": "600900", "field": "industry_change_pct"},
    ]
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert (
        "今日需处理 1 项：\n- fixture-000963：跑输行业 2.1 个点，待查归因\n"
        "留意：\n- 核查缺口：1 只缺昨收；1 只缺同日行业涨跌幅\n"
    ) in brief
    snapshot["escalations"] = []
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert (
        "今日无即时触发项，但仍有待核实或未完成核查项；缺失不视为未触发，"
        "不覆盖未自动计算的基本面条件\n"
    ) in brief


@pytest.mark.parametrize(
    ("tier", "gate", "expected"),
    [
        (1, None, "触及第一档止损"),
        (
            2,
            {"status": "incomplete", "reason_code": "market_snapshot_incomplete"},
            "触及第二档止损；P3 市场快照不全，暂缓",
        ),
        (
            2,
            {"status": "deferred", "reason_code": "price_stop2_liquidity_defer",
             "review_due": "2026-10-23T15:30:05+08:00"},
            "触及第二档止损；P3 暂缓（下次复核 2026-10-23 15:30）",
        ),
        (2, {"status": "untradeable", "reason_code": "quote_untradeable"}, "触及第二档止损；P3 不可交易，暂缓"),
        (2, {"status": "clear", "action_eligible": True}, "触及第二档止损；P3 已通过"),
        (2, {"status": "clear", "action_eligible": False}, "触及第二档止损；P3 复核已回升，暂不执行"),
    ],
)
def test_at_stop_line_and_p3_status(tier, gate, expected):
    holding = _holding("000963", None)
    holding["quote"]["price"] = holding["stop_loss_15" if tier == 1 else "stop_loss_20"]
    if gate:
        holding["p3_gate"] = gate
    snapshot = _snapshot([holding], [{"code": "000963", "reason_code": f"price_stop_{tier}"}])
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert f"- fixture-000963：{expected}" in brief.splitlines()[2]


def test_risk_breaches_keep_ratio_limit_and_partial_portfolio_lower_bound():
    holding = _holding("000963", 26.21)
    holding["stop_risk_pct"] = 7.11
    snapshot = _snapshot(
        [holding], [{"code": "000963", "reason_code": "risk_budget_exceeded"}], review_status="review_required"
    )
    snapshot["account"] = _account(portfolio_value=100_000, total_stop_risk_pct=15.62)
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert "- fixture-000963：单股止损风险 7.11% / 6%（超限）\n" in brief
    assert "第二档止损风险 15.6% / 预算 18%（余 2.4 点）" in brief

    snapshot["account"].update(total_stop_risk_pct=None, known_stop_risk_lower_bound=23_800)
    snapshot["escalations"].append({"code": None, "reason_code": "risk_budget_exceeded"})
    partial = format_daily_brief(snapshot, [], "2026-10-22")
    assert "- 组合：组合止损风险下限 23.80% / 18%（超限）" in partial
    assert "第二档止损风险不完整（下限 23.80% / 预算 18%）" in partial

    # Scope unknown: build_monitor_snapshot raises no breach and the lower bound is not a ratio.
    snapshot["escalations"] = [{"code": None, "reason_code": "denominator_missing"}]
    snapshot["data_gaps"] = [{"code": None, "field": "account_scope"}]
    snapshot["account"]["denominator_scope_status"] = "unknown"
    blocked = format_daily_brief(snapshot, [], "2026-10-22")
    assert "总资产 100,000 元，估值时点待核，账户范围待核；账户范围待核，风险预算暂不可算" in blocked
    assert "23.80%" not in blocked and "均未触发" not in blocked
