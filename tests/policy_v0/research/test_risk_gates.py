from datetime import date, datetime, timedelta, timezone

import pytest

from a_share_research.policy.v0 import risk_gates, store

CST = timezone(timedelta(hours=8))


def _p0(**overrides):
    value = {
        "exchange": "SSE",
        "board": "main",
        "as_of": "2026-09-07T10:00:00+08:00",
        "report_period": "2025年报",
        "sources": ["SSE official rule and company annual report"],
        "listing_status": "clear",
        "financial_opinion": "unqualified",
        "internal_control_opinion": "unqualified",
        "investigation_status": "none",
        "latest_annual_attributable_profit": 100_000_000,
        "parent_unallocated_profit_at_year_end": 100_000_000,
        "three_year_annual_profits": [100_000_000] * 3,
        "three_year_cash_dividend": 40_000_000,
    }
    value.update(overrides)
    return value


def test_regulatory_gate_requires_both_main_board_dividend_thresholds():
    assert (
        risk_gates.regulatory_gate(_p0(three_year_cash_dividend=20_000_000))["status"]
        == "blocked"
    )
    assert (
        risk_gates.regulatory_gate(_p0(three_year_cash_dividend=30_000_000))["status"]
        == "clear"
    )
    assert (
        risk_gates.regulatory_gate(_p0(three_year_cash_dividend=50_000_000))["status"]
        == "clear"
    )


@pytest.mark.parametrize("exchange,board", [("SSE", "star"), ("SZSE", "chinext")])
def test_innovation_boards_use_30m_dividend_amount_threshold(exchange, board):
    common = {
        "exchange": exchange,
        "board": board,
        "three_year_annual_profits": [200_000_000] * 3,
    }
    if exchange == "SZSE":
        common["consolidated_unallocated_profit_at_year_end"] = 100_000_000
    assert (
        risk_gates.regulatory_gate(_p0(**common, three_year_cash_dividend=30_000_000))[
            "status"
        ]
        == "clear"
    )
    assert (
        risk_gates.regulatory_gate(_p0(**common, three_year_cash_dividend=20_000_000))[
            "status"
        ]
        == "blocked"
    )


def test_main_board_40m_dividend_is_blocked_when_ratio_is_below_30_percent():
    gate = risk_gates.regulatory_gate(
        _p0(
            three_year_annual_profits=[200_000_000] * 3,
            three_year_cash_dividend=40_000_000,
        )
    )
    assert gate["status"] == "blocked"


def test_bse_dividend_rule_is_officially_not_applicable():
    gate = risk_gates.regulatory_gate(_p0(exchange="BSE", board="beijing"))
    assert gate["checks"]["dividend_compliance"]["status"] == "not_applicable"
    assert (
        gate["checks"]["dividend_compliance"]["reason_code"]
        == "dividend_rule_not_applicable_bse"
    )
    assert gate["status"] == "clear"
    assert gate["action_eligible"] is True


@pytest.mark.parametrize("status", ["st", "*st", "delisting", "terminated"])
def test_listing_risk_states_are_not_collapsed_into_one_reason(status):
    gate = risk_gates.regulatory_gate(_p0(listing_status=status))
    assert gate["status"] == "blocked"
    assert gate["checks"]["listing_risk"]["status"] == "blocked"
    assert (
        gate["checks"]["listing_risk"]["reason_code"] != "listing_terminated"
        or status == "terminated"
    )


def test_non_positive_main_board_precondition_is_not_applicable():
    gate = risk_gates.regulatory_gate(_p0(latest_annual_attributable_profit=0))
    assert gate["checks"]["dividend_compliance"]["status"] == "not_applicable"
    assert gate["status"] == "clear"


def test_szse_consolidated_unallocated_profit_precondition():
    common = {"exchange": "SZSE", "board": "main"}
    gate = risk_gates.regulatory_gate(
        _p0(
            **common,
            consolidated_unallocated_profit_at_year_end=0,
        )
    )
    assert gate["checks"]["dividend_compliance"]["status"] == "not_applicable"
    assert (
        gate["checks"]["dividend_compliance"]["reason_code"]
        == "dividend_redline_precondition_not_met"
    )
    gate = risk_gates.regulatory_gate(_p0(**common))
    assert gate["checks"]["dividend_compliance"]["status"] == "incomplete"
    assert (
        gate["checks"]["dividend_compliance"]["reason_code"]
        == "dividend_preconditions_missing"
    )


def test_regulatory_missing_or_unofficial_evidence_fails_closed():
    assert (
        risk_gates.regulatory_gate(_p0(sources=["search result"]))["status"]
        == "incomplete"
    )
    assert risk_gates.regulatory_gate(_p0(as_of=None))["status"] == "incomplete"
    assert (
        risk_gates.regulatory_gate(
            _p0(audit_opinion={"status": "clear", "source": "search"})
        )["status"]
        == "incomplete"
    )


def test_statement_mirror_cannot_clear_regulatory_gate() -> None:
    source = "tushare.income (company-filed statement mirror)"
    gate = risk_gates.regulatory_gate(_p0(sources=[source]))

    assert gate["status"] == "incomplete"
    assert gate["action_eligible"] is False
    assert (
        store.validate_gate_payload(
            "regulatory_gate",
            {
                **gate,
                "status": "clear",
                "action_eligible": True,
                "reason_code": "regulatory_clear",
            },
        )
        == "regulatory_gate.sources 非官方"
    )

    official_gate = risk_gates.regulatory_gate(_p0())
    media_gate = {**official_gate, "sources": ["东方财富新闻 提及年报"]}
    assert (
        store.validate_gate_payload("regulatory_gate", media_gate)
        == "regulatory_gate.sources 非官方"
    )
    official_gate["checks"]["listing_risk"] = {
        "status": "blocked",
        "action_eligible": False,
        "reason_code": "listing_risk_st",
    }
    assert (
        store.validate_gate_payload("regulatory_gate", official_gate)
        == "regulatory_gate.status 与 checks 子项冲突"
    )


def test_regulatory_aggregate_prioritizes_blocked_over_incomplete():
    gate = risk_gates.regulatory_gate(_p0(listing_status="st", financial_opinion=""))
    assert gate["status"] == "blocked"
    assert gate["action_eligible"] is False


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 13), "current"),
        (date(2026, 12, 13), "current"),
        (date(2026, 12, 14), "review_due"),
    ],
)
def test_regulatory_rule_freshness_dates(today, expected):
    rule = risk_gates.REGULATORY_RULES[("SSE", "main")]

    assert risk_gates.evaluate_rule_freshness(rule, today) == expected


@pytest.mark.parametrize(
    "mutation",
    [
        {"review_after": None},
        {"review_after": "invalid"},
        {"verified_at": "2027-01-01"},
        {"source_hash": None},
        {"source_hash": "0" * 64},
    ],
)
def test_regulatory_rule_freshness_rejects_invalid_metadata(mutation):
    rule = {**risk_gates.REGULATORY_RULES[("SSE", "main")], **mutation}

    assert (
        risk_gates.evaluate_rule_freshness(rule, date(2026, 9, 13))
        == "invalid_metadata"
    )


def test_expired_regulatory_rule_cannot_clear_gate():
    gate = risk_gates.regulatory_gate(_p0(), today=date(2026, 12, 14))

    assert gate["status"] == "incomplete"
    assert gate["action_eligible"] is False
    assert gate["rule_freshness"] == "review_due"
    assert gate["verified_at"] == "2026-09-13"
    assert gate["source_hash"]


def test_all_regulatory_rules_have_valid_freshness_metadata():
    for rule in risk_gates.REGULATORY_RULES.values():
        assert risk_gates.evaluate_rule_freshness(rule, date(2026, 9, 13)) == "current"
        assert risk_gates.evaluate_rule_freshness(rule, date(2026, 12, 14)) == "review_due"


def test_roe_negative_wins_even_when_two_negative_values_would_make_ratio_positive():
    gate = risk_gates.roe_structural_gate(
        latest_roe_ttm=-10,
        roe_5y_values=[-30, -20, -10, -10, -10],
        sources=["company annual report official"],
        as_of="2026-09-07",
    )
    assert gate["status"] == "blocked"
    assert gate["reason_code"] == "latest_roe_negative"


def test_roe_retention_boundary_is_strict_and_unrounded():
    common = {
        "roe_5y_values": [16] * 5,
        "sources": ["official annual report"],
        "as_of": "2026-09-07",
    }
    assert (
        risk_gates.roe_structural_gate({**common, "latest_roe_ttm": 12})["status"]
        == "clear"
    )
    assert (
        risk_gates.roe_structural_gate({**common, "latest_roe_ttm": 11.999999})[
            "status"
        ]
        == "blocked"
    )
    assert (
        risk_gates.roe_structural_gate(
            {**common, "latest_roe_ttm": 12, "roe_5y_mean": -1}
        )["status"]
        == "incomplete"
    )


def test_roe_ttm_uses_annual_plus_ytd_minus_prior_ytd():
    gate = risk_gates.roe_structural_gate(
        annual_net_profit=100,
        current_ytd_net_profit=40,
        prior_ytd_net_profit=20,
        average_equity=100,
        roe_5y_values=[16] * 5,
        sources=["official annual report"],
        as_of="2026-09-07",
    )
    assert gate["latest_roe_ttm"] == pytest.approx(120)


def test_cash_flow_gate_uses_true_fcf_and_strict_capex_boundary():
    base = {
        "framework": "A通用",
        "sources": ["official annual report"],
        "as_of": "2026-09-07",
    }
    assert (
        risk_gates.cash_flow_gate({**base, "ttm_cfo": 100, "ttm_capex": 40})["fcf"]
        == 60
    )
    assert (
        risk_gates.cash_flow_gate({**base, "ttm_cfo": 100, "ttm_capex": 100})["status"]
        == "clear"
    )
    assert (
        risk_gates.cash_flow_gate({**base, "ttm_cfo": 100, "ttm_capex": 100.01})[
            "status"
        ]
        == "blocked"
    )
    assert (
        risk_gates.cash_flow_gate({**base, "ttm_cfo": 0, "ttm_capex": 1})["reason_code"]
        == "cfo_non_positive"
    )


def test_quantitative_gates_accept_statement_mirror() -> None:
    source = "tushare.fina_indicator (company-filed statement mirror)"
    gate = risk_gates.roe_structural_gate(
        {
            "latest_roe_ttm": 12,
            "roe_5y_values": [12] * 5,
            "sources": [source],
            "as_of": "2026-08-25",
        }
    )

    assert gate["status"] == "clear"


def test_cash_flow_accepts_statement_mirror_and_keeps_quote_time_separate() -> None:
    gate = risk_gates.cash_flow_gate(
        {
            "framework": "A通用",
            "ttm_cfo": 100,
            "ttm_capex": 40,
            "total_market_cap": 1_000,
            "sources": ["tushare.cashflow (company-filed statement mirror)"],
            "as_of": "2026-08-25",
            "quote_as_of": "2026-09-09T10:00:00",
        }
    )

    assert gate["status"] == "clear"
    assert gate["fcf_yield"] == pytest.approx(6.0)


def test_cash_flow_requires_framework_before_capex_exception_decision() -> None:
    gate = risk_gates.cash_flow_gate(
        {
            "ttm_cfo": 100,
            "ttm_capex": 101,
            "sources": ["official annual report"],
            "as_of": "2026-09-07",
        }
    )

    assert gate["status"] == "incomplete"
    assert gate["reason_code"] == "framework_required_for_capex_review"


def test_cash_flow_financial_is_not_applicable_and_cd_starts_review():
    assert (
        risk_gates.cash_flow_gate(
            {"framework": "B银行", "ttm_cfo": -1, "ttm_capex": 10}
        )["status"]
        == "not_applicable"
    )
    review = risk_gates.cash_flow_gate(
        {
            "framework": "C资源",
            "ttm_cfo": 100,
            "ttm_capex": 101,
            "sources": ["official annual report"],
            "as_of": "2026-09-07",
        }
    )
    assert review["status"] == "review_required"


def test_cash_flow_cd_review_requires_coverage_and_official_facts():
    payload = {
        "framework": "D公用",
        "ttm_cfo": 100,
        "ttm_capex": 101,
        "sources": ["official annual report"],
        "as_of": "2026-09-07",
        "cd_capex_review": {
            "cycle_stage": "construction",
            "capex_type": "construction",
            "dividend_stress_test": "pass",
            "cash_dividend_coverage": 1.2,
            "sources": ["company official announcement"],
        },
    }
    assert risk_gates.cash_flow_gate(payload)["status"] == "clear"
    payload["cd_capex_review"]["cash_dividend_coverage"] = 0.9
    assert risk_gates.cash_flow_gate(payload)["status"] == "blocked"


def test_cash_flow_a_review_requires_roe_and_capex_thresholds():
    payload = {
        "framework": "A通用",
        "ttm_cfo": 100,
        "ttm_capex": 113,
        "sources": ["tushare.cashflow (company-filed statement mirror)"],
        "as_of": "2026-08-25",
        "a_capex_review": {
            "capex_type": "扩产",
            "roe_ttm": 21.15,
            "expansion_rationale": "自动化产线扩产与垂直整合",
            "sources": ["立讯精密2025年报与交易所公告"],
        },
    }
    result = risk_gates.cash_flow_gate(payload)
    assert result["status"] == "clear"
    assert result["action_eligible"] is True
    assert result["reason_code"] == "a_capex_review_clear"
    assert result["capex_redline"] == "reviewed_expansion"

    # ROE below 15% threshold fails closed to blocked
    payload["a_capex_review"]["roe_ttm"] = 14.5
    assert risk_gates.cash_flow_gate(payload)["status"] == "blocked"
    assert (
        risk_gates.cash_flow_gate(payload)["reason_code"]
        == "a_capex_roe_below_threshold"
    )

    # CAPEX/CFO ratio above 1.25 fails closed to blocked
    payload["a_capex_review"]["roe_ttm"] = 20.0
    payload["ttm_capex"] = 130
    assert risk_gates.cash_flow_gate(payload)["status"] == "blocked"
    assert (
        risk_gates.cash_flow_gate(payload)["reason_code"]
        == "a_capex_ratio_above_1_25"
    )

    # Incomplete review inputs fail closed to incomplete
    del payload["a_capex_review"]["expansion_rationale"]
    assert risk_gates.cash_flow_gate(payload)["status"] == "incomplete"
    assert (
        risk_gates.cash_flow_gate(payload)["reason_code"]
        == "a_capex_review_incomplete"
    )


def _snapshot(now, count):
    return {
        "limit_down_count": count,
        "universe": 5000,
        "eligible_count": 4800,
        "coverage": "full",
        "status": "final",
        "source": "official exchange",
        "as_of": now.isoformat(),
        "is_final": True,
    }


def _quote(price=8.0, **extra):
    return {
        "price": price,
        "source": "sina",
        "suspended": False,
        "limit_down_locked": False,
        "trading_status": "trading",
        **extra,
    }


def test_stored_gate_aggregation_is_incomplete_without_backfill():
    legacy = {"roe_3y_avg": 12}
    assert risk_gates.aggregate_fundamentals_gates(legacy) is None
    assert legacy == {"roe_3y_avg": 12}

    partial = {**legacy, "regulatory_gate": {"status": "clear"}}
    status, reasons, gates = risk_gates.aggregate_fundamentals_gates(partial)
    assert status == "incomplete"
    for name in ("roe_structural_gate", "cash_flow_gate"):
        assert gates[name]["status"] == "incomplete"
        assert gates[name]["action_eligible"] is False
        assert gates[name]["reason_code"] == "legacy_field_absent"
        assert f"{name}:legacy_field_absent" in reasons
    assert partial == {**legacy, "regulatory_gate": {"status": "clear"}}


def test_liquidity_shock_500_vs_501_and_only_second_stop():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    base = {
        "stop_loss_triggered": True,
        "stop_loss_20": 8,
        "quote": _quote(),
        "now": now,
    }
    assert (
        risk_gates.liquidity_shock_gate(
            {**base, "market_snapshot": _snapshot(now, 500)}
        )["status"]
        == "clear"
    )
    assert (
        risk_gates.liquidity_shock_gate(
            {**base, "market_snapshot": _snapshot(now, 501)}
        )["status"]
        == "deferred"
    )
    assert (
        risk_gates.liquidity_shock_gate(
            {
                **base,
                "stop_loss_triggered": False,
                "market_snapshot": _snapshot(now, 501),
            }
        )["status"]
        == "not_applicable"
    )


def test_liquidity_shock_due_does_not_extend_window_and_protects_untradeable():
    started = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    due = started + timedelta(hours=24)
    payload = {
        "stop_loss_triggered": True,
        "stop_loss_20": 8,
        "quote": _quote(),
        "market_snapshot": _snapshot(due, 501),
        "now": due,
        "existing_defer": {"defer_started_at": started.isoformat()},
    }
    result = risk_gates.liquidity_shock_gate(payload)
    assert result["status"] == "clear"
    assert result["defer_started_at"] == started.isoformat()
    result = risk_gates.liquidity_shock_gate(
        {**payload, "quote": _quote(suspended=True)}
    )
    assert result["status"] == "untradeable"
    assert result["defer_started_at"] == started.isoformat()


def test_liquidity_shock_incomplete_500_is_not_a_clear_signal():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=CST)
    result = risk_gates.liquidity_shock_gate(
        {
            "stop_loss_triggered": True,
            "stop_loss_20": 8,
            "quote": _quote(),
            "now": now,
            "market_snapshot": {"limit_down_count": 500, "as_of": now.isoformat()},
        }
    )
    assert result["status"] == "incomplete"
