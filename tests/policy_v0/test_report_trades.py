"""Ledger trades linked to the Research report they relied on."""

import pytest
import requests
from a_share_research.core.market_data import MarketDataResult

from a_share_research.policy.v0 import cache, commands_holdings, db, research_client

REAL_REQUIRE_REPORT = commands_holdings._require_report


@pytest.fixture(autouse=True)
def _research_backs_every_link(monkeypatch):
    # Ledger and comparison tests below are about storage; the gate has its own tests.
    monkeypatch.setattr(commands_holdings, "_require_report", lambda *_: None)


def _events():
    with db.db_session() as conn:
        return conn.execute(
            "SELECT event_type, shares, report_id FROM holding_events ORDER BY id"
        ).fetchall()


def test_add_buy_and_sell_store_the_report_and_default_to_unlinked():
    commands_holdings.cmd_add_holding(["000001", "10.0", "500", "--report-id", "101"])
    commands_holdings.cmd_buy_holding(["000001", "11.0", "100"])
    commands_holdings.cmd_buy_holding(
        ["000001", "11.5", "100", "--fee", "1", "--report-id", "102"]
    )
    commands_holdings.cmd_sell_holding(
        ["000001", "12.0", "200", "--report-id", "103", "--tax", "1"]
    )
    assert _events() == [
        ("buy", 500, 101),
        ("buy", 100, None),
        ("buy", 100, 102),
        ("sell", 200, 103),
    ]


def test_cli_accepts_the_option_behind_the_write_gate():
    assert (
        cache.main(
            [
                "--confirm-write",
                "add-holding",
                "000001",
                "10.0",
                "500",
                "--report-id",
                "7",
            ]
        )
        == 0
    )
    assert _events() == [("buy", 500, 7)]


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5", "１"])
def test_invalid_report_id_writes_nothing(bad, capsys):
    commands_holdings.cmd_add_holding(["000001", "10.0", "500"])
    with pytest.raises(SystemExit):
        commands_holdings.cmd_buy_holding(["000001", "11.0", "100", "--report-id", bad])
    assert "--report-id" in capsys.readouterr().err
    assert _events() == [("buy", 500, None)]


def test_missing_report_id_value_is_rejected():
    commands_holdings.cmd_add_holding(["000001", "10.0", "500"])
    with pytest.raises(SystemExit):
        commands_holdings.cmd_sell_holding(["000001", "12.0", "100", "--report-id"])
    assert _events() == [("buy", 500, None)]


def test_price_only_lot_cannot_claim_a_report(capsys):
    with pytest.raises(SystemExit):
        commands_holdings.cmd_add_holding(["000001", "10.0", "--report-id", "7"])
    assert "必须提供股数" in capsys.readouterr().err


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
    commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-05-27", "--report-id", "101"]
    )
    commands_holdings.cmd_buy_holding(["000001", "11.0", "100", "--date", "2026-06-01"])
    commands_holdings.cmd_sell_holding(
        ["000001", "12.0", "100", "--date", "2026-09-30", "--report-id", "103"]
    )
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
    commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-06-01", "--report-id", "101"]
    )
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
        ("add-holding", ["000001", "10", "100", "--date"]),
        ("buy-holding", ["000001", "10", "100", "--date"]),
        ("sell-holding", ["000001", "10", "100", "--date"]),
        ("close-holding", ["000001", "10"]),
        ("record-dividend", ["000001", "10"]),
        ("corporate-action", ["000001", "0.1", "0"]),
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
    monkeypatch.setattr(commands_holdings, "_require_report", REAL_REQUIRE_REPORT)
    prefix = (
        ["--confirm-write"] if cache.COMMAND_CLASSIFICATION[command] == "W1" else []
    )
    link = (
        ["--report-id", "7"]
        if command in {"add-holding", "buy-holding", "sell-holding"}
        else []
    )
    assert cache.main([*prefix, command, *arguments, day, *link]) == 1
    assert "日期必须为 YYYY-MM-DD" in capsys.readouterr().err
    assert not isolated_cache_database.exists()


@pytest.mark.parametrize("day", ["2024-02-29", "2026-11-05"])
def test_canonical_trade_date_is_preserved_and_included_on_same_day(
    monkeypatch, capsys, day
):
    assert (
        cache.main(
            [
                "--confirm-write",
                "add-holding",
                "000001",
                "10",
                "100",
                "--date",
                day,
                "--report-id",
                "7",
            ]
        )
        == 0
    )
    with db.db_session() as conn:
        assert conn.execute("SELECT buy_date FROM holdings").fetchone() == (day,)
        assert conn.execute(
            "SELECT event_date, report_id FROM holding_events"
        ).fetchone() == (day, 7)
    _market(monkeypatch, {day: 100.0}, {day: _close(10.0)})
    assert cache.main(["report-trades", "--end", day]) == 0
    out, lines = _lines(capsys)
    assert "关联报告的买卖 1 笔，未关联 0 笔" in out
    assert len(lines) == 1 and f"000001 {day} 买入后" in lines[0]
    assert "含分红送转 +0.00%，基准 +0.00%，差 +0.00pp" in lines[0]


def test_a_split_is_not_scored_as_a_price_collapse(monkeypatch, capsys):
    commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-06-01", "--report-id", "101"]
    )
    closes = {"2026-06-01": 100.0, "2026-10-21": 100.0}
    stock = {"2026-06-01": _close(10.0), "2026-10-21": _close(5.0)}
    _market(monkeypatch, closes, stock, [_dividend("2026-07-01", split=1.0)])
    assert cache.main(["report-trades", "--end", "2026-10-21"]) == 0
    assert "含分红送转 +0.00%，基准 +0.00%，差 +0.00pp" in _lines(capsys)[1][0]


def test_distributions_after_the_position_closed_still_count_for_a_sell(
    monkeypatch, capsys
):
    commands_holdings.cmd_add_holding(["000001", "10.0", "500", "--date", "2026-05-01"])
    commands_holdings.cmd_sell_holding(
        ["000001", "10.0", "all", "--date", "2026-06-01", "--report-id", "103"]
    )
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
    commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-06-01", "--report-id", "101"]
    )
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
    commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-06-01", "--report-id", "101"]
    )
    _market(monkeypatch, {"2026-06-01": 100.0, "2026-10-21": 110.0}, stock)
    assert cache.main(["report-trades", "--end", "2026-10-21"]) == 0
    line = _lines(capsys)[1][0]
    assert reason in line and "行情停在 2026-05-29" in line and "差" not in line


def test_no_index_data_exits_cleanly_without_a_traceback(monkeypatch, capsys):
    commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-06-01", "--report-id", "101"]
    )

    def unavailable(*_):
        raise RuntimeError("H00300.CSI 指数行情不可用（EMPTY）")

    monkeypatch.setattr(commands_holdings, "_index_closes", unavailable)
    assert cache.main(["report-trades", "--end", "2026-06-01"]) == 1
    assert "暂无法计算" in capsys.readouterr().err


def test_one_unavailable_stock_does_not_hide_the_others(monkeypatch, capsys):
    commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-06-01", "--report-id", "101"]
    )
    commands_holdings.cmd_add_holding(
        ["600036", "10.0", "500", "--date", "2026-06-01", "--report-id", "102"]
    )
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
    commands_holdings.cmd_add_holding(["000001", "10.0", "500"])

    def forbidden(*_):
        raise AssertionError("no linked trades should mean no market data call")

    monkeypatch.setattr(commands_holdings, "_index_closes", forbidden)
    monkeypatch.setattr(commands_holdings, "_close_result", forbidden)
    monkeypatch.setattr(commands_holdings, "_implemented_dividends", forbidden)
    assert cache.main(["report-trades"]) == 0
    assert "关联报告的买卖 0 笔，未关联 1 笔" in capsys.readouterr().out


def test_old_inserts_without_the_column_still_work_for_rollback():
    commands_holdings.cmd_add_holding(["000001", "10.0", "500"])
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO holding_events
               (holding_id, code, event_type, event_date, shares, price, fees, created_at)
               VALUES (1, '000001', 'buy', '2026-05-28', 100, 11.0, 0, '2026-05-28')"""
        )
        conn.commit()
    assert _events()[-1] == ("buy", 100, None)


def _report(
    report_id,
    code="000001.SZ",
    status="done",
    passed=1,
    created_at="2026-11-04T16:04:57+08:00",
):
    return {
        "id": report_id,
        "run_id": report_id,
        "code": code,
        "name": "平安银行",
        "created_at": created_at,
        "period": "2026Q3",
        "passed": passed,
        "status": status,
    }


@pytest.fixture
def catalog(monkeypatch):
    """The real gate against a stub of GET /api/reports, filtered by code like the server."""
    monkeypatch.setattr(commands_holdings, "_require_report", REAL_REQUIRE_REPORT)
    rows = [
        _report(94),
        _report(92, code="002648.SZ"),
        _report(106, status="failed", passed=0),
        _report(110, created_at="2026-11-05T19:35:32+08:00"),
    ]
    calls = []

    def request_json(path, *, params=None, **_):
        calls.append((path, params))
        return {
            "version": "research-v1",
            "reports": [r for r in rows if r["code"][:6] == params["code"]],
        }

    monkeypatch.setattr(research_client, "request_json", request_json)
    return calls


def _open_position():
    commands_holdings.cmd_add_holding(["000001", "10.0", "500", "--date", "2026-11-01"])


LINKERS = {
    "add": lambda link: commands_holdings.cmd_add_holding(
        ["000001", "10.0", "500", "--date", "2026-11-05", *link]
    ),
    "buy": lambda link: commands_holdings.cmd_buy_holding(
        ["000001", "10.0", "100", "--date", "2026-11-05", *link]
    ),
    "sell": lambda link: commands_holdings.cmd_sell_holding(
        ["000001", "10.0", "100", "--date", "2026-11-05", *link]
    ),
}


def _prepared(command):
    if command != "add":
        _open_position()
    return _events()


@pytest.mark.parametrize("command", LINKERS)
@pytest.mark.parametrize(
    "report_id, reason",
    [
        ("999999", "编号不存在或属于其他股票"),  # unknown id
        ("92", "编号不存在或属于其他股票"),  # exists, but another stock's report
        ("106", "未通过复核"),
    ],
)
def test_unbacked_link_writes_nothing(catalog, capsys, command, report_id, reason):
    before = _prepared(command)
    with pytest.raises(SystemExit):
        LINKERS[command](["--report-id", report_id])
    captured = capsys.readouterr()
    assert reason in captured.err and "交易未记录" in captured.err
    assert _events() == before


@pytest.mark.parametrize("command", LINKERS)
def test_report_newer_than_the_trade_date_is_refused(catalog, capsys, command):
    before = _prepared(command)
    with pytest.raises(SystemExit):
        LINKERS[command](["--date", "2026-11-04", "--report-id", "110"])
    assert "晚于交易日 2026-11-04" in capsys.readouterr().err
    assert _events() == before


@pytest.mark.parametrize("command", LINKERS)
def test_backed_link_is_stored_and_echoes_the_report(catalog, capsys, command):
    _prepared(command)
    LINKERS[command](["--report-id", "94"])
    assert _events()[-1][2] == 94
    assert "已核对报告 #94：平安银行 2026-11-04T16:04" in capsys.readouterr().out
    assert catalog == [("/api/reports", {"code": "000001"})]


def test_report_from_the_trade_day_itself_is_accepted(catalog):
    LINKERS["add"](["--report-id", "110"])
    assert _events()[-1][2] == 110


def test_unlinked_trade_never_calls_research(catalog):
    LINKERS["add"]([])
    assert catalog == []
    assert _events()[-1][2] is None


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("https://user:secret@research"),
        research_client.ResearchNotConfigured("请配置 A_STOCK_RESEARCH_URL"),
        ValueError("Research HTTP 503"),
    ],
)
def test_unavailable_research_writes_nothing_and_hides_transport_text(
    monkeypatch, capsys, failure
):
    monkeypatch.setattr(commands_holdings, "_require_report", REAL_REQUIRE_REPORT)

    def fail(*_, **__):
        raise failure

    monkeypatch.setattr(research_client, "request_json", fail)
    with pytest.raises(SystemExit):
        LINKERS["add"](["--report-id", "94"])
    err = capsys.readouterr().err
    assert "无法向 Research 核对" in err and "交易未记录" in err
    assert "secret" not in err
    assert _events() == []


def test_malformed_research_payload_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(commands_holdings, "_require_report", REAL_REQUIRE_REPORT)
    monkeypatch.setattr(
        research_client, "request_json", lambda *_, **__: {"version": "research-v1"}
    )
    with pytest.raises(SystemExit):
        LINKERS["add"](["--report-id", "94"])
    assert "无法向 Research 核对" in capsys.readouterr().err
    assert _events() == []


def test_gate_runs_before_the_write_transaction(monkeypatch, catalog):
    # A slow Research call must not hold the ledger's write lock.
    def no_session(*_, **__):
        raise AssertionError("gate must refuse before opening the ledger")

    monkeypatch.setattr(db, "db_session", no_session)
    for command in LINKERS:
        with pytest.raises(SystemExit):
            LINKERS[command](["--report-id", "999999"])


@pytest.mark.parametrize("command", LINKERS)
@pytest.mark.parametrize(
    "changes",
    [
        {"code": "002648.SZ"},
        {"code": "000001.invalid"},
        {"created_at": ""},
        {"created_at": "0000invalid"},
        {"created_at": "2026-11-04"},
        {"created_at": "2026-11-04T12:00:00"},
        {"created_at": "2026-02-30T12:00:00+08:00"},
        {"created_at": "9999-12-31T23:00:00-08:00"},
    ],
)
def test_invalid_report_row_cannot_bypass_the_local_gate(
    monkeypatch, capsys, command, changes
):
    before = _prepared(command)
    monkeypatch.setattr(commands_holdings, "_require_report", REAL_REQUIRE_REPORT)
    report = {**_report(94), **changes}
    # Deliberately do not filter: the client must also verify external rows.
    monkeypatch.setattr(
        research_client, "request_json", lambda *_, **__: {"reports": [report]}
    )
    with pytest.raises(SystemExit):
        LINKERS[command](["--report-id", "94"])
    assert "交易未记录" in capsys.readouterr().err
    assert _events() == before


@pytest.mark.parametrize(
    "stamp,accepted",
    [("2026-11-05T15:59:59+00:00", True), ("2026-11-05T16:00:00+00:00", False)],
)
def test_report_date_uses_the_shanghai_calendar(monkeypatch, stamp, accepted):
    monkeypatch.setattr(commands_holdings, "_require_report", REAL_REQUIRE_REPORT)
    monkeypatch.setattr(
        research_client,
        "request_json",
        lambda *_, **__: {"reports": [_report(94, created_at=stamp)]},
    )
    if accepted:
        LINKERS["add"](["--report-id", "94"])
        assert _events()[-1][2] == 94
    else:
        with pytest.raises(SystemExit):
            LINKERS["add"](["--report-id", "94"])
        assert _events() == []


@pytest.mark.parametrize("outcome", ["ok", "degraded", "failed"])
def test_report_trades_reuses_prices_and_failures_only_within_one_command(
    monkeypatch, capsys, outcome
):
    for code in ("000001", "600900"):
        commands_holdings.cmd_add_holding(
            [code, "10.0", "500", "--date", "2026-06-01", "--report-id", "101"]
        )
        commands_holdings.cmd_buy_holding(
            [code, "10.0", "100", "--date", "2026-06-01", "--report-id", "102"]
        )
        commands_holdings.cmd_buy_holding(
            [code, "10.0", "100", "--date", "2026-10-21", "--report-id", "103"]
        )
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
