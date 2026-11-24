"""Holding lifecycle, return, and risk commands."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import logging
from datetime import date, datetime

from a_share_research.core.market_data import MarketDataResult
from a_share_research.core.providers import TushareMarketDataProvider
from a_share_research.core.providers.tushare_fundamentals import TushareFundamentalsProvider

from a_share_research.policy.v0 import (
    boards,
    db,
    domain,
    market_quotes,
    paths,
    risk_budget,
    risk_gates,
    risk_policy,
)
from a_share_research.policy.v0.market_quotes import PriceQuote
from a_share_research.policy.v0.benchmark_input import BENCHMARKS
from a_share_research.policy.v0.position_ledger import (
    LifecycleReturn,
    calculate_lifecycle_return,
)


logger = logging.getLogger(__name__)


def _single_open_holding(conn: sqlite3.Connection, code: str) -> tuple:
    rows = conn.execute(
        """SELECT id, buy_date, framework FROM holdings
           WHERE code=? AND exit_date IS NULL ORDER BY id""",
        (code,),
    ).fetchall()
    if not rows:
        print(f"错误：未找到 {code} 的在仓记录", file=sys.stderr)
        sys.exit(1)
    if len(rows) != 1:
        print(
            f"错误：{code} 有{len(rows)}条在仓记录；结构化状态要求先按单行持仓约定合并",
            file=sys.stderr,
        )
        sys.exit(1)
    return rows[0]


def _load_lifecycle_events(conn: sqlite3.Connection, holding_id: int) -> list[tuple]:
    return conn.execute(
        """SELECT event_type, event_date, shares, price, fees, tax, cash_amount,
                  inferred
           FROM holding_events WHERE holding_id=? ORDER BY event_date, id""",
        (holding_id,),
    ).fetchall()


def _parse_cli_finite_float(
    raw: str,
    label: str,
    *,
    minimum: float | None = None,
    strict_minimum: bool = False,
) -> float:
    """Parse CLI monetary/rate input and reject NaN/Infinity explicitly."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"错误：{label}必须为数字", file=sys.stderr)
        sys.exit(1)
    if not math.isfinite(value):
        print(f"错误：{label}必须是有限数值", file=sys.stderr)
        sys.exit(1)
    if minimum is not None and (
        value <= minimum if strict_minimum else value < minimum
    ):
        comparator = f"大于{minimum}" if strict_minimum else f"不小于{minimum}"
        print(f"错误：{label}必须{comparator}", file=sys.stderr)
        sys.exit(1)
    return value


def _parse_cli_date(raw: str) -> str:
    """Keep ledger dates sortable as YYYY-MM-DD; reject other ISO date forms."""
    try:
        if date.fromisoformat(raw).isoformat() != raw:
            raise ValueError
    except ValueError:
        print("错误：日期必须为 YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    return raw


# Holdings plus same-industry members; at most three Sina requests.
_MONITOR_SYMBOL_LIMIT = 3 * market_quotes.SINA_BATCH_SIZE


def fetch_monitor_price_quotes(
    codes: list[str],
) -> tuple[
    dict[str, PriceQuote | None],
    MarketDataResult[dict[str, str]] | None,
]:
    """Fetch holdings and same-industry constituents in a bounded number of quote requests."""
    if not codes:
        return {}, None
    codes = sorted(set(codes))
    if len(codes) > _MONITOR_SYMBOL_LIMIT:
        return dict.fromkeys(codes), None

    industry_result = TushareFundamentalsProvider().read_cached_industry_map()
    industry_map = (
        industry_result.value
        if industry_result.status == "ok" and industry_result.value is not None
        else {}
    )
    requested_industries = {
        industry_map[code] for code in codes if code in industry_map
    }
    members = {
        code: industry
        for code, industry in industry_map.items()
        if industry in requested_industries
    }
    expanded_codes = sorted(set(codes) | set(members))
    # Beyond the cap, use provider-side sector quotes instead of more requests.
    if len(expanded_codes) > _MONITOR_SYMBOL_LIMIT:
        members = {}
        expanded_codes = codes
    request_codes = expanded_codes
    raw = market_quotes.fetch_sina_batch_quotes(request_codes)

    today = domain.cst_today()
    changes: dict[str, list[float]] = {
        industry: [] for industry in requested_industries
    }
    industry_times: dict[str, list[str]] = {
        industry: [] for industry in requested_industries
    }
    if members:
        for code, industry in sorted(members.items()):
            value = raw.get(code)
            if value is None:
                continue
            price, quote_date, quote_time, previous_close = value
            if (
                quote_date == today
                and quote_time
                and math.isfinite(price)
                and previous_close is not None
                and math.isfinite(previous_close)
                and price > 0
                and previous_close > 0
            ):
                try:
                    datetime.strptime(quote_time, "%H:%M:%S")
                except ValueError:
                    continue
                change = (price - previous_close) / previous_close * 100
                if not math.isfinite(change):
                    continue
                changes[industry].append(change)
                industry_times[industry].append(quote_time)

    industry_quotes: dict[str, tuple[float, str]] = {}
    for industry, values in changes.items():
        expected = sum(value == industry for value in members.values())
        if expected and len(values) / expected >= 0.8:
            industry_quotes[industry] = (
                sum(values) / len(values),
                f"{today}T{max(industry_times[industry])}+08:00",
            )

    result: dict[str, PriceQuote | None] = {}
    for code in codes:
        value = raw.get(code)
        if value is None:
            result[code] = None
            continue
        price, quote_date, quote_time, previous_close = value
        industry_quote = industry_quotes.get(industry_map.get(code, ""))
        result[code] = PriceQuote(
            price=price,
            quote_date=quote_date,
            quote_time=quote_time,
            source="sina",
            previous_close=previous_close,
            industry_change_pct=industry_quote[0] if industry_quote else None,
            industry_source="sina.industry_mean" if industry_quote else None,
            industry_as_of=industry_quote[1] if industry_quote else None,
        )
    return result, industry_result


def _a_share_board(code: str) -> tuple[str, int]:
    """Return (board, minimum/integer-lot reference) for executable sizing."""
    name = boards.board(code)
    return name, 200 if name == "STAR" else 100


def _print_open_holdings(open_rows: list[tuple]) -> None:
    if open_rows:
        unique_open = len(set(r[1] for r in open_rows))
        lot_str = f"，{len(open_rows)} 笔" if len(open_rows) > unique_open else ""
        print(f"\n{'─' * 80}")
        print(f"  在仓持股（{unique_open} 只{lot_str}）")
        print(f"{'─' * 80}")
        print(
            f"  {'股票':<14} {'成本价':>7} {'股数':>6} {'止损15%':>8} {'止损20%':>8} {'得分':>4} {'买入日期':<11} 备注"
        )
        print(f"  {'─' * 74}")
        for r in open_rows:
            _, code, name, cost, shares, buy_date, score, sl15, sl20, notes, _, _ = r
            label = f"{name}({code})" if name else code
            shares_str = str(shares) if shares else "─"
            score_str = str(score) if score else "─"
            notes_str = notes or "─"
            print(
                f"  {label:<14} {cost:>7.3f} {shares_str:>6} {sl15:>8.3f} {sl20:>8.3f} {score_str:>4} {buy_date:<11} {notes_str}"
            )


def _print_closed_holdings(
    closed_rows: list[tuple],
    lifecycle_returns: dict[int, LifecycleReturn],
) -> None:
    if closed_rows:
        print(f"\n{'─' * 80}")
        print(f"  已平仓历史（{len(closed_rows)} 只）")
        print(f"{'─' * 80}")
        print(
            f"  {'股票':<14} {'成本价':>7} {'卖出价':>7} {'盈亏%':>7} {'得分':>4} {'买入':>11} {'卖出':>11} 备注"
        )
        print(f"  {'─' * 74}")
        for r in closed_rows:
            (
                holding_id,
                code,
                name,
                cost,
                shares,
                buy_date,
                score,
                _,
                _,
                notes,
                exit_price,
                exit_date,
            ) = r
            label = f"{name}({code})" if name else code
            score_str = str(score) if score else "─"
            notes_str = notes or "─"
            lifecycle_return = lifecycle_returns.get(holding_id)
            pnl_str = (
                f"{lifecycle_return.total_return_pct:+.1f}%"
                if lifecycle_return is not None
                else "账本缺失"
            )
            print(
                f"  {label:<14} {cost:>7.3f} {exit_price:>7.3f} {pnl_str:>7} {score_str:>4} {buy_date:>11} {exit_date:>11} {notes_str}"
            )

        # 统计：平均盈亏、胜率，帮助验证评分系统有效性
        pnl_list = [item.total_return_pct for item in lifecycle_returns.values()]
        if pnl_list:
            win_rate = round(sum(1 for p in pnl_list if p > 0) / len(pnl_list) * 100)
            avg_pnl = round(sum(pnl_list) / len(pnl_list), 1)
            print(
                f"\n  已平仓统计：胜率 {win_rate}% | 平均盈亏 {avg_pnl:+.1f}% | 有效账本 {len(pnl_list)}/{len(closed_rows)} 笔"
            )
        else:
            print(f"\n  已平仓统计：无可核验事件账本（共 {len(closed_rows)} 笔）")


def cmd_holdings(args: list[str] | None = None) -> None:
    """显示全部持仓，或只显示指定代码的当前/历史持仓。"""
    args = args or []
    compact = "--compact" in args
    json_output = "--json" in args
    active_only = "--active-only" in args
    positionals = [arg for arg in args if not arg.startswith("--")]
    if len(positionals) > 1:
        print("错误：holdings 最多接受一个股票代码", file=sys.stderr)
        sys.exit(1)
    code = positionals[0] if positionals else None
    clauses = []
    params = []
    if code:
        clauses.append("code=?")
        params.append(code)
    if active_only:
        clauses.append("exit_date IS NULL")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    try:
        with db.read_only_db_session() as conn:
            if compact or json_output:
                rows = conn.execute(
                    """SELECT id, code, name, cost_price, shares, buy_date, buy_score,
                              stop_loss_15, stop_loss_20, exit_price, exit_date,
                              framework, initial_shares, main_entry_date,
                              reference_cost, framework_confident
                       FROM holdings"""
                    + where
                    + " ORDER BY exit_date IS NULL DESC, buy_date DESC",
                    tuple(params),
                ).fetchall()
                records = [
                    {
                        "id": row[0],
                        "code": row[1],
                        "name": row[2],
                        "status": "active" if row[10] is None else "closed",
                        "shares": row[4],
                        "cost_price": row[3],
                        "reference_cost": row[14],
                        "buy_date": row[5],
                        "buy_score": row[6],
                        "framework": row[11],
                        "framework_confident": bool(row[15]),
                        "initial_shares": row[12],
                        "main_entry_date": row[13],
                        "stop_loss_15": row[7],
                        "stop_loss_20": row[8],
                        "exit_price": row[9],
                        "exit_date": row[10],
                    }
                    for row in rows
                ]
                if json_output:
                    print(
                        json.dumps(records, ensure_ascii=False, separators=(",", ":"))
                    )
                elif records:
                    for record in records:
                        print(
                            f"{record['status']} {record['code']}({record['name'] or '─'}) "
                            f"shares={record['shares'] or '─'} framework={record['framework'] or '?'} "
                            f"confident={int(record['framework_confident'])} "
                            f"reference_cost={record['reference_cost'] or '─'} "
                            f"stop1={record['stop_loss_15'] or '─'} "
                            f"stop2={record['stop_loss_20'] or '─'}"
                        )
                else:
                    print(f"NOT_HELD {code}" if code else "暂无持仓记录")
                return
            rows = conn.execute(
                """SELECT id, code, name, cost_price, shares, buy_date, buy_score,
                          stop_loss_15, stop_loss_20, notes, exit_price, exit_date
                   FROM holdings"""
                + where
                + " ORDER BY exit_date IS NULL DESC, buy_date DESC",
                tuple(params),
            ).fetchall()
            lifecycle_returns: dict[int, LifecycleReturn] = {}
            for row in rows:
                holding_id, exit_date = row[0], row[11]
                if exit_date is None:
                    continue
                try:
                    lifecycle_returns[holding_id] = calculate_lifecycle_return(
                        _load_lifecycle_events(conn, holding_id),
                        0,
                        end_date=exit_date,
                    )
                except ValueError:
                    continue
    except sqlite3.OperationalError as exc:
        print(f"HOLDINGS_UNAVAILABLE {exc}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print(f"NOT_HELD {code}" if code else "暂无持仓记录")
        return

    open_rows = [r for r in rows if r[11] is None]  # exit_date IS NULL
    closed_rows = [r for r in rows if r[11] is not None]

    _print_open_holdings(open_rows)
    _print_closed_holdings(closed_rows, lifecycle_returns)


def unrecorded_dividends(
    events: list[tuple], dividends: list[dict], today: str
) -> list[dict]:
    """Implemented dividends the ledger position was entitled to but never recorded.

    ``events`` use the lifecycle query order; ``dividends`` carry ISO
    ``record_date``/``ex_date``/``pay_date``, pre-tax ``cash_div`` and
    ``split_ratio``. Cash counts as recorded with a ledger dividend, and a bonus
    or split with a positive share adjustment, dated from the record date through
    the later of ex/pay date. Entitlement is the position at the record-date close.
    """
    missing = []
    for item in dividends:
        record = item["record_date"]
        if not events or record > today or record < events[0][1]:
            continue
        held = sum(
            (-1 if event_type == "sell" else 1) * (shares or 0)
            for event_type, day, shares, *_ in events
            if day <= record and event_type in ("buy", "sell", "adjustment")
        )
        window_end = max(item["ex_date"] or record, item["pay_date"] or record)
        in_window = [
            (event_type, shares)
            for event_type, day, shares, *_ in events
            if record <= day <= window_end
        ]
        cash_found = bool(item["cash_div"]) and any(
            event_type == "dividend" for event_type, _ in in_window
        )
        split_found = bool(item["split_ratio"]) and any(
            event_type == "adjustment" and (shares or 0) > 0
            for event_type, shares in in_window
        )
        cash_recorded = cash_found or not item["cash_div"]
        split_recorded = split_found or not item["split_ratio"]
        if held <= 0 or cash_recorded and split_recorded:
            continue
        missing.append(
            {
                **item,
                "shares": held,
                "cash": item["cash_div"] * held,
                "partially_recorded": cash_found or split_found,
            }
        )
    return missing


def _implemented_dividends(code: str) -> list[dict]:
    from a_share_research.core.providers import TushareDividendProvider

    result = TushareDividendProvider(
        token=paths.tushare_token()
    ).fetch_dividend_history(code)
    frame = result.value
    if frame is None:
        raise RuntimeError(
            f"TuShare 分红数据不可用（{getattr(result, 'status', 'error')}）"
        )
    from a_share_research.policy.v0.dividend_input import implemented_rows

    return implemented_rows(frame.to_dict("records"))


def scan_unrecorded_dividends(
    code: str | None = None,
) -> tuple[int, list[tuple], list[tuple]]:
    """Return (open holdings, (code, name, shares, item) findings, (code, name, error))."""
    with db.read_only_db_session() as conn:
        holdings = conn.execute(
            """SELECT id, code, name, shares FROM holdings
               WHERE exit_date IS NULL AND (? IS NULL OR code=?) ORDER BY code""",
            (code, code),
        ).fetchall()
        events = {row[0]: _load_lifecycle_events(conn, row[0]) for row in holdings}
    today = domain.cst_today()
    findings, failures = [], []
    for holding_id, holding_code, name, current_shares in holdings:
        try:
            dividends = _implemented_dividends(holding_code)
        except Exception as exc:  # one provider failure must not hide other codes
            failures.append((holding_code, name, exc))
            continue
        findings += [
            (holding_code, name, current_shares, item)
            for item in unrecorded_dividends(events[holding_id], dividends, today)
        ]
    return len(holdings), findings, failures


def cmd_dividend_check(args: list[str]) -> None:
    """Compare TuShare implemented dividends with ledger events for open holdings."""
    count, findings, failures = scan_unrecorded_dividends(args[0] if args else None)
    if not count:
        print("暂无在仓持仓")
        return
    for code, name, exc in failures:
        print(f"{code} {name or ''}：分红数据获取失败，未核对（{exc}）")
    for code, name, current_shares, item in findings:
        print(
            f"{code} {name or ''}：登记日{item['record_date']} 每股{item['cash_div']:g}元"
            f" 转增{item['split_ratio']:g} | 登记日账本持股{item['shares']}股 → "
            f"应收{item['cash']:.2f}元；账本无对应分红记录"
        )
        if item["partially_recorded"]:
            # A full corporate action would re-record the half already booked.
            print(
                "  现金与转增只记了一部分：不给自动记账参数，按券商流水人工核对未记部分"
            )
        elif current_shares == item["shares"]:
            print(
                f"  核对到账后经账本工具提交公司行动：每股现金{item['cash_div']:g}元"
                f" 转增{item['split_ratio']:g} 登记日{item['record_date']}"
            )
        elif not item["split_ratio"]:
            # A per-share corporate action uses today's shares; record the entitled total instead.
            print(
                f"  登记日后股数已变（现{current_shares}股）：核对到账后经账本工具提交分红"
                f" {item['cash']:.2f}元（登记日{item['record_date']}）；规则参考成本另行人工核对"
            )
        else:
            print(
                f"  登记日后股数已变（现{current_shares}股）且含转增："
                "不给自动记账参数，按券商流水人工核对"
            )
    if not findings and not failures:
        print(f"已核对{count}只在仓持仓：未发现未记账分红")
    if failures:
        sys.exit(1)


STOP_NEAR_PCT = 0.03  # warn once a price is within 3% above the first stop
_STOP_LEVELS = {"near": 1, "stop1": 2, "stop2": 3}


def stop_level(price: float, stop1: float | None, stop2: float | None) -> str | None:
    """Most severe stop level reached by the price, or None."""
    if stop2 and price <= stop2:
        return "stop2"
    if stop1 and price <= stop1:
        return "stop1"
    if stop1 and price <= stop1 * (1 + STOP_NEAR_PCT):
        return "near"
    return None


def new_stop_alerts(
    levels: dict[str, str], state: dict, today: str
) -> tuple[list, dict]:
    """Codes whose level worsened since today's last alert, plus the next state."""
    seen = dict(state.get("levels", {})) if state.get("date") == today else {}
    alerts = [
        (code, level)
        for code, level in levels.items()
        if _STOP_LEVELS[level] > _STOP_LEVELS.get(seen.get(code), 0)
    ]
    seen.update(dict(alerts))
    return alerts, {"date": today, "levels": seen}


def cmd_stop_alert(args: list[str]) -> None:
    """Intraday stop-line alert for push delivery; silent outside sessions or without news."""
    if args:
        print("错误：stop-alert 不接受参数", file=sys.stderr)
        sys.exit(1)
    now = domain.cst_now()
    if not domain.is_a_share_trading_hours(now):
        return
    with db.read_only_db_session() as conn:
        rows = conn.execute(
            """SELECT code, name, stop_loss_15, stop_loss_20 FROM holdings
               WHERE exit_date IS NULL ORDER BY code"""
        ).fetchall()
    if not rows:
        return
    today = now.date().isoformat()
    quotes = market_quotes.fetch_current_price_quotes([row[0] for row in rows])
    fresh = {}
    for code, *_ in rows:
        quote = quotes.get(code)
        stamp = quote and risk_gates._parse_time(quote.quote_as_of)
        # Only same-day quotes at most 15 minutes old may raise a live-looking alert.
        if (
            risk_budget.valid_quote(quote, today)
            and stamp
            and abs((now - stamp).total_seconds()) <= 900
        ):
            fresh[code] = quote
    if not fresh:
        print("止损预警：本时段所有持仓行情无效或过期，未能检查", file=sys.stderr)
        sys.exit(1)
    levels = {
        code: level
        for code, _name, stop1, stop2 in rows
        if code in fresh and (level := stop_level(fresh[code].price, stop1, stop2))
    }
    state_path = paths.state_dir() / "stop-alert-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    alerts, state = new_stop_alerts(levels, state, today)
    if not alerts:
        return
    names = {row[0]: row for row in rows}
    for code, level in alerts:
        _, name, stop1, stop2 = names[code]
        quote = fresh[code]
        where = f"（{quote.quote_time}，{quote.source}）"
        if level == "near":
            print(
                f"⚠️ 接近止损 {name}({code}) 现价{quote.price:.2f}，距第一档{stop1:.3f}"
                f"仅{(quote.price - stop1) / quote.price:+.1%}{where}"
            )
        else:
            stop = stop2 if level == "stop2" else stop1
            label = "第二档" if level == "stop2" else "第一档"
            print(
                f"🚨 止损预警 {name}({code}) 现价{quote.price:.2f} 已跌破{label}止损{stop:.3f}{where}"
            )
    print("按 a-share 的生效政策复核止损与 L3；预警不等于自动卖出。")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_path)


def _index_closes(ts_code: str, start: str, end: str) -> dict[str, float]:
    result = TushareMarketDataProvider(token=paths.tushare_token()).fetch_index_bars(
        ts_code, start, end
    )
    frame = result.value
    if result.status == "failed" or frame is None or frame.empty:
        raise RuntimeError(f"{ts_code} 指数行情不可用（{result.error_code}）")
    return dict(zip(frame["date"], frame["close"].astype(float)))


def _close_result(code: str, day: str) -> MarketDataResult[float]:
    result = TushareMarketDataProvider(token=paths.tushare_token()).fetch_close_price(
        code, day
    )
    if result.status == "failed" or result.value is None:
        raise RuntimeError(f"{code} 在 {day} 无可用收盘价（{result.error_code}）")
    return result


def _close_on(code: str, day: str) -> float:
    result = _close_result(code, day)
    if result.status == "degraded":
        print(
            f"价格参考：{code} {day} 使用 {result.source_as_of} 收盘价，"
            f"相差 {result.freshness_days} 天（{result.source}；{result.fallback_reason}）",
            file=sys.stderr,
        )
    return float(result.value)


def cmd_benchmark_report(args: list[str]) -> None:
    """Compare ledger cash flows with the same flows into total-return indices."""
    if args and (len(args) != 2 or args[0] != "--end"):
        print("错误：用法 benchmark-report [--end YYYY-MM-DD]", file=sys.stderr)
        sys.exit(1)
    end = _parse_cli_date(args[1] if args else domain.cst_today())
    with db.read_only_db_session() as conn:
        events = conn.execute(
            """SELECT code, event_type, event_date, shares, price, fees, tax,
                      cash_amount, inferred
               FROM holding_events WHERE event_date <= ? ORDER BY event_date, id""",
            (end,),
        ).fetchall()
    if not events:
        print("错误：账本没有交易事件", file=sys.stderr)
        sys.exit(1)
    first = events[0][2]
    benchmarks = {code: _index_closes(code, first, end) for code, _ in BENCHMARKS}
    from a_share_research.policy.v0.benchmark_input import calculate, prepare

    prepared = prepare(events, benchmarks)
    prices = {code: _close_on(code, prepared['valuation_day'])
              for code, shares in prepared['open_shares'].items() if shares}
    print(calculate(events, benchmarks, prices)['stdout'], end='')


def total_return(
    start_close: float, end_close: float, dividends: list[dict], start: str, end: str
) -> float | None:
    """Close-to-close return with cash and share distributions, or None if undatable.

    Only distributions going ex after ``start`` and on or before ``end`` count: the
    start close already reflects earlier ones. Cash is pre-tax and not reinvested.
    """
    # A record-date close still carries the entitlement; an unknown ex-date may follow it.
    # A zero-length window has no later distribution to account for.
    if start < end and any(
        not item["ex_date"] and start <= item["record_date"] <= end
        for item in dividends
    ):
        return None
    shares, cash = 1.0, 0.0
    for item in sorted(
        (i for i in dividends if i["ex_date"] and start < i["ex_date"] <= end),
        key=lambda i: i["ex_date"],
    ):
        cash += shares * item["cash_div"]
        shares *= 1 + item["split_ratio"]
    return (shares * end_close + cash) / start_close - 1


def _trade_comparison(
    start: str,
    end: str,
    index_change: float,
    dividends: list[dict],
    start_close: MarketDataResult[float],
    end_close: MarketDataResult[float],
) -> str:
    """One trade against the index, both from the same close to the same close."""
    for closing, day, label in ((start_close, start, "起点"), (end_close, end, "期末")):
        if closing.status != "ok":
            return f"{label} {day} 没有当日收盘（行情停在 {closing.source_as_of}），不作比较"
    change = total_return(
        float(start_close.value), float(end_close.value), dividends, start, end
    )
    if change is None:
        return "期间有分红或送转缺除权日，不作比较"
    return (
        f"含分红送转 {change:+.2%}，基准 {index_change:+.2%}，"
        f"差 {(change - index_change) * 100:+.2f}pp"
    )


def cmd_report_trades(args: list[str]) -> None:
    """Show how trades linked to a Research report fared against the index."""
    if args and (len(args) != 2 or args[0] != "--end"):
        print("错误：用法 report-trades [--end YYYY-MM-DD]", file=sys.stderr)
        sys.exit(1)
    end = _parse_cli_date(args[1] if args else domain.cst_today())
    with db.read_only_db_session() as conn:
        linked = conn.execute(
            """SELECT report_id, code, event_type, event_date, shares, price, realized_pnl
               FROM holding_events
               WHERE report_id IS NOT NULL AND event_date <= ?
               ORDER BY report_id, event_date, id""",
            (end,),
        ).fetchall()
        unlinked = conn.execute(
            """SELECT COUNT(*) FROM holding_events
               WHERE report_id IS NULL AND event_type IN ('buy', 'sell')
                 AND event_date <= ?""",
            (end,),
        ).fetchone()[0]
    print(f"关联报告的买卖 {len(linked)} 笔，未关联 {unlinked} 笔")
    if not linked:
        return
    index_code, index_label = BENCHMARKS[0]
    try:
        closes = _index_closes(index_code, min(row[3] for row in linked), end)
    except RuntimeError as exc:
        print(f"错误：暂无法计算：{exc}；当天收盘未更新时稍后再查", file=sys.stderr)
        sys.exit(1)
    valuation_day = max(closes)
    print(
        f"以下均从交易日（非交易日顺延）收盘算到 {valuation_day} 收盘，基准为{index_label}"
    )
    dividends: dict[str, list[dict]] = {}
    prices: dict[tuple[str, str], MarketDataResult[float] | RuntimeError] = {}

    def close(code: str, day: str) -> MarketDataResult[float]:
        key = (code, day)
        if key not in prices:
            try:
                prices[key] = _close_result(code, day)
            except RuntimeError as exc:
                prices[key] = exc
        result = prices[key]
        if isinstance(result, RuntimeError):
            raise result
        return result

    for report_id, code, event_type, day, shares, price, realized in linked:
        label = "买入后" if event_type == "buy" else "卖出后"
        line = f"  报告 #{report_id} {code} {day} {label} {shares}股 @ {price:.3f}："
        start_days = [item for item in closes if item >= day]
        if not start_days:
            print(line + "晚于最新指数收盘，暂不计算")
            continue
        start = min(start_days)
        index_change = closes[valuation_day] / closes[start] - 1
        try:
            if code not in dividends:
                dividends[code] = _implemented_dividends(code)
            line += _trade_comparison(
                start,
                valuation_day,
                index_change,
                dividends[code],
                close(code, start),
                close(code, valuation_day),
            )
        except RuntimeError as exc:
            line += f"暂不计算（{exc}）"
        if realized is not None:
            line += f"，已实现 {realized:+,.0f} 元"
        print(line)
    print(
        "  买入看差是否为正；卖出差为负说明卖出后跑输基准。笔数少时只说明这些交易之后的相对表现"
    )


def cmd_retro_pending(args: list[str] | None = None) -> None:
    """显示已平仓但尚未复盘的记录。用法：retro-pending"""
    with db.read_only_db_session() as conn:
        rows = conn.execute(
            """SELECT h.id, h.code, h.name, h.exit_date, h.buy_score
               FROM holdings h
               LEFT JOIN retro_notes r ON h.id = r.holding_id
               WHERE h.exit_date IS NOT NULL AND r.id IS NULL
               ORDER BY h.exit_date DESC"""
        ).fetchall()

        rendered_rows: list[
            tuple[str, str | None, str, int | None, LifecycleReturn | None, str | None]
        ] = []
        for holding_id, code, name, exit_date, buy_score in rows:
            try:
                lifecycle_return = calculate_lifecycle_return(
                    _load_lifecycle_events(conn, holding_id),
                    0,
                    end_date=exit_date,
                )
                rendered_rows.append(
                    (code, name, exit_date, buy_score, lifecycle_return, None)
                )
            except ValueError as exc:
                rendered_rows.append((code, name, exit_date, buy_score, None, str(exc)))

    if not rendered_rows:
        print("无待复盘记录")
        return

    for code, name, exit_date, buy_score, lifecycle_return, error in rendered_rows:
        name_str = name or "─"
        score_str = str(buy_score) if buy_score is not None else "─"
        if lifecycle_return is None:
            print(
                f"{code} {name_str} 平仓日期:{exit_date} 账本不完整:{error} 买入得分:{score_str}"
            )
        else:
            print(
                f"{code} {name_str} 平仓日期:{exit_date} "
                f"回报:{lifecycle_return.total_return_pct:+.1f}% 买入得分:{score_str}"
            )


def cmd_retro_stats(args: list[str]) -> None:
    """显示复盘统计。用法：retro-stats [框架名]"""
    framework_filter = args[0] if args else None
    params: tuple[str, ...] = ()
    where = ""
    if framework_filter:
        where = "WHERE framework=?"
        params = (framework_filter,)

    with db.db_session() as conn:
        rows = conn.execute(
            f"""SELECT actual_return_pct, error_tags
                FROM retro_notes
                {where}""",
            params,
        ).fetchall()

    if not rows:
        print("暂无复盘记录")
        return

    returns = [r[0] for r in rows if r[0] is not None]
    total = len(rows)
    win_rate = (
        round(sum(1 for value in returns if value > 0) / len(returns) * 100, 1)
        if returns
        else 0.0
    )
    avg_return = round(sum(returns) / len(returns), 1) if returns else 0.0

    tag_counts: dict[str, int] = {}
    for _, tags in rows:
        if not tags:
            continue
        for tag in tags.split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    title = framework_filter or "全部"
    print(f"=== 复盘统计 [{title}] ===")
    print(f"案例数: {total}  胜率: {win_rate}%  平均回报: {avg_return:+.1f}%")
    print("错误标签分布:")
    for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0])):
        warning = " ★ 建议复查框架规则" if count >= 3 else ""
        print(f"  {tag}: {count}次{warning}")


def cmd_retro_outliers(args: list[str]) -> None:
    """显示亏损超过阈值且尚未复盘的记录。用法：retro-outliers [--loss N]"""
    threshold = -10.0
    i = 0
    while i < len(args):
        if args[i] == "--loss":
            if i + 1 >= len(args):
                print("错误：--loss 需要参数", file=sys.stderr)
                sys.exit(1)
            threshold = -abs(_parse_cli_finite_float(args[i + 1], "--loss"))
            i += 2
        else:
            print(f"错误：未知参数 {args[i]}", file=sys.stderr)
            sys.exit(1)

    with db.read_only_db_session() as conn:
        rows = conn.execute(
            """SELECT h.id, h.code, h.name, h.exit_date, h.buy_score
               FROM holdings h
               LEFT JOIN retro_notes r ON h.id = r.holding_id
               WHERE h.exit_date IS NOT NULL
                 AND r.id IS NULL
               ORDER BY h.exit_date DESC""",
        ).fetchall()
        outliers: list[tuple[str, str | None, str, LifecycleReturn]] = []
        for holding_id, code, name, exit_date, _buy_score in rows:
            try:
                lifecycle_return = calculate_lifecycle_return(
                    _load_lifecycle_events(conn, holding_id),
                    0,
                    end_date=exit_date,
                )
            except ValueError:
                continue
            if lifecycle_return.total_return_pct < threshold:
                outliers.append((code, name, exit_date, lifecycle_return))

    if not outliers:
        print(f"无亏损超过 {threshold:.0f}% 的未复盘记录")
        return

    print(f"亏损超过 {threshold:.0f}% 的未复盘记录:")
    for code, name, exit_date, lifecycle_return in sorted(
        outliers,
        key=lambda item: item[3].total_return_pct,
    ):
        name_str = name or "─"
        print(
            f"{code} {name_str} 平仓日期:{exit_date} "
            f"实际回报:{lifecycle_return.total_return_pct:+.1f}%"
        )


def cmd_position_return(args: list[str]) -> None:
    """Calculate cash-flow return for the current, or latest closed, position lifecycle."""
    if not args:
        print("错误：需要参数 <代码> [当前价]", file=sys.stderr)
        sys.exit(1)
    code = args[0]
    current_price = None
    if len(args) > 1:
        current_price = _parse_cli_finite_float(
            args[1],
            "当前价",
            minimum=0,
            strict_minimum=True,
        )
    with db.db_session() as conn:
        open_rows = conn.execute(
            """SELECT id, shares, exit_date FROM holdings
               WHERE code=? AND exit_date IS NULL ORDER BY id""",
            (code,),
        ).fetchall()
        if len(open_rows) > 1:
            print(
                f"错误：{code} 有{len(open_rows)}条在仓记录；"
                "无法确定应计算哪个持仓生命周期",
                file=sys.stderr,
            )
            sys.exit(1)
        lifecycle = (
            open_rows[0]
            if open_rows
            else conn.execute(
                """SELECT id, shares, exit_date FROM holdings
               WHERE code=? AND exit_date IS NOT NULL
               ORDER BY exit_date DESC, id DESC LIMIT 1""",
                (code,),
            ).fetchone()
        )
        if lifecycle is None:
            print(f"错误：未找到 {code} 的持仓记录", file=sys.stderr)
            sys.exit(1)
        holding_id, open_shares, exit_date = lifecycle
        events = _load_lifecycle_events(conn, holding_id)
    if not events:
        print(
            f"错误：{code} 当前持仓生命周期无交易事件；旧持仓须先迁移账本",
            file=sys.stderr,
        )
        sys.exit(1)
    remaining_shares = open_shares if exit_date is None else 0
    if remaining_shares and current_price is None:
        quote = market_quotes.fetch_current_price_quotes([code])[code]
        current_price = quote.price if quote else None
    last_day = domain.cst_today() if remaining_shares else (exit_date or events[-1][1])
    try:
        lifecycle_return = calculate_lifecycle_return(
            events,
            remaining_shares,
            end_date=last_day,
            current_price=current_price,
        )
    except ValueError as exc:
        print(f"错误：{code} {exc}", file=sys.stderr)
        sys.exit(1)
    print(
        f"{code} 事件账本总回报：{lifecycle_return.total_return_pct:+.2f}% | "
        f"盈亏:{lifecycle_return.pnl:+.2f}元 | 投入:{lifecycle_return.invested:.2f} "
        f"卖出回款:{lifecycle_return.sale_cash:.2f} 分红:{lifecycle_return.dividends:.2f} "
        f"在仓市值:{lifecycle_return.market_value:.2f} | 持有:{lifecycle_return.holding_days}天"
    )
    if lifecycle_return.contains_inferred:
        print(
            "  ⚠️ 含旧持仓推断事件：历史费用、分红或部分交易可能缺失，须与券商流水核对"
        )


def calculate_position_risk(
    shares: int | None,
    current_price: float | None,
    stop_loss_20: float | None,
    portfolio_value: float | None,
    reference_cost: float | None = None,
) -> dict[str, float | None]:
    """Validate inputs once, preserving the existing second-stop risk formula."""
    shares = shares if risk_budget.valid_shares(shares) else None
    current_price = (
        current_price if risk_budget.positive_number(current_price) else None
    )
    stop_loss_20 = stop_loss_20 if risk_budget.positive_number(stop_loss_20) else None
    portfolio_value = (
        portfolio_value if risk_budget.positive_number(portfolio_value) else None
    )
    market_value = (
        current_price * shares
        if current_price is not None and shares is not None
        else None
    )
    stop_risk = (
        max(current_price - stop_loss_20, 0) * shares
        if current_price is not None and stop_loss_20 is not None and shares is not None
        else None
    )
    if not risk_budget.finite_number(market_value):
        market_value = None
        stop_risk = None
    # The part of stop_risk above the rule reference cost is unrealized gain that
    # would be given back; the rest is principal between that cost and the stop.
    unrealized_gain = (
        max(current_price - max(reference_cost, stop_loss_20), 0) * shares
        if stop_risk is not None and risk_budget.positive_number(reference_cost)
        else None
    )
    result = {
        "market_value": market_value,
        "portfolio_weight_pct": (
            market_value / portfolio_value * 100
            if market_value is not None and portfolio_value is not None
            else None
        ),
        "stop_risk": stop_risk,
        "stop_risk_pct": (
            stop_risk / portfolio_value * 100
            if stop_risk is not None and portfolio_value is not None
            else None
        ),
        "stop_risk_unrealized_gain": unrealized_gain,
    }
    return {
        key: value if risk_budget.finite_number(value) else None
        for key, value in result.items()
    }


def _parse_portfolio_risk_args(
    args: list[str] | None, *, allow_missing_scope: bool = False
) -> risk_policy.RiskParameters:
    try:
        return risk_policy.parse_risk_args(
            args, allow_missing_scope=allow_missing_scope
        )
    except risk_policy.RiskPolicyError as exc:
        print(f"风险参数不可用：{exc}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_portfolio_risk(args: list[str] | None = None) -> None:
    """Market-value weighted portfolio view with explicit stop-loss risk budget."""
    parameters = _parse_portfolio_risk_args(args)
    portfolio_value, limits = parameters.portfolio_value, parameters.risk_policy
    account_value_supplied = portfolio_value is not None
    try:
        with db.read_only_db_session() as conn:
            holdings = conn.execute(
                """SELECT h.code, h.name, h.cost_price, h.shares, h.buy_date, h.buy_score,
                          f.industry, h.framework, h.framework_confident, h.stop_loss_20,
                          h.reference_cost
                   FROM holdings h LEFT JOIN stock_fundamentals f ON h.code = f.code
                   WHERE h.exit_date IS NULL ORDER BY h.buy_date DESC"""
            ).fetchall()
    except (OSError, sqlite3.Error):
        print("组合风险不可用：状态数据库缺失或 schema 不完整", file=sys.stderr)
        raise SystemExit(1) from None
    if not holdings:
        print("暂无持仓")
        return

    print(f"\n{'─' * 104}")
    print(f"  组合风险视图  现价查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'─' * 104}")

    codes = [h[0] for h in holdings]
    quotes = market_quotes.fetch_current_price_quotes(codes)
    today_str = domain.cst_today()
    valued_rows = []
    reference_costs: dict[str, float | None] = {}
    for (
        code,
        name,
        cost,
        shares,
        buy_date,
        score,
        industry,
        holding_framework,
        framework_confident,
        sl20,
        reference_cost,
    ) in holdings:
        if holding_framework:
            fw, confident = holding_framework, bool(framework_confident)
        else:
            fw, confident = domain.infer_framework(industry)
        quote = quotes.get(code)
        curr = quote.price if risk_budget.valid_quote(quote, today_str) else None
        if codes.count(code) > 1:
            curr = None  # Conflicting lifecycle rows are not independent positions.
        market_value = calculate_position_risk(shares, curr, sl20, None)["market_value"]
        valued_rows.append(
            (code, name, cost, shares, score, fw, confident, sl20, curr, market_value)
        )
        reference_costs[code] = reference_cost

    risks = [
        {
            "code": row[0],
            **calculate_position_risk(
                row[3], row[8], row[7], portfolio_value, reference_costs[row[0]]
            ),
        }
        for row in valued_rows
    ]
    budget = risk_budget.assess(
        risks,
        portfolio_value,
        limits,
        account_scope=parameters.account_scope,
        denominator_source=parameters.denominator_source() or "cli",
    )
    stock_market_value = sum(row[9] for row in valued_rows if row[9] is not None)
    valued_count = sum(row[9] is not None for row in valued_rows)
    if portfolio_value is None:
        portfolio_value = stock_market_value
        denominator_label = "已取价股票市值（未提供现金/其他资产）"
    else:
        denominator_label = "用户提供的可投资组合总资产"
    if portfolio_value <= 0:
        print("  无法计算：所有持仓均缺少股数或实时价格；风险不可完整判定")
        return

    print(f"  风险分母：{denominator_label} = {portfolio_value:.2f}元")
    evidence = parameters.denominator_evidence()
    if parameters.off_account_strategy_cash:
        components = evidence["denominator_components"]
        print(
            f"  分母构成：账户资产 {components['account_value']:.2f}元 + "
            f"政策文件确认的账户外策略现金 {components['off_account_strategy_cash']:.2f}元"
        )
    print(
        f"  分母估值时间：{evidence['portfolio_value_as_of'] or 'unknown'}；"
        f"账户范围：{evidence['account_scope'] or 'unknown'}；"
        f"denominator_freshness_status={evidence['denominator_freshness_status']}；"
        f"denominator_scope_status={evidence['denominator_scope_status']}"
    )
    if evidence["denominator_requires_review"]:
        print("  分母时点/范围不完整：新增风险须复核，不以报告采集时间补造估值时间")
    print(
        f"  {'股票':<16} {'框架':>5} {'股数':>6} {'当前价':>7} {'市值权重':>8} "
        f"{'浮盈%':>7} {'止损风险':>9} {'风险贡献':>8} {'状态':>5}"
    )
    print(f"  {'─' * 100}")

    framework_values: dict[str, float] = {}
    for (
        code,
        name,
        cost,
        shares,
        score,
        fw,
        confident,
        sl20,
        curr,
        market_value,
    ) in valued_rows:
        fw_display = fw if confident else f"{fw}?"
        label = f"{name}({code})" if name else code
        pnl_str = (
            f"{(curr - cost) / cost * 100:+.1f}%"
            if curr is not None and risk_budget.positive_number(cost)
            else "─"
        )
        curr_str = f"{curr:.2f}" if curr else "─"
        shares_str = str(shares) if shares is not None else "─"
        if market_value is None:
            missing_status = (
                "已破线/缺数据"
                if curr is not None
                and risk_budget.positive_number(sl20)
                and curr <= sl20
                else "缺数据"
            )
            print(
                f"  {label:<16} {fw_display:>5} {shares_str:>6} {curr_str:>7} "
                f"{'─':>8} {pnl_str:>7} {'─':>9} {'─':>8} {missing_status:>5}"
            )
            continue
        risk = calculate_position_risk(shares, curr, sl20, portfolio_value)
        weight_pct = risk["portfolio_weight_pct"]
        stop_risk = risk["stop_risk"]
        risk_pct = risk["stop_risk_pct"]
        framework_values[fw] = framework_values.get(fw, 0.0) + market_value
        risk_str = f"{stop_risk:.0f}元" if stop_risk is not None else "─"
        risk_pct_str = f"{risk_pct:.2f}%" if risk_pct is not None else "─"
        status = (
            "已破线"
            if risk_budget.positive_number(sl20) and curr <= sl20
            else "缺数据"
            if stop_risk is None or risk_pct is None
            else "临时口径"
            if not account_value_supplied
            else "超预算"
            if any(item["code"] == code for item in budget["breaches"])
            else "正常"
        )
        weight_text = f"{weight_pct:.1f}%" if weight_pct is not None else "─"
        print(
            f"  {label:<16} {fw_display:>5} {shares_str:>6} {curr_str:>7} "
            f"{weight_text:>8} {pnl_str:>7} {risk_str:>9} {risk_pct_str:>8} {status:>5}"
        )

    if account_value_supplied:
        weight_label = (
            "股票总仓位" if valued_count == len(holdings) else "已取价股票仓位（下限）"
        )
        risk_summary = (
            f"{weight_label}：{stock_market_value / portfolio_value * 100:.1f}%"
        )
        risk_pct_label = "占组合总资产"
    else:
        risk_summary = f"已取价股票市值：{stock_market_value:.2f}元（非账户总仓位）"
        risk_pct_label = "占已取价股票市值，临时口径"
    total = budget["total_stop_risk"]
    if total is None:
        lower_bound = budget["known_stop_risk_lower_bound"]
        lower_text = f"{lower_bound:.2f}元" if lower_bound is not None else "不可用"
        print(
            f"\n  {risk_summary} | 第二档止损总风险：不可完整判定；已知风险下限：{lower_text}"
        )
    else:
        print(
            f"\n  {risk_summary} | 第二档止损总风险：{total:.2f}元 "
            f"({total / portfolio_value * 100:.2f}%，{risk_pct_label})"
        )
    print(f"  行情覆盖：{valued_count}/{len(holdings)}只")
    print(
        f"  单股风险预算上限：{limits['max_position_risk_pct']:.2f}% | "
        f"组合风险预算上限：{limits['max_portfolio_risk_pct']:.2f}% | "
        f"policy_id={limits['policy_id']}; source={limits['source']}; "
        f"field_sources={limits['field_sources']}"
        + (
            "（文件含确认元数据，不构成交易授权）"
            if limits["source"] == "policy_file"
            else "（未确认个人风险预算；兼容默认/本次覆盖不等于个人政策）"
        )
    )
    if not account_value_supplied:
        print("  未提供总资产：不判定预算超限；风险不可完整判定")
    elif budget["risk_budget_status"] == "over_budget":
        print("  已知超预算：冻结新增风险建议，仅只读复核；不阻断已成交事实记账")
        for breach in budget["breaches"]:
            print(f"    {breach['code'] or '组合'}: {breach['detail']}")
    elif budget["risk_budget_status"] == "within_budget":
        print("  本次有效参数内：预算内；已破线须单独复核，不代表安全")
    else:
        print("  风险不可完整判定：不得宣称组合在预算内")
    print(f"\n  框架分布（按市值，{len(holdings)} 只在仓）")
    for fw, value in sorted(framework_values.items()):
        print(f"    {fw}: {value:.2f}元 ({value / portfolio_value * 100:.1f}%)")
    print()


def cmd_check_holdings(args: list[str] | None = None) -> None:
    """持仓止损检查：对比当前价与15%/20%止损线，主动预警（P3-4）。
    区分盘中现价/收盘价/上一交易日陈旧行情三种口径，非交易时段拿到隔夜收盘价
    时只输出观察提醒、不触发同等级止损预警（见 PITFALLS.md [BUG-006]）。
    """
    with db.db_session() as conn:
        holdings = conn.execute(
            """SELECT id, code, name, cost_price, stop_loss_15, stop_loss_20
               FROM holdings WHERE exit_date IS NULL ORDER BY code"""
        ).fetchall()
        defer_rows = conn.execute(
            """SELECT holding_id, evidence, review_due
               FROM holding_alerts
               WHERE reason_code='price_stop2_liquidity_defer'
                 AND status != 'resolved'"""
        ).fetchall()
    from a_share_research.policy.v0.holdings_check_input import calculate
    if not holdings:
        print("暂无持仓")
        return
    now = domain.cst_now()
    codes = [row[1] for row in holdings]
    quotes = market_quotes.fetch_current_price_quotes(codes)
    market_snapshot = market_quotes.fetch_market_limit_down_snapshot()
    result = calculate(holdings, defer_rows, quotes, market_snapshot, now)
    print(result['stdout'], end='')
