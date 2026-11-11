"""Unrecorded-dividend detection on real 2026 ledger shapes and TuShare dates."""

import pytest

from a_share_research.policy.v0 import cache, commands_holdings

# 000963 lifecycle as recorded before the 2026-10-22 dividend entry.
HUADONG_EVENTS = [
    ("buy", "2026-06-10", 600, 30.1367, 0.0, 0.0, None, 0),
    ("buy", "2026-08-06", 200, 27.81, 4.84, 0.0, None, 0),
]
HUADONG_DIVIDENDS = [
    # Record date before the first buy: not entitled.
    {"record_date": "2026-06-09", "ex_date": "2026-06-10", "pay_date": "2026-06-10",
     "cash_div": 0.58, "split_ratio": 0.0},
    {"record_date": "2026-09-17", "ex_date": "2026-09-18", "pay_date": "2026-09-18",
     "cash_div": 0.35, "split_ratio": 0.0},
]


def test_entitled_unrecorded_dividend_uses_record_date_position():
    missing = commands_holdings.unrecorded_dividends(
        HUADONG_EVENTS, HUADONG_DIVIDENDS, "2026-10-22"
    )
    assert [(m["record_date"], m["shares"], m["cash"]) for m in missing] == [
        ("2026-09-17", 800, pytest.approx(280.0))
    ]


@pytest.mark.parametrize("ledger_day", ["2026-09-17", "2026-09-18"])
def test_dividend_recorded_on_record_or_pay_date_is_not_reported(ledger_day):
    events = [*HUADONG_EVENTS, ("dividend", ledger_day, None, None, 0, 0, 280.0, 0)]
    assert commands_holdings.unrecorded_dividends(
        events, HUADONG_DIVIDENDS, "2026-10-22"
    ) == []


def test_sold_out_or_future_record_date_is_not_entitled():
    sold = [*HUADONG_EVENTS, ("sell", "2026-09-17", 800, 26.0, 0, 10.4, None, 0)]
    assert commands_holdings.unrecorded_dividends(
        sold, HUADONG_DIVIDENDS, "2026-10-22"
    ) == []
    assert commands_holdings.unrecorded_dividends(
        HUADONG_EVENTS, HUADONG_DIVIDENDS, "2026-09-16"
    ) == []


def test_dividend_check_prints_entry_command(isolated_cache_database, monkeypatch, capsys):
    from policy_v0.monitor.test_monitor_snapshot import _seed_holding

    _seed_holding(isolated_cache_database, "000963", shares=800)
    monkeypatch.setattr(
        commands_holdings,
        "_load_lifecycle_events",
        lambda conn, holding_id: HUADONG_EVENTS,
    )
    monkeypatch.setattr(
        commands_holdings, "_implemented_dividends", lambda code: HUADONG_DIVIDENDS
    )
    monkeypatch.setattr(commands_holdings.domain, "cst_today", lambda: "2026-10-22")
    assert cache.main(["dividend-check"]) == 0
    out = capsys.readouterr().out
    assert "应收280.00元" in out
    assert "corporate-action 000963 0.35 0 2026-09-17" in out


def test_zero_tax_sale_warns_with_stamp_duty_estimate(isolated_cache_database, capsys):
    from policy_v0.monitor.test_monitor_snapshot import _seed_holding

    _seed_holding(isolated_cache_database, "600036", shares=200)
    argv = ["--confirm-write", "sell-holding", "600036", "42.88", "all"]
    assert cache.main([*argv, "--date", "2026-10-13"]) == 0
    assert "印花税（成交额×0.5‰≈4.29元）" in capsys.readouterr().out


def test_recorded_bonus_split_is_matched_by_share_adjustment():
    split = [{"record_date": "2026-09-17", "ex_date": "2026-09-18",
              "pay_date": "2026-09-18", "cash_div": 0.0, "split_ratio": 0.3}]
    events = [*HUADONG_EVENTS, ("adjustment", "2026-09-17", 240, 22.7, 0, 0, None, 0)]
    assert commands_holdings.unrecorded_dividends(events, split, "2026-10-22") == []
    assert commands_holdings.unrecorded_dividends(HUADONG_EVENTS, split, "2026-10-22")


def test_changed_share_count_prompts_entitled_total_that_records_correctly(
    isolated_cache_database, monkeypatch, capsys
):
    import sqlite3

    from policy_v0.monitor.test_monitor_snapshot import _seed_holding

    # 1000 shares on the record date, 500 sold afterwards; 500 remain open.
    _seed_holding(isolated_cache_database, "000963", shares=500)
    events = [
        ("buy", "2026-06-10", 1000, 30.0, 0.0, 0.0, None, 0),
        ("sell", "2026-10-08", 500, 27.0, 0.0, 6.75, None, 0),
    ]
    monkeypatch.setattr(commands_holdings, "_load_lifecycle_events", lambda c, h: events)
    monkeypatch.setattr(
        commands_holdings, "_implemented_dividends", lambda code: HUADONG_DIVIDENDS[1:]
    )
    monkeypatch.setattr(commands_holdings.domain, "cst_today", lambda: "2026-10-22")
    assert cache.main(["dividend-check"]) == 0
    out = capsys.readouterr().out
    assert "corporate-action" not in out
    command = out.split("用 a-stock-cache ", 1)[1].split("；", 1)[0].split()
    # Keep monkeypatch active: undoing it would also drop the isolated database.
    assert cache.main(command) == 0
    with sqlite3.connect(isolated_cache_database) as conn:
        cash = conn.execute(
            "SELECT cash_amount FROM holding_events WHERE event_type='dividend'"
        ).fetchone()[0]
    assert cash == pytest.approx(350.0)


def test_partially_recorded_cash_and_split_gives_no_auto_command(
    isolated_cache_database, monkeypatch, capsys
):
    from policy_v0.monitor.test_monitor_snapshot import _seed_holding

    mixed = [{"record_date": "2026-09-17", "ex_date": "2026-09-18",
              "pay_date": "2026-09-18", "cash_div": 0.35, "split_ratio": 0.3}]
    events = [*HUADONG_EVENTS, ("dividend", "2026-09-17", None, None, 0, 0, 280.0, 0)]
    [item] = commands_holdings.unrecorded_dividends(events, mixed, "2026-10-22")
    assert item["partially_recorded"] is True

    _seed_holding(isolated_cache_database, "000963", shares=800)
    monkeypatch.setattr(commands_holdings, "_load_lifecycle_events", lambda c, h: events)
    monkeypatch.setattr(commands_holdings, "_implemented_dividends", lambda code: mixed)
    monkeypatch.setattr(commands_holdings.domain, "cst_today", lambda: "2026-10-22")
    assert cache.main(["dividend-check"]) == 0
    out = capsys.readouterr().out
    assert "只记了一部分" in out and "a-stock-cache --confirm-write" not in out
