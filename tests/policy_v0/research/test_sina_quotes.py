"""Sina quote parsing against real captured responses.

Captures live in ``fixtures/sina`` (hq.sinajs.cn, GBK decoded, stored as UTF-8). Malformed inputs are
minimal, named edits of a captured line, never new quote rows.
"""

from pathlib import Path
from unittest.mock import MagicMock

from a_share_research.policy.v0 import commands_holdings, market_quotes
from policy_v0.helpers import seed_position

CAPTURES = Path(__file__).parents[1] / "fixtures" / "sina"
PREOPEN = (CAPTURES / "20261119-0904-preopen.txt").read_text(encoding="utf-8")
INTRADAY = (CAPTURES / "20261119-0931-intraday.txt").read_text(encoding="utf-8")
CODES = ["600036", "000001", "920189", "600900"]


def _serve(monkeypatch, text):
    request = MagicMock(return_value=MagicMock(text=text))
    monkeypatch.setattr(market_quotes.requests, "get", request)
    return request


def _line(text, code):
    return next(line for line in text.splitlines() if f"{code}=" in line)


def test_intraday_capture_parses_price_timestamp_and_previous_close(monkeypatch):
    request = _serve(monkeypatch, INTRADAY)
    quotes = market_quotes.fetch_current_price_quotes(CODES)
    assert "sh600036,sz000001,bj920189,sh600900" in request.call_args.args[0]
    assert {code: (q.price, q.quote_as_of, q.previous_close, q.source) for code, q in quotes.items()} == {
        "600036": (43.33, "2026-11-19T09:30:58", 43.12, "sina"),
        "000001": (12.21, "2026-11-19T09:31:03", 12.2, "sina"),
        "920189": (14.69, "2026-11-19T09:30:54", 14.87, "sina"),
        "600900": (30.84, "2026-11-19T09:31:00", 30.68, "sina"),
    }


def test_malformed_fields_of_a_captured_line():
    fields = _line(INTRADAY, "600036").split(",")
    for previous_close, expected in (("bad", None), ("", None)):
        variant = ",".join([*fields[:2], previous_close, *fields[3:]])
        assert market_quotes.parse_sina_quote_line(variant, {"600036"})[4] == expected
    assert market_quotes.parse_sina_quote_line(",".join([*fields[:3], "bad", *fields[4:]]), {"600036"}) is None
    # Truncated before the date/time columns: the price survives, the timestamp does not.
    truncated = ",".join(fields[:6]) + '";'
    assert market_quotes.parse_sina_quote_line(truncated, {"600036"}) == ("600036", 43.33, None, None, 43.12)


def test_missing_symbol_and_network_failure_leave_no_quote(monkeypatch):
    only_one = _line(INTRADAY, "600036")
    request = _serve(monkeypatch, only_one)
    quotes = market_quotes.fetch_current_price_quotes(["600036", "000001"])
    assert quotes["600036"].price == 43.33 and quotes["000001"] is None
    request.side_effect = market_quotes.requests.RequestException("network down")
    assert market_quotes.fetch_current_price_quotes(["600036", "000001"]) == dict.fromkeys(["600036", "000001"])
    assert request.call_count == 2


def test_pre_open_zero_price_is_no_quote(monkeypatch):
    # Before the first trade Sina sends 0.000 as the current price next to a real previous close.
    assert _line(PREOPEN, "600036").split(",")[2:4] == ["43.120", "0.000"]
    request = _serve(monkeypatch, PREOPEN)
    assert market_quotes.fetch_current_price_quotes(CODES) == dict.fromkeys(CODES)
    assert "sh600036,sz000001,bj920189,sh600900" in request.call_args.args[0]


def test_check_holdings_before_first_trade_reports_missing_price_not_a_stop(capsys, monkeypatch):
    seed_position("600036", 45.0, 100)  # stop lines 38.25 / 36.00
    _serve(monkeypatch, PREOPEN)
    monkeypatch.setattr(commands_holdings.domain, "cst_today", lambda: "2026-11-19")
    commands_holdings.cmd_check_holdings()
    out = capsys.readouterr().out
    assert "600036           ─ 无实时价格" in out
    assert "无预警；1 只持仓无实时价格，未判定止损" in out
    assert "止损线（" not in out and "所有持仓价格在止损线之上" not in out


def test_empty_batch_does_not_fetch(monkeypatch):
    request = _serve(monkeypatch, PREOPEN)
    assert market_quotes.fetch_current_price_quotes([]) == {}
    request.assert_not_called()
