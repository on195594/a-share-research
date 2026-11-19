"""Ledger trades linked to the Research report they relied on."""

import pytest
from a_share_research.core.market_data import MarketDataResult

from a_share_research.policy.v0 import cache, commands_holdings, db, research_client


def _trade(code, event_type, day, price, shares, report_id=None, realized=None):
    """Seed one account projection event (the v0 ledger writers were removed 11-19)."""
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO holding_events
               (holding_id, code, event_type, event_date, shares, price,
                realized_pnl, created_at, report_id)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, event_type, day, shares, price, realized, day, report_id),
        )
        conn.commit()


def _close(value, freshness=0):
    return MarketDataResult(
        value,
        "ok" if freshness == 0 else "degraded",
        "tushare.daily",
        "2026-11-05T00:00:00Z",
        freshness_days=freshness,
        source_as_of="2026-05-29" if freshness else None,
    )


def _market(monkeypatch, closes, stock, dividends=()):
    monkeypatch.setattr(commands_holdings, "_index_closes", lambda *_: closes)
    monkeypatch.setattr(
        commands_holdings, "_close_result", lambda code, day: stock[day]
    )
    monkeypatch.setattr(
        commands_holdings, "_implemented_dividends", lambda code: list(dividends)
    )


def _dividend(ex_date, cash=0.0, split=0.0, record_date=None):
    return {
        "record_date": record_date or ex_date,
        "ex_date": ex_date,
        "pay_date": ex_date,
        "cash_div": cash,
        "split_ratio": split,
    }


def _lines(capsys):
    out = capsys.readouterr().out
    return out, [line for line in out.splitlines() if line.startswith("  报告")]


def test_stock_and_index_share_the_same_closes_not_the_trade_price(monkeypatch, capsys):
    _trade("000001", "buy", "2026-05-27", 10.0, 500, 101)
    _trade("000001", "buy", "2026-06-01", 11.0, 100)
    _trade("000001", "sell", "2026-09-30", 12.0, 100, 103, realized=200.0)
    # 5-27 is not a trading day here: the buy starts from the 5-28 close of both.
    closes = {"2026-05-28": 100.0, "2026-09-30": 110.0, "2026-10-21": 121.0}
    stock = {
        "2026-05-28": _close(11.0),
        "2026-09-30": _close(13.0),
        "2026-10-21": _close(15.4),
    }
    _market(monkeypatch, closes, stock)
    assert cache.main(["report-trades", "--end", "2026-10-22"]) == 0
    out, (buy, sell) = _lines(capsys)
    assert "关联报告的买卖 2 笔，未关联 1 笔" in out
    assert "算到 2026-10-21 收盘" in out
    assert "买入后 500股 @ 10.000：含分红送转 +40.00%，基准 +21.00%，差 +19.00pp" in buy
    assert "卖出后 100股 @ 12.000：含分红送转 +18.46%，基准 +10.00%，差 +8.46pp" in sell
    assert "已实现" in sell and "已实现" not in buy


def test_same_day_trade_has_no_invented_gap_from_its_trade_price(monkeypatch, capsys):
    _trade("000001", "buy", "2026-06-01", 10.0, 500, 101)
    _market(
        monkeypatch,
        {"2026-06-01": 100.0},
        {"2026-06-01": _close(11.0)},
        [_dividend(None, cash=1.0, record_date="2026-06-01")],
    )
    assert cache.main(["report-trades", "--end", "2026-06-01"]) == 0
    assert "含分红送转 +0.00%，基准 +0.00%，差 +0.00pp" in _lines(capsys)[1][0]

@pytest.mark.parametrize(
    "day", ["20261105", "2026-W45-4", "2026W454", "2026-11-5", "2026-02-29"]
)
@pytest.mark.parametrize(
    "command,arguments",
    [
        ("report-trades", ["--end"]),
        ("benchmark-report", ["--end"]),
    ],
)
def test_ledger_cli_rejects_noncanonical_dates_before_io(
    monkeypatch, capsys, isolated_cache_database, command, arguments, day
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid dates must be rejected before database or Research access")

    monkeypatch.setattr(db, "get_db", forbidden)
    monkeypatch.setattr(db, "read_only_db_session", forbidden)
    monkeypatch.setattr(research_client, "request_json", forbidden)
    assert cache.main([command, *arguments, day]) == 1
    assert "日期必须为 YYYY-MM-DD" in capsys.readouterr().err
    assert not isolated_cache_database.exists()


@pytest.mark.parametrize("day", ["2024-02-29", "2026-11-05"])
def test_canonical_trade_date_is_preserved_and_included_on_same_day(
    monkeypatch, capsys, day
):
    _trade("000001", "buy", day, 10.0, 100, 7)
    _market(monkeypatch, {day: 100.0}, {day: _close(10.0)})
    assert cache.main(["report-trades", "--end", day]) == 0
    out, lines = _lines(capsys)
    assert "关联报告的买卖 1 笔，未关联 0 笔" in out
    assert len(lines) == 1 and f"000001 {day} 买入后" in lines[0]
    assert "含分红送转 +0.00%，基准 +0.00%，差 +0.00pp" in lines[0]


def test_a_split_is_not_scored_as_a_price_collapse(monkeypatch, capsys):
    _trade("000001", "buy", "2026-06-01", 10.0, 500, 101)
    closes = {"2026-06-01": 100.0, "2026-10-21": 100.0}
    stock = {"2026-06-01": _close(10.0), "2026-10-21": _close(5.0)}
    _market(monkeypatch, closes, stock, [_dividend("2026-07-01", split=1.0)])
    assert cache.main(["report-trades", "--end", "2026-10-21"]) == 0
    assert "含分红送转 +0.00%，基准 +0.00%，差 +0.00pp" in _lines(capsys)[1][0]


def test_distributions_after_the_position_closed_still_count_for_a_sell(
    monkeypatch, capsys
):
    _trade("000001", "buy", "2026-05-01", 10.0, 500)
    _trade("000001", "sell", "2026-06-01", 10.0, 500, 103)
    closes = {"2026-05-01": 100.0, "2026-06-01": 100.0, "2026-10-21": 100.0}
    stock = {"2026-06-01": _close(10.0), "2026-10-21": _close(9.0)}
    _market(monkeypatch, closes, stock, [_dividend("2026-07-01", cash=1.0)])
    assert cache.main(["report-trades", "--end", "2026-10-21"]) == 0
    assert "含分红送转 +0.00%" in _lines(capsys)[1][0]


@pytest.mark.parametrize(
    "items, expected",
    [
        ([_dividend("2026-07-01", cash=1.0)], 0.1),
        ([_dividend("2026-07-01", split=1.0)], 1.0),
        # Split first: the later cash is paid on twice the shares.
        ([_dividend("2026-07-01", split=1.0), _dividend("2026-08-01", cash=1.0)], 1.2),
        # Cash first: it is paid before the shares double, in ex-date order.
        ([_dividend("2026-08-01", split=1.0), _dividend("2026-07-01", cash=1.0)], 1.1),
        # Went ex on the start day: the start close already excludes it.
        ([_dividend("2026-06-01", cash=1.0)], 0.0),
        # Goes ex after the end date: not known yet at that time.
        ([_dividend("2026-10-22", cash=1.0)], 0.0),
    ],
)
def test_total_return_counts_only_distributions_inside_the_window(items, expected):
    assert commands_holdings.total_return(
        10.0, 10.0, items, "2026-06-01", "2026-10-21"
    ) == pytest.approx(expected)


@pytest.mark.parametrize("record_date", ["2026-06-01", "2026-07-01", "2026-10-21"])
def test_distribution_without_an_ex_date_blocks_the_comparison(record_date):
    item = _dividend(None, cash=1.0, record_date=record_date)
    assert (
        commands_holdings.total_return(10.0, 10.0, [item], "2026-06-01", "2026-10-21")
        is None
    )
    outside = _dividend(None, cash=1.0, record_date="2026-05-01")
    assert (
        commands_holdings.total_return(
            10.0, 10.0, [outside], "2026-06-01", "2026-10-21"
        )
        == 0.0
    )


def test_undated_start_day_distribution_does_not_become_a_certain_price_only_loss(
    monkeypatch, capsys
):
    item = _dividend(None, cash=1.0, record_date="2026-06-01")
    assert (
        commands_holdings.total_return(10.0, 9.0, [item], "2026-06-01", "2026-06-03")
        is None
    )
    known = _dividend("2026-06-02", cash=1.0, record_date="2026-06-01")
    assert (
        commands_holdings.total_return(10.0, 9.0, [known], "2026-06-01", "2026-06-03")
        == 0.0
    )
    _trade("000001", "buy", "2026-06-01", 10.0, 500, 101)
    _market(
        monkeypatch,
        {"2026-06-01": 100.0, "2026-06-03": 100.0},
        {"2026-06-01": _close(10.0), "2026-06-03": _close(9.0)},
        [item],
    )
    assert cache.main(["report-trades", "--end", "2026-06-03"]) == 0
    line = _lines(capsys)[1][0]
    assert "期间有分红或送转缺除权日，不作比较" in line
    assert "含分红送转" not in line and "差" not in line


@pytest.mark.parametrize(
    "stock, reason",
    [
        # The stock's only close before the trade day is pre-trade, so never "after buying".
        (
            {"2026-06-01": _close(9.0, 3), "2026-10-21": _close(10.0)},
            "起点 2026-06-01 没有当日收盘",
        ),
        (
            {"2026-06-01": _close(10.0), "2026-10-21": _close(9.0, 5)},
            "期末 2026-10-21 没有当日收盘",
        ),
    ],
)
def test_stale_closes_are_not_compared_with_the_index(
    stock, reason, monkeypatch, capsys
):
    _trade("000001", "buy", "2026-06-01", 10.0, 500, 101)
    _market(monkeypatch, {"2026-06-01": 100.0, "2026-10-21": 110.0}, stock)
    assert cache.main(["report-trades", "--end", "2026-10-21"]) == 0
    line = _lines(capsys)[1][0]
    assert reason in line and "行情停在 2026-05-29" in line and "差" not in line


def test_no_index_data_exits_cleanly_without_a_traceback(monkeypatch, capsys):
    _trade("000001", "buy", "2026-06-01", 10.0, 500, 101)

    def unavailable(*_):
        raise RuntimeError("H00300.CSI 指数行情不可用（EMPTY）")

    monkeypatch.setattr(commands_holdings, "_index_closes", unavailable)
    assert cache.main(["report-trades", "--end", "2026-06-01"]) == 1
    assert "暂无法计算" in capsys.readouterr().err


def test_one_unavailable_stock_does_not_hide_the_others(monkeypatch, capsys):
    _trade("000001", "buy", "2026-06-01", 10.0, 500, 101)
    _trade("600036", "buy", "2026-06-01", 10.0, 500, 102)
    monkeypatch.setattr(
        commands_holdings,
        "_index_closes",
        lambda *_: {"2026-06-01": 100.0, "2026-10-21": 110.0},
    )

    def dividends(code):
        if code == "000001":
            raise RuntimeError("TuShare 分红数据不可用（failed）")
        return []

    monkeypatch.setattr(commands_holdings, "_implemented_dividends", dividends)
    monkeypatch.setattr(commands_holdings, "_close_result", lambda *_: _close(10.0))
    assert cache.main(["report-trades", "--end", "2026-10-21"]) == 0
    first, second = _lines(capsys)[1]
    assert "暂不计算（TuShare 分红数据不可用" in first
    assert "差 -10.00pp" in second


def test_without_linked_trades_no_market_data_is_requested(monkeypatch, capsys):
    _trade("000001", "buy", "2026-06-01", 10.0, 500)

    def forbidden(*_):
        raise AssertionError("no linked trades should mean no market data call")

    monkeypatch.setattr(commands_holdings, "_index_closes", forbidden)
    monkeypatch.setattr(commands_holdings, "_close_result", forbidden)
    monkeypatch.setattr(commands_holdings, "_implemented_dividends", forbidden)
    assert cache.main(["report-trades"]) == 0
    assert "关联报告的买卖 0 笔，未关联 1 笔" in capsys.readouterr().out



@pytest.mark.parametrize("outcome", ["ok", "degraded", "failed"])
def test_report_trades_reuses_prices_and_failures_only_within_one_command(
    monkeypatch, capsys, outcome
):
    for code in ("000001", "600900"):
        _trade(code, "buy", "2026-06-01", 10.0, 500, 101)
        _trade(code, "buy", "2026-06-01", 10.0, 100, 102)
        _trade(code, "buy", "2026-10-21", 10.0, 100, 103)
    _market(monkeypatch, {"2026-06-01": 100.0, "2026-10-21": 100.0}, {})
    calls = []

    def quote(code, day):
        calls.append((code, day))
        if outcome == "failed":
            raise RuntimeError("fixture unavailable")
        return _close(10.0, freshness=1 if outcome == "degraded" else 0)

    monkeypatch.setattr(commands_holdings, "_close_result", quote)
    for _ in range(2):
        calls.clear()
        assert cache.main(["report-trades", "--end", "2026-10-21"]) == 0
        _, lines = _lines(capsys)
        assert len(lines) == 6
        assert sorted(calls) == [
            ("000001", "2026-06-01"),
            ("000001", "2026-10-21"),
            ("600900", "2026-06-01"),
            ("600900", "2026-10-21"),
        ]
        if outcome == "degraded":
            assert all("不作比较" in line for line in lines)
        elif outcome == "failed":
            assert all("fixture unavailable" in line for line in lines)
        else:
            assert all("含分红送转 +0.00%" in line for line in lines)
