"""Price reference (price-reference-v2, ported from a-stock-tracker): program-computed, no model.

61 sessions of forward-adjusted daily bars and CSI 300 give MA20/MA60, a 20-day relative move,
an observation line (20-day high + 0.01) and an invalidation line (10-day low); valid for the
next 5 trading sessions.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

import tushare as ts

from a_share_research import db
from a_share_research.db import SHANGHAI

log = logging.getLogger(__name__)

SESSIONS = 61
VALID_SESSIONS = 5
LIMITS = (
    "量价仅作风险纪律参考，历史重放未发现稳定超额，不代表投资价值或交易建议。"
    "未覆盖公告、财报与资金变化；A 股 T+1、跳空、停牌和涨跌停可能使退出条件无法执行。"
)


class PriceError(Exception):
    pass


def _compact(d) -> str:
    return d.strftime("%Y%m%d")


def _iso(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def compute(stock: list[dict], benchmark: list[dict]) -> dict:
    """Levels from aligned, forward-adjusted bars (oldest first); pure so it can be tested."""
    if len(stock) != SESSIONS or len(benchmark) != SESSIONS:
        raise PriceError(f"需要连续 {SESSIONS} 个交易日的日线")
    closes = [float(b["close"]) for b in stock]  # plain floats keep the result JSON-serializable
    close = closes[-1]
    ma20, ma60 = sum(closes[-20:]) / 20, sum(closes[-60:]) / 60
    relative = (close / closes[-21] - float(benchmark[-1]["close"]) / float(benchmark[-21]["close"])) * 100
    trigger = round(float(max(b["high"] for b in stock[-20:])) + 0.01, 2)
    stop = round(float(min(b["low"] for b in stock[-10:])), 2)
    return {
        "close": round(close, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "relative": round(relative, 2), "trigger": trigger, "stop": stop,
        "distance": round((close - stop) / close * 100, 2), "uptrend": close > ma20 > ma60,
    }


def reference(code: str, name: str) -> dict:
    if not re.fullmatch(r"(?:60\d{4}\.SH|00\d{4}\.SZ)", code):
        raise PriceError("价格参考只支持沪深主板股票")
    if "ST" in name.upper() or "退" in name:
        raise PriceError("ST 或退市风险股票不提供价格参考")
    pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
    today = datetime.now(SHANGHAI).date()
    start, end = _compact(today - timedelta(days=150)), _compact(today + timedelta(days=30))
    calendar = sorted(pro.trade_cal(exchange="SSE", is_open="1", start_date=start, end_date=end)["cal_date"])
    daily = pro.daily(ts_code=code, start_date=start, end_date=_compact(today)).sort_values("trade_date")
    if daily.empty:
        raise PriceError("没有取到日线")
    as_of = daily["trade_date"].iloc[-1]
    sessions = [d for d in calendar if d <= as_of][-SESSIONS:]
    daily = daily[daily["trade_date"] >= sessions[0]]
    if list(daily["trade_date"]) != sessions:
        raise PriceError("近 61 个交易日有停牌或缺失日线，不提供价格参考")
    factors = pro.adj_factor(ts_code=code, start_date=sessions[0], end_date=as_of).set_index("trade_date")["adj_factor"]
    index = pro.index_daily(ts_code="000300.SH", start_date=sessions[0], end_date=as_of).sort_values("trade_date")
    if list(index["trade_date"]) != sessions or set(factors.index) != set(sessions):
        raise PriceError("沪深 300 或复权因子与交易日不一致")
    latest = factors[as_of]
    stock = [
        {k: row[k] * factors[row["trade_date"]] / latest for k in ("open", "high", "low", "close")}
        for _, row in daily.iterrows()
    ]
    adjustments = [
        _iso(d) for prev, d in zip(sessions, sessions[1:]) if factors[d] != factors[prev]
    ]
    future = [d for d in calendar if d > as_of][:VALID_SESSIONS]
    if len(future) < VALID_SESSIONS:
        raise PriceError("交易日历不足，无法确定有效期")
    levels = compute(stock, index.to_dict("records"))
    return {
        "rule": "price-reference-v2", "code": code, "name": name,
        "as_of": _iso(as_of), "expires": _iso(future[-1]), "levels": levels,
        "adjustments": adjustments, "limits": LIMITS,
    }


def execute(conn, run_id: int) -> None:
    run = conn.execute("SELECT code, name FROM runs WHERE id = ?", (run_id,)).fetchone()
    db.update_run(conn, run_id, status="running", step="price", started_at=db.now())
    try:
        body = reference(run["code"], run["name"] or "")
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO prices (run_id, code, name, as_of, expires, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, run["code"], run["name"], body["as_of"], body["expires"],
                 json.dumps(body, ensure_ascii=False), db.now()),
            )
            db.update_run(conn, run_id, status="done", step=None, finished_at=db.now())
    except Exception as exc:
        if not isinstance(exc, PriceError):
            log.exception("price run %s failed", run_id)
        db.update_run(conn, run_id, status="failed", step=None, finished_at=db.now(),
                      error=str(exc) or type(exc).__name__)
