"""Unrecorded-dividend detection on real 2026 ledger shapes and TuShare dates."""

import pytest

from a_share_research.policy.v0 import cache, commands_holdings
from support.policy_replay import HUADONG_DIVIDENDS, HUADONG_EVENTS

# 000963 lifecycle as recorded before the 2026-10-22 dividend entry.


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
    from support.policy_replay import _seed_holding

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
    assert "核对到账后经账本工具提交公司行动：每股现金0.35元 转增0 登记日2026-09-17" in out


def test_recorded_bonus_split_is_matched_by_share_adjustment():
    split = [{"record_date": "2026-09-17", "ex_date": "2026-09-18",
              "pay_date": "2026-09-18", "cash_div": 0.0, "split_ratio": 0.3}]
    events = [*HUADONG_EVENTS, ("adjustment", "2026-09-17", 240, 22.7, 0, 0, None, 0)]
    assert commands_holdings.unrecorded_dividends(events, split, "2026-10-22") == []
    assert commands_holdings.unrecorded_dividends(HUADONG_EVENTS, split, "2026-10-22")


def test_changed_share_count_prompts_the_entitled_total(
    isolated_cache_database, monkeypatch, capsys
):

    from support.policy_replay import _seed_holding

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
    assert "提交公司行动" not in out
    assert "核对到账后经账本工具提交分红 350.00元" in out


def test_partially_recorded_cash_and_split_gives_no_auto_command(
    isolated_cache_database, monkeypatch, capsys
):
    from support.policy_replay import _seed_holding

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
    assert "只记了一部分" in out and "经账本工具提交" not in out
