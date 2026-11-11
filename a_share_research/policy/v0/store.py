"""Database-backed fundamentals, quote, and indicator stores."""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, time as dtime, timedelta

from a_share_research.policy.v0 import db, domain, risk_gates

logger = logging.getLogger(__name__)
_DATA_PERIOD_RE = re.compile(r"^(?:\d{4}年报|\d{4}半年报|\d{4}Q[1-3])$")
QUOTE_SNAPSHOT_MAX_AGE = timedelta(minutes=30)
QUOTE_SNAPSHOT_RETENTION_PER_CODE = 64
MAX_SNAPSHOT_CLOCK_SKEW = timedelta(seconds=30)
_GATE_FIELDS = {
    "regulatory_gate": {"clear", "blocked", "incomplete"},
    "roe_structural_gate": {"clear", "blocked", "incomplete"},
    "cash_flow_gate": {"clear", "blocked", "incomplete", "review_required", "not_applicable"},
}


def validate_gate_payload(name: str, value: object) -> str | None:
    if not isinstance(value, dict):
        return f"{name} 必须是 JSON 对象"
    status = value.get("status")
    if status not in _GATE_FIELDS[name]:
        return f"{name}.status 非法"
    eligible = value.get("action_eligible")
    if not isinstance(eligible, bool):
        return f"{name}.action_eligible 必须为布尔值"
    if eligible != (status in {"clear", "not_applicable"}):
        return f"{name}.action_eligible 与 status 不一致"
    if not isinstance(value.get("reason_code"), str) or not value["reason_code"].strip():
        return f"{name}.reason_code 缺失"
    sources = value.get("sources", value.get("source"))
    if not isinstance(sources, (str, list, tuple)) or not sources:
        return f"{name}.sources 缺失"
    source_values = [sources] if isinstance(sources, str) else list(sources)
    if status in {"clear", "not_applicable"} and not risk_gates.official_sources(
        [item for item in source_values if isinstance(item, str)],
        allow_statement_mirror=name != "regulatory_gate",
    ):
        return f"{name}.sources 非官方"
    if not isinstance(value.get("as_of"), str) or not value["as_of"].strip():
        return f"{name}.as_of 缺失"
    if name == "regulatory_gate":
        if not isinstance(value.get("rule_version"), str) or not value["rule_version"].strip():
            return "regulatory_gate.rule_version 缺失"
        checks = value.get("checks")
        expected = {"listing_risk", "audit_opinion", "investigation", "dividend_compliance"}
        if not isinstance(checks, dict) or set(checks) != expected:
            return "regulatory_gate.checks 不完整"
        for check_name, check in checks.items():
            if not isinstance(check, dict) or check.get("status") not in {"clear", "blocked", "incomplete", "not_applicable"}:
                return f"regulatory_gate.checks.{check_name} 非法"
            if not isinstance(check.get("reason_code"), str) or not check["reason_code"].strip():
                return f"regulatory_gate.checks.{check_name}.reason_code 缺失"
            expected_check_eligibility = check["status"] in {"clear", "not_applicable"}
            if check.get("action_eligible") is not None and check["action_eligible"] != expected_check_eligibility:
                return f"regulatory_gate.checks.{check_name}.action_eligible 与 status 不一致"
        if status == "clear" and any(
            check["status"] not in {"clear", "not_applicable"}
            for check in checks.values()
        ):
            return "regulatory_gate.status 与 checks 子项冲突"
    return None


def validate_fundamentals_payload(data: dict) -> str | None:
    """Validate report period, null reasons and per-field provenance."""
    if not isinstance(data, dict):
        return "基本面数据必须是 JSON 对象"
    period = data.get("data_period")
    if not isinstance(period, str) or _DATA_PERIOD_RE.fullmatch(period) is None:
        return "data_period 必须为 YYYY年报、YYYY半年报或 YYYYQ1—YYYYQ3"
    null_reasons = data.get("null_reasons")
    provenance = data.get("field_provenance")
    if not isinstance(null_reasons, dict):
        return "null_reasons 必须是 JSON 对象"
    if not isinstance(provenance, dict):
        return "field_provenance 必须是 JSON 对象"

    business_fields = set(data) - {"data_period", "null_reasons", "field_provenance"}
    for gate_name in _GATE_FIELDS.keys() & business_fields:
        gate_error = validate_gate_payload(gate_name, data[gate_name])
        if gate_error:
            return gate_error
    for field in sorted(business_fields):
        field_provenance = provenance.get(field)
        if not isinstance(field_provenance, dict):
            return f"字段 {field} 缺少 field_provenance"
        source = field_provenance.get("source")
        as_of = field_provenance.get("as_of")
        status = field_provenance.get("status")
        if not isinstance(source, str) or not source.strip():
            return f"字段 {field} provenance.source 缺失"
        if not isinstance(as_of, str) or not as_of.strip():
            return f"字段 {field} provenance.as_of 缺失"
        if status not in {"ok", "missing"}:
            return f"字段 {field} provenance.status 必须为 ok 或 missing"
        gate_incomplete = (
            field in _GATE_FIELDS
            and isinstance(data[field], dict)
            and data[field].get("status") not in {"clear", "not_applicable"}
        )
        if data[field] is None or gate_incomplete:
            if (
                status != "missing"
                or not isinstance(null_reasons.get(field), str)
                or not null_reasons[field].strip()
            ):
                return f"缺失字段 {field} 必须同时提供 status=missing 和 null_reasons"
        elif status != "ok":
            return f"非空字段 {field} 的 provenance.status 必须为 ok"

    unknown_provenance = set(provenance) - business_fields
    if unknown_provenance:
        return f"field_provenance 包含未写入的字段: {', '.join(sorted(unknown_provenance))}"
    unknown_reasons = set(null_reasons) - {
        field
        for field in business_fields
        if data[field] is None
        or (
            field in _GATE_FIELDS
            and isinstance(data[field], dict)
            and data[field].get("status") not in {"clear", "not_applicable"}
        )
    }
    if unknown_reasons:
        return f"null_reasons 包含非缺失字段: {', '.join(sorted(unknown_reasons))}"
    return None


def safe_json_value(raw: str | None, expected_type: type, default):
    """Read legacy JSON without letting one corrupt row break batch commands."""
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring malformed cached JSON value")
        return default
    if not isinstance(value, expected_type):
        logger.warning("Ignoring cached JSON value with unexpected type")
        return default
    return value


def record_quote_snapshot(
    code: str,
    price: float,
    quote_date: str,
    quote_time: str,
    source: str,
    verification_sources: dict,
    degraded: bool,
    *,
    fetched_at: str | None = None,
) -> None:
    """Persist one already-validated realtime quote snapshot."""
    if (
        not code
        or isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(float(price))
        or price <= 0
        or not source
        or not isinstance(verification_sources, dict)
        or source not in verification_sources
        or not isinstance(degraded, bool)
    ):
        raise ValueError("invalid validated quote snapshot")
    date.fromisoformat(quote_date)
    dtime.fromisoformat(quote_time)
    fetched_at_dt = domain.parse_timestamp_utc(fetched_at or domain.utc_now_iso())
    if fetched_at_dt - domain.utc_now() > MAX_SNAPSHOT_CLOCK_SKEW:
        raise ValueError("quote snapshot fetched_at is in the future")
    fetched_at_value = fetched_at_dt.isoformat()
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO quote_snapshots
               (code, price, quote_date, quote_time, fetched_at, source,
                verification_sources, degraded, valid)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (
                code,
                price,
                quote_date,
                quote_time,
                fetched_at_value,
                source,
                json.dumps(verification_sources, ensure_ascii=False),
                int(degraded),
            ),
        )
        conn.execute(
            """DELETE FROM quote_snapshots
               WHERE code=?
                 AND id NOT IN (
                     SELECT id FROM quote_snapshots
                     WHERE code=?
                     ORDER BY fetched_at DESC, id DESC
                     LIMIT ?
                 )""",
            (code, code, QUOTE_SNAPSHOT_RETENTION_PER_CODE),
        )
        conn.commit()


def get_latest_quote_snapshot(
    code: str,
    *,
    max_age: timedelta | None = None,
) -> dict | None:
    """Return the latest valid quote snapshot, optionally bounded by age."""
    if max_age is not None and max_age < timedelta(0):
        raise ValueError("max_age must be non-negative")
    with db.db_session() as conn:
        row = conn.execute(
            """SELECT price, quote_date, quote_time, fetched_at, source,
                      verification_sources, degraded
               FROM quote_snapshots
               WHERE code=? AND valid=1 ORDER BY fetched_at DESC LIMIT 1""",
            (code,),
        ).fetchone()
    if row is None:
        return None
    try:
        age = domain.utc_now() - domain.parse_timestamp_utc(row[3])
    except (TypeError, ValueError):
        logger.warning("Ignoring quote snapshot with malformed fetched_at")
        return None
    if age < -MAX_SNAPSHOT_CLOCK_SKEW:
        return None
    if max_age is not None and age > max_age:
        return None
    return {
        "price": row[0],
        "quote_date": row[1],
        "quote_time": row[2],
        "fetched_at": row[3],
        "source": row[4],
        "verification_sources": safe_json_value(row[5], dict, {}),
        "degraded": bool(row[6]),
        "quote_as_of": f"{row[1]}T{row[2]}",
    }


def set_market_indicator_snapshot(
    indicator_key: str,
    value: float,
    as_of: str,
    source: str,
    *,
    fetched_at: str | None = None,
) -> None:
    """Persist the latest trusted market-wide indicator snapshot."""
    if (
        not indicator_key
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
        or not as_of
        or not source
    ):
        raise ValueError("invalid market indicator snapshot")
    fetched_at_dt = domain.parse_timestamp_utc(fetched_at or domain.utc_now_iso())
    if fetched_at_dt - domain.utc_now() > MAX_SNAPSHOT_CLOCK_SKEW:
        raise ValueError("market indicator fetched_at is in the future")
    fetched_at_value = fetched_at_dt.isoformat()
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO market_indicator_snapshots
               (indicator_key, value, as_of, fetched_at, source, status)
               VALUES (?,?,?,?,?,'ok')
               ON CONFLICT(indicator_key) DO UPDATE SET
                   value=excluded.value, as_of=excluded.as_of,
                   fetched_at=excluded.fetched_at, source=excluded.source,
                   status='ok'
               WHERE excluded.fetched_at >= market_indicator_snapshots.fetched_at""",
            (indicator_key, value, as_of, fetched_at_value, source),
        )
        conn.commit()


def update_qualitative_only_security(
    code: str, name: str, industry: str | None
) -> None:
    """Persist or clear the terminal qualitative-only routing decision."""
    if not industry or "未知" in industry:
        return
    with db.db_session() as conn:
        if domain.is_unsupported_financial_industry(industry):
            conn.execute(
                """INSERT INTO qualitative_only_securities
                   (code, name, industry, updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                       name=excluded.name, industry=excluded.industry,
                       updated_at=excluded.updated_at""",
                (code, name, industry, domain.utc_now_iso()),
            )
        else:
            conn.execute(
                "DELETE FROM qualitative_only_securities WHERE code=?", (code,)
            )
        conn.commit()


def get_market_indicator_snapshot(
    indicator_key: str,
    *,
    max_age: timedelta | None = None,
) -> dict | None:
    """Return a source-complete market indicator within the requested age."""
    if max_age is not None and max_age < timedelta(0):
        raise ValueError("max_age must be non-negative")
    with db.db_session() as conn:
        row = conn.execute(
            """SELECT value, as_of, fetched_at, source, status
               FROM market_indicator_snapshots WHERE indicator_key=?""",
            (indicator_key,),
        ).fetchone()
    if row is None or row[4] != "ok" or not row[3]:
        return None
    try:
        age = domain.utc_now() - domain.parse_timestamp_utc(row[2])
    except (TypeError, ValueError):
        logger.warning("Ignoring market indicator with malformed fetched_at")
        return None
    if age < -MAX_SNAPSHOT_CLOCK_SKEW:
        return None
    if max_age is not None and age > max_age:
        return None
    return {
        "value": row[0],
        "as_of": row[1],
        "fetched_at": row[2],
        "source": row[3],
        "status": row[4],
    }


def add_market_indicators(data: dict) -> dict:
    """Expose fresh global indicators without storing them per stock."""
    result = dict(data)
    snapshot = get_market_indicator_snapshot(
        "bond_yield_10y", max_age=timedelta(hours=24)
    )
    if snapshot is not None:
        result["bond_yield_10y"] = snapshot["value"]
        result.setdefault("market_indicator_provenance", {})["bond_yield_10y"] = (
            snapshot
        )
    return result


def apply_regulatory_gate(data: dict, gate: dict) -> None:
    """Install a P0 gate with matching null reason and provenance."""
    data["regulatory_gate"] = gate
    null_reasons = data.setdefault("null_reasons", {})
    if gate["status"] == "clear":
        null_reasons.pop("regulatory_gate", None)
    else:
        null_reasons["regulatory_gate"] = gate["reason_code"]
    data.setdefault("field_provenance", {})["regulatory_gate"] = {
        "source": "computed",
        "as_of": gate.get("as_of") or data.get("data_period"),
        "status": "ok" if gate["status"] == "clear" else "missing",
    }


def _carry_forward_regulatory_gate(conn, code: str, data_dict: dict) -> None:
    incoming = data_dict.get("regulatory_gate")
    if isinstance(incoming, dict) and "evidence_facts" in incoming:
        return
    row = conn.execute(
        "SELECT data FROM stock_fundamentals WHERE code=?", (code,)
    ).fetchone()
    try:
        previous = json.loads(row[0]).get("regulatory_gate") if row else None
    except (TypeError, ValueError, AttributeError):
        previous = None
    carried = risk_gates.carry_forward_regulatory_gate(
        previous, code, data_dict.get("data_period")
    )
    if carried is not None:
        apply_regulatory_gate(data_dict, carried)


def get_fundamentals(code: str) -> dict | None:
    """获取基本面缓存。未命中或过期返回 None；命中返回含 _cache_meta 的 dict。"""
    with db.db_session() as conn:
        row = conn.execute(
            "SELECT name, industry, data, updated_at, ttl_hours FROM stock_fundamentals WHERE code=?",
            (code,),
        ).fetchone()
    if not row:
        return None
    name, industry, data, updated_at, ttl_hours = row
    if domain.is_expired(updated_at, ttl_hours):
        return None
    result = add_market_indicators(json.loads(data))
    result["_cache_meta"] = {
        "code": code,
        "name": name,
        "industry": industry,
        "updated_at": domain.format_timestamp_cst(updated_at),
        "ttl_hours": ttl_hours,
    }
    return result


def set_fundamentals(
    code: str, name: str, industry: str, data_dict: dict, ttl: int | None = None
) -> str:
    """写入基本面缓存，ttl=None 时按行业自动推断。返回状态消息。

    同一写事务内读取最新 P0 证据并按年报期沿用，避免覆盖并发补录的证据。
    """
    ttl_hours = ttl if ttl is not None else domain.get_industry_ttl(industry)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _carry_forward_regulatory_gate(conn, code, data_dict)
        validation_error = validate_fundamentals_payload(data_dict)
        if validation_error:
            conn.rollback()
            raise ValueError(validation_error)
        conn.execute(
            """INSERT OR REPLACE INTO stock_fundamentals
               (code, name, industry, data, updated_at, ttl_hours)
               VALUES (?,?,?,?,?,?)""",
            (
                code,
                name,
                industry,
                json.dumps(data_dict, ensure_ascii=False),
                domain.utc_now_iso(),
                ttl_hours,
            ),
        )
        conn.commit()
    return f"已缓存 {name}({code}) 行业:{industry} TTL:{ttl_hours}h"


def list_codes() -> list[str]:
    """返回所有基本面缓存中的股票代码（含过期），按更新时间倒序。"""
    with db.db_session() as conn:
        rows = conn.execute(
            "SELECT code FROM stock_fundamentals ORDER BY updated_at DESC"
        ).fetchall()
    return [r[0] for r in rows]
