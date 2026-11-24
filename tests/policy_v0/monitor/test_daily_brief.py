"""Post-close push brief built from the monitor-v1 snapshot."""

import json

from a_share_research.policy.v0 import cache, commands_holdings, commands_monitor, paths
from a_share_research.policy.v0.commands_monitor import format_daily_brief


def _holding(code, price, quote_date="2026-10-22", l3=None):
    return {
        "code": code,
        "name": f"fixture-{code}",
        "quote": {"price": price, "quote_date": quote_date, "daily_change_pct": -1.43},
        "stop_loss_15": 24.824,
        "stop_loss_20": 23.364,
        "l3": {"conditions": l3 or []},
    }


def _snapshot(holdings, escalations=()):
    return {
        "as_of": "2026-10-22T15:30:05+08:00",
        "holdings": holdings,
        "escalations": list(escalations),
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
    snapshot = _snapshot(
        [_holding("000963", 26.21, l3=l3)],
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
    assert "fixture-000963 26.21 -1.43% ｜距止损 5.3%" in brief
    # One line per holding, Chinese phrases only; the English detail stays in the snapshot.
    assert (
        "- fixture-000963：今日 -1.43%；L3 1 条观察中，2026-10-31 复核（#35）；"
        "L3 1 条未触发，2026-10-28 例行复核（#34）"
    ) in brief
    assert "#36" not in brief and "denominator" not in brief and "daily change" not in brief
    assert "今日需处理 1 项：\n- 000963 疑似漏记分红" in brief


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
        "距第二档 4.0%；今日 -3.41%"
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
    red = "红色预警 fixture-000963：控股股东资金占用待核实（复核日 2026-11-30）"
    yellow = "黄色预警 1 条持续中（fixture-000963 1），今日无到期，最近复核 2026-12-31"
    assert brief.index(red) < brief.index(yellow)
    assert "valuation_high" not in brief and "fund_occupation" not in brief
    # A condition without a review date stays visible.
    assert "L3 1 条未触发，未设复核日（#9）" in brief


def test_brief_uses_saved_denominator_without_repeated_screenshot_requests():
    snapshot = _snapshot([_holding("000963", 26.21)])
    snapshot["valuation_status"] = "exact"
    snapshot["holdings"][0]["portfolio_weight_pct"] = 25
    snapshot["account"] = {
        "portfolio_value": 100000,
        "stock_weight_pct": 75,
        "total_stop_risk_pct": 12.5,
        "risk_policy": {"max_position_risk_pct": 6, "max_portfolio_risk_pct": 18},
        "portfolio_value_source": "account_snapshot",
        "portfolio_value_recorded_at": "2026-10-22T15:40:00+08:00",
        "portfolio_value_as_of": None,
    }
    brief = format_daily_brief(snapshot, [], "2026-10-22")
    assert "仓位25.0%" in brief
    assert (
        "组合：股票仓位 75.0%（总资产 100,000 元，10-22 记录）；"
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
                    {"id": 7, "status": "triggered", "next_review": "2026-10-30"},
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
