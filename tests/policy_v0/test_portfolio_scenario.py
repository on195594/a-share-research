import copy
import json
from pathlib import Path

import pytest

from a_share_research.policy.v0 import cache, portfolio_scenario


@pytest.fixture
def snapshot():
    return json.loads((Path(__file__).parent / "fixtures/monitor/agent-behavior-snapshots.json").read_text())["no_action"]


def test_sell_converts_stock_to_cash_without_double_counting(snapshot):
    original = copy.deepcopy(snapshot)
    result = portfolio_scenario.compare(snapshot, {"600000": 100}, fees=5, cash=500)
    assert result["before"]["portfolio_value"] == 100000
    assert result["after"]["portfolio_value"] == 99995
    assert result["cash_delta"] == 1225
    assert result["cash_after"] == 1725
    assert result["source_denominator"]["denominator_components"] == snapshot["account"]["denominator_components"]
    assert result["source_denominator"]["portfolio_value_as_of"] == snapshot["account"]["portfolio_value_as_of"]
    assert result["source_denominator"]["denominator_requires_review"] is False
    assert result["after"]["total_stop_risk"] == pytest.approx(270)
    assert result["after"]["total_stop_risk_pct"] == pytest.approx(270 / 99995 * 100)
    assert result["after"]["stock_market_value"] == 11070
    assert result["action_status"] == "not_formed"
    assert result["execution_status"] == "not_checked"
    assert not result["writes"] and snapshot == original


def test_full_exit_unknown_cash_and_source_blocks_are_preserved(snapshot):
    snapshot["data_status"] = "partial"
    snapshot["action_status"] = "review_candidate"
    snapshot["review_status"] = "review_required"
    snapshot["stop_reason"] = None
    snapshot["manifest"]["stop_reason"] = None
    result = portfolio_scenario.compare(snapshot, {"600000": 1000}, fees=0)
    assert result["after"]["total_stop_risk"] == 0
    assert result["positions_after"][0]["shares"] == 0
    assert result["cash_before"] is None and result["cash_after"] is None
    assert result["source_action_status"] == "review_candidate"
    assert result["source_review_status"] == "review_required"
    assert result["source_data_status"] == "partial"
    assert result["action_status"] == "not_formed"


@pytest.mark.parametrize("sales,fees,cash", [
    ({}, 0, None), ({"600000": 1001}, 0, None), ({"600001": 100}, 0, None),
    ({"600000": True}, 0, None), ({"600000": 1.5}, 0, None),
    ({"600000": 100}, float("nan"), None), ({"600000": 100}, -1, None),
    ({"600000": 100}, 1500, None), ({"600000": 100}, 0, float("inf")),
    ({"600000": 100}, 0, 100000),
])
def test_invalid_inputs_fail_closed(snapshot, sales, fees, cash):
    with pytest.raises(ValueError):
        portfolio_scenario.compare(snapshot, sales, fees=fees, cash=cash)


@pytest.mark.parametrize("field,value", [("denominator_requires_review", True),
                                          ("denominator_status", "market_value_fallback")])
def test_unconfirmed_denominator_cannot_produce_a_scenario(snapshot, field, value):
    snapshot["account"][field] = value
    with pytest.raises(ValueError):
        portfolio_scenario.compare(snapshot, {"600000": 100}, fees=0)


def test_conflicting_snapshot_totals_are_not_silently_replaced(snapshot):
    snapshot["account"]["total_stop_risk"] = 500
    snapshot["account"]["known_stop_risk_lower_bound"] = 500
    snapshot["account"]["total_stop_risk_pct"] = 0.5
    with pytest.raises(ValueError, match="汇总"):
        portfolio_scenario.compare(snapshot, {"600000": 100}, fees=0)


def test_snapshot_freshness_is_not_a_claim_about_today(snapshot):
    result = portfolio_scenario.compare(snapshot, {"600000": 100}, fees=0)
    assert result["snapshot_as_of"] == snapshot["as_of"]
    assert result["execution_status"] == "not_checked"
    snapshot["holdings"][0]["quote"]["status"] = "stale"
    with pytest.raises(ValueError):
        portfolio_scenario.compare(snapshot, {"600000": 100}, fees=0)


def test_cli_never_creates_account_database(snapshot, tmp_path, monkeypatch, capsys):
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(snapshot))
    database = tmp_path / "absent.db"
    monkeypatch.setenv("CACHE_DB_PATH", str(database))
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(tmp_path / "absent.env"))
    args = ["portfolio-scenario", "--snapshot", str(source), "--sell", "600000:100", "--fees", "0"]
    assert cache.main(args) == 0
    assert json.loads(capsys.readouterr().out)["writes"] is False
    assert not database.exists()
    assert cache.COMMAND_CLASSIFICATION["portfolio-scenario"] == "R0"
    assert cache.main(args + ["--sell", "600000:100"]) == 1
    assert not database.exists()
