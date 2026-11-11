"""2026-10-22 spec: supplied P0 evidence survives refreshes within one annual period."""

import io
import json
import sqlite3
from pathlib import Path
from datetime import date

import pytest

from a_share_research import db as app_db
from a_share_research.policy.v0 import cache, risk_gates, store

TODAY = date(2026, 10, 22)
FACTS = {
    "report_period": "2025年报",
    "exchange": "SSE",
    "board": "main",
    "as_of": "2026-10-22T10:00:00+08:00",
    "sources": ["长江电力2025年年度报告（上交所公司公告）"],
    "listing_status": "normal",
    "financial_opinion": "unqualified",
    "internal_control_opinion": "unqualified",
    "investigation_status": "none",
    "latest_annual_attributable_profit": 3.4e10,
    "parent_unallocated_profit_at_year_end": 5.0e10,
    "three_year_cash_dividend": 6.9e10,
    "three_year_annual_profits": [2.7e10, 3.2e10, 3.4e10],
}


def _gate(**updates):
    return risk_gates.regulatory_evidence_gate({**FACTS, **updates}, "600900", today=TODAY)


def _carry(previous, period="2025年报", today=TODAY):
    return risk_gates.carry_forward_regulatory_gate(previous, "600900", period, today=today)


def test_same_annual_period_rejudges_clear_and_blocked_evidence():
    clear = _gate()
    assert clear["status"] == "clear"
    assert _carry(clear) == clear
    assert _carry(_gate(investigation_status="正式立案"))["status"] == "blocked"


def test_new_period_incomplete_or_factless_evidence_is_not_carried():
    assert _carry(_gate(), period="2026年报") is None
    assert _carry(_gate(listing_status=None)) is None
    legacy = dict(_gate())
    legacy.pop("evidence_facts")
    assert _carry(legacy) is None


def test_carry_forward_applies_changed_rule_thresholds(monkeypatch):
    # 3-year dividends 40M vs avg profit 100M: clear at 30%, blocked at 50% (50M floor).
    facts = {"three_year_cash_dividend": 4.0e7, "three_year_annual_profits": [1e8] * 3,
             "latest_annual_attributable_profit": 1e8}
    rule = dict(risk_gates.REGULATORY_RULES[("SSE", "main")])
    assert rule["dividend_ratio"] == 0.3 and rule["dividend_amount"] == 5e7
    old = _gate(**facts)
    assert old["status"] == "clear"
    monkeypatch.setitem(
        risk_gates.REGULATORY_RULES, ("SSE", "main"), {**rule, "dividend_ratio": 0.5}
    )
    assert _carry(old)["status"] == "blocked"


def test_stale_rule_still_fails_closed_after_carry_forward():
    assert _carry(_gate(), today=date(2027, 6, 1))["status"] == "incomplete"


@pytest.mark.parametrize("field", ["listing_status", "investigation_status"])
def test_explicit_null_status_is_missing_evidence_not_none_token(field):
    assert _gate(**{field: None})["status"] == "incomplete"


def test_preset_sub_check_cannot_override_facts():
    preset = {"status": "clear", "sources": ["上交所公司公告"],
              "as_of": "2026-10-22T10:00:00+08:00", "report_period": "2024年报"}
    with pytest.raises(ValueError):
        _gate(listing_status="st", listing_risk=preset)


def _seed_fundamentals(database, data_period="2025年报"):
    app_db.connect(Path(database)).close()
    with sqlite3.connect(database) as conn:
        conn.execute(
            """INSERT INTO stock_fundamentals
               (code, name, industry, data, updated_at, ttl_hours)
               VALUES ('600900', '长江电力', '水电', ?, '2026-10-15T03:33:00+00:00', 168)""",
            (json.dumps({"data_period": data_period, "null_reasons": {},
                         "field_provenance": {}}),),
        )


def _run_evidence(monkeypatch, facts):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(facts)))
    return cache.main(["--confirm-write", "regulatory-evidence", "600900"])


def _stored(database):
    with sqlite3.connect(database) as conn:
        raw, updated_at = conn.execute(
            "SELECT data, updated_at FROM stock_fundamentals WHERE code='600900'"
        ).fetchone()
    return json.loads(raw), updated_at


def _refresh(period):
    # What fetch writes: a fresh payload whose P0 gate is the unavailable default.
    default = risk_gates.regulatory_gate(
        {"code": "600900", "report_period": period, "as_of": period,
         "sources": ["unavailable"]}
    )
    data = {"data_period": period, "null_reasons": {}, "field_provenance": {}}
    store.apply_regulatory_gate(data, default)
    store.set_fundamentals("600900", "长江电力", "水电", data, ttl=168)


def test_command_merges_gate_and_refresh_rejudges_latest_stored_evidence(
    isolated_cache_database, monkeypatch
):
    _seed_fundamentals(isolated_cache_database)
    assert _run_evidence(monkeypatch, FACTS) == 0
    data, updated_at = _stored(isolated_cache_database)
    gate = data["regulatory_gate"]
    assert gate["evidence_facts"] == FACTS
    assert gate["status"] == risk_gates.regulatory_gate({**FACTS, "code": "600900"})["status"]
    assert updated_at == "2026-10-15T03:33:00+00:00"

    # Evidence written after a fetch started is read inside the refresh's write.
    blocked = {**FACTS, "investigation_status": "正式立案"}
    assert _run_evidence(monkeypatch, blocked) == 0
    _refresh("2025年报")
    assert _stored(isolated_cache_database)[0]["regulatory_gate"]["status"] == "blocked"
    _refresh("2026年报")
    assert _stored(isolated_cache_database)[0]["regulatory_gate"]["status"] == "incomplete"


@pytest.mark.parametrize(
    "facts",
    [
        {**FACTS, "report_period": "2024年报"},
        {**FACTS, "report_period": None},
        {**FACTS, "listing_risk": {"status": "clear", "sources": ["上交所公司公告"],
                                   "as_of": "2026-10-22T10:00:00+08:00"}},
    ],
)
def test_command_rejects_wrong_period_or_preset_checks(
    facts, isolated_cache_database, monkeypatch
):
    _seed_fundamentals(isolated_cache_database)
    assert _run_evidence(monkeypatch, facts) == 1
    assert "regulatory_gate" not in _stored(isolated_cache_database)[0]


def test_command_without_confirmation_writes_nothing(isolated_cache_database, monkeypatch):
    _seed_fundamentals(isolated_cache_database)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(FACTS)))
    assert cache.main(["regulatory-evidence", "600900"]) == 3
    assert "regulatory_gate" not in _stored(isolated_cache_database)[0]
