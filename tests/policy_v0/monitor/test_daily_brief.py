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
    assert "fixture-000963 000963 26.21（-1.43%） 距止损1+5.3% 距止损2+10.9%" in brief
    assert "- 000963 日跌幅达3%：daily change -3.1%" in brief
    assert "L3#35 观察中" in brief
    assert "2026-10-28 有1项L3例行复核，当前均未触发（000963）" in brief
    assert "L3#36" not in brief and "denominator" not in brief
    assert "- 000963 疑似漏记分红" in brief


def test_calm_day_limits_no_trigger_to_program_checks():
    brief = format_daily_brief(_snapshot([_holding("000963", 26.21)]), [], "2026-10-22")
    assert "需关注：本轮程序检查未触发" in brief
    assert "不覆盖未自动计算的基本面条件" in brief
    assert "无触发项" not in brief and "尚未关闭" not in brief


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
    red = "000963 红色预警 fund_occupation：控股股东资金占用待核实（pending，开立 2026-10-20，复核日 2026-11-30）"
    yellow = "000963 黄色预警 valuation_high"
    assert brief.index("尚未关闭：") < brief.index(red) < brief.index(yellow)
    # A condition without a review date is surfaced with its evidence date and cadence.
    assert "L3#9 未触发（证据日期 2026-08-30，证据频率 半年），下次复核 未设" in brief


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
    assert "仓位25.00%" in brief
    assert "股票仓位 75.00%（总资产 100,000.00 元）" in brief
    assert "组合第二档止损风险 12.50% / 预算18%；单股预算6%" in brief
    assert "资产基线：系统保存" in brief and "资产估值时点未标注" in brief
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
    assert "2026-10-29 有3项L3例行复核，当前均未触发（000963、600900）" in brief
    for condition_id in (3, 4, 5, 7):
        assert f"L3#{condition_id}" in brief
    for condition_id in (1, 2, 6, 8):
        assert f"L3#{condition_id} " not in brief
    assert "L3#5 待核实（证据日期 未记录，证据频率 未分类，季报不能改变状态），下次复核 未设" in brief
    assert "L3#7 已触发" in brief
