"""Watchlist and cache maintenance commands."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from a_share_research.policy.v0 import (
    db,
    domain,
    store,
)

_UTC = timezone.utc


def get_watchlist_rows() -> list[dict]:
    """Return the durable refresh set in a single fair scheduling order."""
    now = domain.utc_now()
    today = domain.cst_today()
    analysis_cutoff = now - timedelta(days=14)
    first_analysis_cutoff = now - timedelta(hours=24)
    with db.db_session() as conn:
        fundamentals_rows = conn.execute(
            "SELECT code, name, industry, data, updated_at, ttl_hours FROM stock_fundamentals"
        ).fetchall()
        analysis_rows = conn.execute(
            """SELECT code, date, name, created_at, score, flags,
                      score_breakdown, quote_price
               FROM (
                   SELECT code, date, name, created_at, score, flags,
                          score_breakdown, quote_price,
                          ROW_NUMBER() OVER (
                              PARTITION BY code
                              ORDER BY created_at DESC, date DESC
                          ) AS row_rank
                   FROM analysis_results
               )
               WHERE row_rank=1"""
        ).fetchall()
        today_analysis_rows = conn.execute(
            """SELECT code, date, name, created_at, score, flags,
                      score_breakdown, quote_price
               FROM analysis_results WHERE date=?""",
            (today,),
        ).fetchall()
        holding_rows = conn.execute(
            "SELECT code, name FROM holdings WHERE exit_date IS NULL"
        ).fetchall()
        quote_rows = conn.execute(
            """SELECT code, price, fetched_at
               FROM (
                   SELECT code, price, fetched_at,
                          ROW_NUMBER() OVER (
                              PARTITION BY code
                              ORDER BY fetched_at DESC, id DESC
                          ) AS row_rank
                   FROM quote_snapshots WHERE valid=1
               )
               WHERE row_rank=1"""
        ).fetchall()
        qualitative_rows = conn.execute(
            "SELECT code, name, industry FROM qualitative_only_securities"
        ).fetchall()

    fundamentals = {row[0]: row for row in fundamentals_rows}
    latest_analysis = {row[0]: row for row in analysis_rows}
    today_analysis = {row[0]: row for row in today_analysis_rows}
    has_any_analysis = set(latest_analysis)
    recent_analysis_codes: set[str] = set()
    for row in analysis_rows:
        code = row[0]
        try:
            if domain.parse_timestamp_utc(row[3]) >= analysis_cutoff:
                recent_analysis_codes.add(code)
        except (TypeError, ValueError):
            continue

    latest_quotes = {row[0]: row for row in quote_rows}
    recent_fetch_codes: set[str] = set()
    for row in quote_rows:
        try:
            if domain.parse_timestamp_utc(row[2]) >= first_analysis_cutoff:
                recent_fetch_codes.add(row[0])
        except (TypeError, ValueError):
            continue

    holding_names = {code: name for code, name in holding_rows}
    qualitative_only_securities = {
        code: (name, industry) for code, name, industry in qualitative_rows
    }
    holding_codes = set(holding_names)
    pending_first_codes = recent_fetch_codes - has_any_analysis
    eligible_codes = holding_codes | recent_analysis_codes | pending_first_codes

    rows: list[dict] = []
    for code in eligible_codes:
        fund = fundamentals.get(code)
        latest = latest_analysis.get(code)
        current = today_analysis.get(code)
        quote = latest_quotes.get(code)
        data = store.safe_json_value(fund[3], dict, {}) if fund else {}
        cache_expired = fund is None or domain.is_expired(fund[4], fund[5])
        terminal_status = qualitative_only_securities.get(code)
        industry = (fund[2] if fund else None) or (
            terminal_status[1] if terminal_status else None
        )
        qualitative_only = bool(
            terminal_status
        ) or domain.is_unsupported_financial_industry(industry)
        price_invalidated = False
        analysis_price = current[7] if current else None
        quote_price = quote[1] if quote else None
        if (
            isinstance(analysis_price, (int, float))
            and not isinstance(analysis_price, bool)
            and math.isfinite(float(analysis_price))
            and analysis_price > 0
            and isinstance(quote_price, (int, float))
            and not isinstance(quote_price, bool)
            and math.isfinite(float(quote_price))
            and quote_price > 0
        ):
            price_invalidated = (
                abs(quote_price - analysis_price) / analysis_price
                >= domain.ANALYSIS_PRICE_INVALIDATION_THRESHOLD
            )
        needs_refresh = not qualitative_only and (
            current is None or cache_expired or price_invalidated
        )
        if code in holding_codes:
            eligible_reason = "holding"
        elif code in pending_first_codes:
            eligible_reason = "pending-first-analysis"
        else:
            eligible_reason = "recent-analysis"
        display_analysis = current or latest
        raw_flags = (
            store.safe_json_value(display_analysis[5], list, [])
            if display_analysis
            else []
        )
        flags = [
            flag
            for flag in raw_flags
            if isinstance(flag, dict) and flag.get("level") in {"red", "yellow"}
        ]
        score_breakdown = (
            store.safe_json_value(display_analysis[6], dict, None)
            if display_analysis
            else None
        )
        rows.append(
            {
                "code": code,
                "name": (
                    (fund[1] if fund else None)
                    or holding_names.get(code)
                    or (terminal_status[0] if terminal_status else None)
                    or (latest[2] if latest else None)
                    or code
                ),
                "industry": industry,
                "eligible_reason": eligible_reason,
                "refresh_blocked_reason": (
                    "unsupported-financial-qualitative-only"
                    if qualitative_only
                    else None
                ),
                "is_holding": code in holding_codes,
                "cache_expired": cache_expired,
                "last_analysis_at": latest[3] if latest else None,
                "updated_at": fund[4] if fund else None,
                "ttl_hours": fund[5] if fund else None,
                "analysis_time": current[3] if current else None,
                "score": display_analysis[4] if display_analysis else None,
                "flags": flags,
                "score_breakdown": score_breakdown,
                "needs_refresh": needs_refresh,
                "price_invalidated": price_invalidated,
                "pe_static": data.get("pe_static") or data.get("pe_ttm"),
                "pe_ttm": data.get("pe_ttm"),
                "pb": data.get("pb"),
                "dividend_yield": data.get("dividend_yield"),
                "roe_3y_avg": data.get("roe_3y_avg"),
            }
        )

    def oldest_first(value: str | None) -> tuple[int, datetime]:
        if value is None:
            return (0, datetime.min.replace(tzinfo=_UTC))
        try:
            return (1, domain.parse_timestamp_utc(value))
        except (TypeError, ValueError):
            return (0, datetime.min.replace(tzinfo=_UTC))

    rows.sort(
        key=lambda row: (
            not row["needs_refresh"],
            not row["is_holding"],
            row["eligible_reason"] != "pending-first-analysis",
            oldest_first(row["last_analysis_at"]),
            oldest_first(row["updated_at"]),
            row["code"],
        )
    )
    for priority, row in enumerate(rows, start=1):
        row["refresh_priority"] = priority
    return rows


def _format_breakdown_line(bd: dict) -> str:
    """Format score breakdown as a watchlist sub-row.

    Supports two schemas:
    - Nested: {"fundamentals": {"subtotal": N, ...}, "timing": {"subtotal": N, ...}, "total": N}
    - Flat with top-level subtotals: {"fundamentals": N, "timing": N, "total": N}
    - Legacy flat: {"roe": N, "volume": N, ...} (no fundamentals/timing keys)
    """
    if "fundamentals" in bd and "timing" in bd:
        f_val = bd["fundamentals"]
        t_val = bd["timing"]
        f_sub = f_val.get("subtotal", "?") if isinstance(f_val, dict) else f_val
        t_sub = t_val.get("subtotal", "?") if isinstance(t_val, dict) else t_val
        total = bd.get("total", "?")
        return f"  分项: 基本面 {f_sub}/60 | 择时 {t_sub}/20 | 合计 {total}/80"
    total = bd.get("total", sum(v for v in bd.values() if isinstance(v, (int, float))))
    return f"  分项: 合计 {total}"


def cmd_watchlist(args: list[str] | None = None) -> None:
    """显示持久刷新集合，顺序与自动刷新公平调度顺序一致。"""
    args = args or []
    show_breakdown = "--breakdown" in args
    rows = get_watchlist_rows()
    if "--json" in args:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if not rows:
        print("暂无有效缓存股票")
        return

    header = f"{'股票':<14} {'行业':<16} {'得分':>4} {'PE':>6} {'PB':>5} {'股息率':>7} {'ROE':>7}  预警  分析"
    print(header)
    print("─" * 75)
    for row in rows:
        code, name = row["code"], row["name"]
        industry = row["industry"] or "─"
        analysis_time, score, flags = row["analysis_time"], row["score"], row["flags"]
        pe = str(row["pe_static"] or "─")
        pb = str(row["pb"] or "─")
        div = (str(row["dividend_yield"]) + "%") if row["dividend_yield"] else "─"
        roe = (str(row["roe_3y_avg"]) + "%") if row["roe_3y_avg"] else "─"
        score_str = str(score) if score is not None else "─"
        # 预警标记
        if any(f["level"] == "red" for f in flags):
            flag_str = "🔴"
        elif any(f["level"] == "yellow" for f in flags):
            flag_str = "⚠️"
        else:
            flag_str = "─"
        tag = (
            "仅定性"
            if row.get("refresh_blocked_reason")
            else "✅今日"
            if analysis_time
            else "─"
        )
        label = f"{name}({code})"
        print(
            f"{label:<14} {industry:<16} {score_str:>4} {pe:>6} {pb:>5} {div:>7} {roe:>7}  {flag_str:<4}  {tag}"
        )
        if show_breakdown and row.get("score_breakdown"):
            print(_format_breakdown_line(row["score_breakdown"]))


def cmd_list(args: list[str] | None = None) -> None:
    """列出所有缓存内容（含过期）"""
    today = domain.cst_today()
    with db.db_session() as conn:
        stocks = conn.execute(
            "SELECT code, name, industry, updated_at, ttl_hours FROM stock_fundamentals ORDER BY updated_at DESC"
        ).fetchall()
        analyses = conn.execute(
            """SELECT a.code, a.date, a.created_at, COALESCE(a.name, f.name, a.code) as display_name,
                      a.score, a.flags, a.score_breakdown
               FROM analysis_results a
               LEFT JOIN stock_fundamentals f ON a.code = f.code
               ORDER BY a.created_at DESC LIMIT 20"""
        ).fetchall()

    valid_count = sum(1 for s in stocks if not domain.is_expired(s[3], s[4]))
    print(f"=== 基本面缓存 ({valid_count}有效 / {len(stocks)}条) ===")
    for s in stocks:
        code, name, industry, updated_at, ttl_hours = s
        expired = domain.is_expired(updated_at, ttl_hours)
        status = "⚠️ 已过期" if expired else "✅ 有效"
        print(
            f"  {name}({code}) [{industry}] 更新:{domain.format_timestamp_cst(updated_at)} TTL:{ttl_hours}h [{status}]"
        )

    today_count = sum(1 for a in analyses if a[1] == today)
    print(f"\n=== 分析结论缓存 ({today_count}今日有效 / 近{len(analyses)}条) ===")
    for a in analyses:
        code, date, created_at, display_name, score, flags_raw, breakdown_raw = a
        status = "✅ 今日有效" if date == today else f"⚠️ 过期({date})"
        score_str = f" 得分:{score}" if score is not None else ""
        breakdown_str = " 📊分项" if breakdown_raw else ""
        raw_flags = store.safe_json_value(flags_raw, list, [])
        flags = [
            flag
            for flag in raw_flags
            if isinstance(flag, dict) and flag.get("level") in {"red", "yellow"}
        ]
        flag_icons = "".join("🔴" if f["level"] == "red" else "⚠️" for f in flags)
        print(
            f"  {display_name}({code}) [{status}]{score_str}{breakdown_str}{flag_icons} 创建:{domain.format_timestamp_cst(created_at)}"
        )


def cmd_cleanup(args: list[str] | None = None) -> None:
    """清除所有过期的缓存条目"""
    with db.db_session() as conn:
        stocks = conn.execute(
            "SELECT code, name, updated_at, ttl_hours FROM stock_fundamentals"
        ).fetchall()
        expired_names = []
        for s in stocks:
            if domain.is_expired(s[2], s[3]):
                deleted = conn.execute(
                    """DELETE FROM stock_fundamentals
                       WHERE code=? AND updated_at=?""",
                    (s[0], s[2]),
                ).rowcount
                if deleted:
                    expired_names.append(f"{s[1]}({s[0]})")

        analysis_cutoff = domain.utc_now() - timedelta(days=14)
        analysis_rows = conn.execute(
            "SELECT code, date, created_at FROM analysis_results"
        ).fetchall()
        old_count = 0
        for code, analysis_date, created_at in analysis_rows:
            try:
                if domain.parse_timestamp_utc(created_at) < analysis_cutoff:
                    old_count += conn.execute(
                        """DELETE FROM analysis_results
                           WHERE code=? AND date=? AND created_at=?""",
                        (code, analysis_date, created_at),
                    ).rowcount
            except (TypeError, ValueError):
                # Unknown legacy timestamp formats are retained rather than
                # risking deletion of scheduler history.
                continue
        pruned_quote_count = conn.execute(
            """DELETE FROM quote_snapshots
               WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY code
                                  ORDER BY fetched_at DESC, id DESC
                              ) AS row_rank
                       FROM quote_snapshots
                   )
                   WHERE row_rank > ?
               )""",
            (store.QUOTE_SNAPSHOT_RETENTION_PER_CODE,),
        ).rowcount
        orphan_count = 0
        for table in (
            "holding_events",
            "holding_l3_conditions",
            "holding_thesis_versions",
            "holding_tier_state",
            "holding_alerts",
            "retro_notes",
        ):
            orphan_count += conn.execute(
                f"""DELETE FROM {table}
                    WHERE NOT EXISTS (
                      SELECT 1 FROM holdings h WHERE h.id={table}.holding_id
                    )"""
            ).rowcount
        conn.commit()

    if expired_names:
        print(f"已清除过期基本面缓存：{', '.join(expired_names)}")
    if old_count:
        print(f"已清除 {old_count} 条历史分析结论")
    if pruned_quote_count:
        print(f"已清除 {pruned_quote_count} 条超限行情快照")
    if orphan_count:
        print(f"已清除 {orphan_count} 条孤儿持仓关联记录")
    if (
        not expired_names
        and not old_count
        and not pruned_quote_count
        and not orphan_count
    ):
        print("无过期缓存，无需清理")


def cmd_clear(args: list[str]) -> None:
    """清除缓存。不带参数=清全部，带代码=清指定股票"""
    with db.db_session() as conn:
        if args:
            code = args[0]
            conn.execute("DELETE FROM stock_fundamentals WHERE code=?", (code,))
            conn.execute("DELETE FROM analysis_results WHERE code=?", (code,))
            conn.execute("DELETE FROM quote_snapshots WHERE code=?", (code,))
            conn.execute(
                "DELETE FROM qualitative_only_securities WHERE code=?", (code,)
            )
            conn.commit()
            print(f"已清除 {code} 的所有缓存")
        else:
            conn.execute("DELETE FROM stock_fundamentals")
            conn.execute("DELETE FROM analysis_results")
            conn.execute("DELETE FROM quote_snapshots")
            conn.execute("DELETE FROM market_indicator_snapshots")
            conn.execute("DELETE FROM qualitative_only_securities")
            conn.commit()
            print("已清除全部缓存")
