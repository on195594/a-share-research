"""Risk-budget sizing against the captured no-action monitor snapshot (100,000 CNY, 2%/8% default)."""
import copy
import json
from pathlib import Path

import pytest

from a_share_research.policy.v0 import cache, domain, position_size


@pytest.fixture
def snapshot():
    return json.loads((Path(__file__).parent / "fixtures/monitor/agent-behavior-snapshots.json").read_text())["no_action"]


def personal(snapshot):
    snapshot["account"]["risk_policy"] |= {"policy_id": "personal", "max_position_risk_pct": 6.0,
                                           "max_portfolio_risk_pct": 18.0}
    return snapshot


def test_size_fits_single_stock_budget_in_board_lots(snapshot):
    original = copy.deepcopy(snapshot)
    stop_pct = domain.get_stop_loss_pct("A通用")[1]
    result = position_size.size(snapshot, "600900", 29.5, "A")
    per_share = 29.5 - round(29.5 * stop_pct, 3)
    # 2% of 100,000 = 2,000 of stop risk; the portfolio still has 8,000 - 300 left.
    assert result["limits"] == {"single_stock_remaining": 2000, "portfolio_remaining": 7700}
    assert result["max_shares"] == int(2000 // per_share) // 100 * 100
    assert result["max_shares"] * per_share <= 2000 < (result["max_shares"] + 100) * per_share
    assert result["binding_limit"] == "single_stock_remaining"
    assert [t["cumulative_shares"] for t in result["tranches"]] == [100, 200, 300] and result["max_shares"] == 300
    assert [t["stage"] for t in result["tranches"]] == ["1/3", "2/3", "1"]
    assert result["action_status"] == "not_formed" and snapshot == original


def test_existing_position_and_portfolio_budget_reduce_room(snapshot):
    # 600000 already carries 300 of stop risk, so only 1,700 is left for it.
    added = position_size.size(snapshot, "600000", 12.3, "B")
    assert added["existing_position_stop_risk"] == 300
    assert added["limits"]["single_stock_remaining"] == 1700
    assert added["max_shares"] * added["risk_per_share"] <= 1700
    # Only 100 of the 8,000 portfolio budget left.
    snapshot["account"] |= {"total_stop_risk": 7900, "known_stop_risk_lower_bound": 7900, "total_stop_risk_pct": 7.9}
    tight = position_size.size(snapshot, "600900", 29.5, "A")
    assert tight["binding_limit"] == "budget_below_one_lot" and tight["max_shares"] == 0
    assert [t["cumulative_shares"] for t in tight["tranches"]] == [0, 0, 0]


def test_star_board_minimum_and_personal_policy(snapshot):
    # 688578 at 83: 2% leaves room for ~120 shares, below the STAR 200-share minimum.
    assert position_size.size(snapshot, "688578", 83.0, "A")["max_shares"] == 0
    result = position_size.size(personal(snapshot), "688578", 83.0, "A")
    assert result["max_shares"] == int(6000 // result["risk_per_share"]) and result["max_shares"] >= 200
    steps = [t["cumulative_shares"] for t in result["tranches"]]
    assert steps[-1] == result["max_shares"] and all(s == 0 or s >= 200 for s in steps)
    # 6,000 / (83 - 66.4) = 361 shares: the 1/3 stage (120) is below the STAR 200 minimum and stays 0
    # instead of shifting the 2/3 stage into the first-tranche slot.
    assert steps == [0, 240, 361]


def test_cash_limit_and_fail_closed_inputs(snapshot):
    result = position_size.size(personal(snapshot), "600900", 29.5, "A", cash=5000)
    assert result["max_shares"] == 100 and result["binding_limit"] == "cash"
    # One lot total: the 1/3 and 2/3 stages are not executable, the first tranche is not the full lot.
    assert [t["cumulative_shares"] for t in result["tranches"]] == [0, 0, 100]
    with pytest.raises(ValueError, match="框架"):
        position_size.size(snapshot, "600900", 29.5, "X")
    snapshot["account"]["denominator_requires_review"] = True
    with pytest.raises(ValueError, match="分母"):
        position_size.size(snapshot, "600900", 29.5, "A")
    # A snapshot whose risk total is unknown is rejected by the monitor contract before sizing.
    snapshot["account"] |= {"denominator_requires_review": False, "total_stop_risk": None,
                            "total_stop_risk_pct": None, "risk_budget_status": None}
    with pytest.raises(ValueError):
        position_size.size(snapshot, "600900", 29.5, "A")


def test_cli_reads_snapshot_file_only(snapshot, tmp_path, capsys):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert cache.main(["position-size", "--snapshot", str(path), "--code", "600900", "--price", "29.5",
                       "--framework", "a"]) == 0
    assert json.loads(capsys.readouterr().out)["max_shares"] == 300


def test_untradable_board_is_not_sized(snapshot):
    """0.1.36: a policy's untradable_boards refuses sizing; other boards are unaffected."""
    snapshot = personal(snapshot)
    before = [position_size.size(snapshot, code, price, "A")["max_shares"]
              for code, price in (("600900", 29.5), ("300750", 380.0), ("688578", 83.0))]
    snapshot["account"]["risk_policy"] |= {"untradable_boards": ["STAR"]}
    snapshot["account"]["risk_policy"]["field_sources"] |= {"untradable_boards": "policy_file"}
    with pytest.raises(ValueError, match="科创板"):
        position_size.size(snapshot, "688578", 83.0, "A")
    # Main board and ChiNext sizing is unchanged by a STAR-only restriction.
    assert [position_size.size(snapshot, code, price, "A")["max_shares"]
            for code, price in (("600900", 29.5), ("300750", 380.0))] == before[:2]
    assert before[2] == 361


@pytest.mark.parametrize("code", ["300750", "002946", "002648"])
def test_checkpoint_note_limits_action_to_first_tranche(snapshot, code):
    from datetime import date
    from a_share_research.policy.v0 import monitor_research
    cards = json.loads((Path(__file__).parent / "fixtures/monitor/reference-evidence-cards.json").read_text())
    card = next(c for c in cards["cards"] if c["code"] == code)
    research, prompts, _ = monitor_research.summarize(
        {"status": "ok", "cards": [card], "missing": []}, ["600000"], date(2026, 11, 4))
    snapshot |= {"research": research, "review_prompts": prompts}
    result = position_size.size(snapshot, code, card["current"]["last_close"], "A")
    if code == "002648":
        assert "加仓检查点" not in result["note"]
    else:
        assert f"首批 tranches[0]：{result['tranches'][0]['cumulative_shares']} 股" in result["note"]
        assert "参考，不阻止首批" in result["note"] and "更新的有效研究确认" in result["note"]
        if code == "300750":
            assert result["tranches"][0]["cumulative_shares"] == 0
