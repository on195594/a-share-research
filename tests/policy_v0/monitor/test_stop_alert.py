"""Intraday stop-line alerts from canonical holding stops, deduplicated per day."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from a_share_research.policy.v0 import cache, domain, market_quotes
from a_share_research.policy.v0.commands_holdings import new_stop_alerts, stop_level

# 000963 stops after the 2026-09-17 dividend adjustment.
STOP1, STOP2 = 24.824, 23.364
CST = timezone(timedelta(hours=8))


@pytest.mark.parametrize(
    ("price", "level"),
    [(26.21, None), (25.50, "near"), (24.824, "stop1"), (23.0, "stop2")],
)
def test_stop_level_uses_canonical_lines(price, level):
    assert stop_level(price, STOP1, STOP2) == level


def test_alerts_once_per_day_per_worsening_level():
    alerts, state = new_stop_alerts({"000963": "near"}, {}, "2026-10-22")
    assert alerts == [("000963", "near")]
    assert new_stop_alerts({"000963": "near"}, state, "2026-10-22")[0] == []
    alerts, state = new_stop_alerts({"000963": "stop1"}, state, "2026-10-22")
    assert alerts == [("000963", "stop1")]
    assert new_stop_alerts({"000963": "near"}, state, "2026-10-22")[0] == []
    assert new_stop_alerts({"000963": "stop1"}, state, "2026-10-23")[0] == [("000963", "stop1")]


def _run(monkeypatch, tmp_path, capsys, now, price, quote_time="10:30:00"):
    monkeypatch.setattr(domain, "cst_now", lambda: now)
    monkeypatch.setattr(
        market_quotes,
        "fetch_current_price_quotes",
        lambda codes: {c: market_quotes.PriceQuote(price, "2026-10-22", quote_time, "fixture")
                       for c in codes},
    )
    code = cache.main(["stop-alert"])
    return code, capsys.readouterr()


def test_command_alerts_then_stays_silent_and_keeps_state_beside_the_database(
    isolated_cache_database, monkeypatch, tmp_path, capsys
):
    from policy_v0.monitor.test_monitor_snapshot import _seed_holding

    # Fixture holding: stop_loss_15=85, stop_loss_20=80.
    _seed_holding(isolated_cache_database, "000963", shares=800)
    now = datetime(2026, 10, 22, 10, 31, tzinfo=CST)
    code, out = _run(monkeypatch, tmp_path, capsys, now, 84.0)
    assert code == 0 and "已跌破第一档止损85.000" in out.out
    state = json.loads((tmp_path / "stop-alert-state.json").read_text())
    assert state == {"date": "2026-10-22", "levels": {"000963": "stop1"}}
    assert _run(monkeypatch, tmp_path, capsys, now, 84.0)[1].out == ""


def test_command_is_silent_at_lunch_and_fails_visibly_on_stale_quotes(
    isolated_cache_database, monkeypatch, tmp_path, capsys
):
    from policy_v0.monitor.test_monitor_snapshot import _seed_holding

    _seed_holding(isolated_cache_database, "000963", shares=800)
    lunch = datetime(2026, 10, 22, 12, 0, tzinfo=CST)
    assert _run(monkeypatch, tmp_path, capsys, lunch, 70.0)[1].out == ""
    late = datetime(2026, 10, 22, 14, 0, tzinfo=CST)
    code, out = _run(monkeypatch, tmp_path, capsys, late, 70.0, quote_time="10:30:00")
    assert code == 1 and out.out == "" and "行情无效或过期" in out.err
