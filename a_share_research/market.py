"""TuShare gateway: clients, raw-response cache and shared upstream pacing."""

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta

import pandas as pd
import tushare as ts
from a_stock_lib.providers.tushare_quotes import to_tushare_stock_code

from a_share_research import db
from a_share_research.db import SHANGHAI
from a_share_research.companies import SourceError

log = logging.getLogger(__name__)


def client():
    return ts.pro_api(os.environ["TUSHARE_TOKEN"])


def redact_error(exc: Exception) -> str:
    """Preserve the upstream message while removing the configured credential."""
    return str(exc).replace(os.environ.get("TUSHARE_TOKEN") or "\x00", "[REDACTED]")


FINANCIAL_APIS = {"fina_indicator", "income", "cashflow", "fina_audit", "balancesheet"}
HISTORICAL_APIS = {"daily", "daily_basic", "adj_factor", "index_daily", "stk_limit", "suspend_d", "trade_cal", "report_rc"}
CONDITION_APIS = {"index_member_all", "fut_basic", "fut_mapping", "fut_daily", "cn_pmi", "cn_m", "sf_month",
                  "shibor", "shibor_lpr", "cn_cpi", "cn_ppi"}
_condition_last_call = 0.0
REPORT_RC_DAILY_BUDGET = 8
REPORT_RC_ROW_CAP = 3000  # 上限未经官方文档核实，达到即视为可能截断
REPORT_RC_MIN_INTERVAL = 61  # Leave margin around TuShare's one-request-per-minute quota.
REPORT_RC_CONTROL_KEY = "report_rc:cooldown-v1"


def _tushare_cache_key(api: str, params: dict) -> str | None:
    if os.environ.get("TUSHARE_CACHE") == "0":
        return None
    today = datetime.now(SHANGHAI).date()
    if api in CONDITION_APIS:
        # A dated snapshot also covers catalogs and monthly publication/revision lag.
        key = {"api": api, "params": params, "snapshot_day": today.isoformat()}
        return hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    bound = params.get("trade_date") or params.get("end_date")
    try:
        historical = bool(bound) and date.fromisoformat(str(bound)) < today
    except ValueError:
        return None
    if not historical or api not in FINANCIAL_APIS | HISTORICAL_APIS:
        return None
    key = {"api": api, "params": params}
    return hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cached_response(api: str, params: dict) -> pd.DataFrame | None:
    key = _tushare_cache_key(api, params)
    if key is None:
        return None
    path = db.DATA / "tushare_cache.db"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS tushare_cache (key TEXT PRIMARY KEY, data TEXT, created_at TEXT)")
            row = conn.execute("SELECT data,created_at FROM tushare_cache WHERE key=?", (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                if not isinstance(saved, dict) or set(saved) != {"data", "index", "columns"}:
                    raise ValueError("TuShare cache is not a split DataFrame")
                frame = pd.DataFrame(**saved)
                if api == "report_rc" and frame.empty:
                    # A recent empty response can be publication lag, not no coverage.
                    age = datetime.now(SHANGHAI).date() - date.fromisoformat(str(params["end_date"]))
                    ttl = timedelta(days=1 if age.days <= 7 else 7)
                    if datetime.now(SHANGHAI) - datetime.fromisoformat(row[1]) >= ttl:
                        return None
                return frame
    except (OSError, sqlite3.Error, ValueError, TypeError):
        log.warning("TuShare %s cache read failed; fetching upstream", api, exc_info=True)
    return None


class ReportRCBudgetExhausted(SourceError):
    pass


class ReportRCCoolingDown(SourceError):
    pass


def report_rc_connect():
    path = db.DATA / "tushare_cache.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS tushare_cache (key TEXT PRIMARY KEY, data TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS report_rc_budget (day TEXT PRIMARY KEY, calls INTEGER)")
    return conn


def _report_rc_control(conn) -> dict:
    row = conn.execute("SELECT data FROM tushare_cache WHERE key=?", (REPORT_RC_CONTROL_KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def _save_report_rc_control(conn, value: dict, at: datetime) -> None:
    conn.execute("INSERT OR REPLACE INTO tushare_cache VALUES (?,?,?)",
                 (REPORT_RC_CONTROL_KEY, json.dumps(value, ensure_ascii=False), at.isoformat()))


def report_rc_status() -> dict:
    """Read shared pacing state without an upstream request or quota reservation."""
    at = datetime.now(SHANGHAI)
    budget = int(os.environ.get("REPORT_RC_DAILY_BUDGET", REPORT_RC_DAILY_BUDGET))
    with closing(report_rc_connect()) as conn:
        state = _report_rc_control(conn)
        row = conn.execute("SELECT calls FROM report_rc_budget WHERE day=?", (at.date().isoformat(),)).fetchone()
    used = row[0] if row else 0
    retry = datetime.fromisoformat(state["retry_at"]) if state.get("retry_at") else at
    if used >= budget:
        retry = max(retry, datetime.combine(at.date() + timedelta(days=1), datetime.min.time(), SHANGHAI))
    return {"used": used, "limit": budget, "retry_at": max(at, retry).isoformat(), "reason": state.get("reason")}


def _defer_report_rc(exc: Exception) -> None:
    at = datetime.now(SHANGHAI)
    reason = str(exc).replace(os.environ.get("TUSHARE_TOKEN") or "\x00", "[REDACTED]")
    if any(part in reason for part in ("每天", "每日", "次/天", "次/日")):
        retry = datetime.combine(at.date() + timedelta(days=1), datetime.min.time(), SHANGHAI)
    elif "小时" in reason:
        retry = at + timedelta(hours=1, seconds=1)
    else:
        retry = at + timedelta(seconds=REPORT_RC_MIN_INTERVAL if _report_rc_limited(exc) else 300)
    with closing(report_rc_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        state = _report_rc_control(conn)
        if state.get("retry_at"):
            retry = max(retry, datetime.fromisoformat(state["retry_at"]))
        _save_report_rc_control(conn, {"retry_at": retry.isoformat(), "reason": reason}, at)


def _reserve_report_rc_call() -> None:
    """Atomically reserve the daily budget AND minute slot across processes/restarts."""
    budget = int(os.environ.get("REPORT_RC_DAILY_BUDGET", REPORT_RC_DAILY_BUDGET))
    with closing(report_rc_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        at = datetime.now(SHANGHAI)
        day = at.date().isoformat()
        row = conn.execute("SELECT calls FROM report_rc_budget WHERE day=?", (day,)).fetchone()
        if (row and row[0] >= budget) or budget <= 0:
            raise ReportRCBudgetExhausted(f"卖方预测每日上游调用预算已用尽（{budget} 次）")
        state = _report_rc_control(conn)
        if state.get("retry_at") and datetime.fromisoformat(state["retry_at"]) > at:
            raise ReportRCCoolingDown(f"卖方预测冷却至 {state['retry_at']}：{state.get('reason') or '每分钟一次'}")
        conn.execute("INSERT INTO report_rc_budget VALUES (?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (day,))
        _save_report_rc_control(conn, {"retry_at": (at + timedelta(seconds=REPORT_RC_MIN_INTERVAL)).isoformat()}, at)


def query(pro, api: str, **params) -> pd.DataFrame:
    """Cache historical raw responses, before a-stock-lib normalizes dates and numbers."""
    cached = cached_response(api, params)
    if cached is not None:
        cached.attrs["cache_hit"] = True
        return cached
    if api == "report_rc":
        _reserve_report_rc_call()
    if api == "shibor_lpr":
        # This account permits one upstream request per hour, shared across workers.
        with closing(report_rc_connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            at = datetime.now(SHANGHAI)
            row = conn.execute("SELECT created_at FROM tushare_cache WHERE key='shibor_lpr:last-call'").fetchone()
            if row and at < datetime.fromisoformat(row[0]) + timedelta(seconds=3601):
                retry = datetime.fromisoformat(row[0]) + timedelta(seconds=3601)
                raise SourceError(f"LPR 每小时一次，冷却至 {retry.isoformat()}；请先使用其他宏观系列，勿重复查询")
            conn.execute("INSERT OR REPLACE INTO tushare_cache VALUES ('shibor_lpr:last-call', '{}', ?)", (at.isoformat(),))
    if api in CONDITION_APIS:
        global _condition_last_call
        time.sleep(max(0, 0.35 - (time.monotonic() - _condition_last_call)))
        _condition_last_call = time.monotonic()
    try:
        frame = pro.query(api, **params)
        if api == "report_rc":
            if not isinstance(frame, pd.DataFrame):
                raise SourceError("report_rc 返回值不是数据表")
            report_rc_rows(frame)
            if params.get("start_date") == params.get("end_date") and not frame.empty:
                if not pd.to_datetime(frame["report_date"], errors="raise").dt.date.eq(
                        date.fromisoformat(str(params["end_date"]))).all():
                    raise SourceError(f"report_rc {params['end_date']} 返回了其他日期的预测")
    except Exception as exc:
        if api == "report_rc":
            _defer_report_rc(exc)
        raise
    key = _tushare_cache_key(api, params)
    if key is None:
        return frame
    path = db.DATA / "tushare_cache.db"
    # Only report_rc caches empty historical days, with a bounded retry TTL.
    if isinstance(frame, pd.DataFrame) and (not frame.empty or api == "report_rc"):
        try:
            # Python's JSON float encoding preserves precision and leaves date/code strings intact.
            data = json.dumps(frame.to_dict(orient="split"), ensure_ascii=False)
            with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as conn:
                conn.execute("INSERT OR REPLACE INTO tushare_cache VALUES (?, ?, ?)", (key, data, db.now()))
        except Exception:
            log.warning("TuShare %s cache write failed", api, exc_info=True)
    return frame


def report_rc_rows(frame: pd.DataFrame, code: str | None = None) -> list[dict]:
    """Validate the response before handing sell-side data to the pure consensus calculator."""
    if not isinstance(frame, pd.DataFrame):
        raise SourceError("TuShare report_rc 返回的不是数据表")
    if frame.empty:
        return []
    required = {"ts_code", "report_date", "org_name", "quarter", "np"}
    if not required <= set(frame.columns):
        raise SourceError("TuShare report_rc 缺少 ts_code、report_date、org_name、quarter 或 np")
    if code is not None:
        frame = frame.loc[frame["ts_code"].eq(to_tushare_stock_code(code))]
    selected = frame[sorted(required)].copy()
    selected["report_date"] = pd.to_datetime(selected["report_date"], errors="raise").dt.strftime("%Y-%m-%d")
    selected["np"] = pd.to_numeric(selected["np"], errors="raise")
    # TuShare np is 万元, not 元. The captured 603606 response has 2028Q4 np=217800
    # (21.78 亿元); np/eps implies ~8.25 亿 shares, matching current shares. Treat np
    # as attributable net profit on this evidence (inference); tp is total profit.
    return selected.astype(object).where(pd.notna(selected), None).to_dict(orient="records")


def _report_rc_limited(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(part in message for part in ("频次", "限频", "超限", "每分钟", "每小时", "每天", "每日", "rate limit", "over limit"))


class ScreenError(Exception):
    pass


def _calendar_query(pro, api: str, **params) -> pd.DataFrame:
    """Cached TuShare call with two retries; the last error names the endpoint and parameters."""
    for attempt in range(3):
        try:
            return query(pro, api, **params)
        except Exception as exc:
            if attempt == 2:
                raise ScreenError(f"{api} {params.get('ts_code') or params.get('trade_date') or ''}: {exc}") from exc
            time.sleep(1 + attempt * 2)
    raise AssertionError("unreachable")


def trade_days(pro, start: str, end: str) -> list[str]:
    return sorted(_calendar_query(pro, "trade_cal", exchange="SSE", is_open="1", start_date=start, end_date=end)["cal_date"])
