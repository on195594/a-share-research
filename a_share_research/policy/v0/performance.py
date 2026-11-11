"""Deterministic, file-only account performance reporting."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
_MONEY = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?\Z")
_BENCHMARK_NAV = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_REQUIRED_OBSERVATION = {
    "valuation_at",
    "total_equity",
    "cash",
    "liabilities",
    "external_inflow",
    "external_outflow",
    "cashflow_coverage",
    "flow_timing",
    "reconciliation_status",
    "source_ref",
}
_REQUIRED_BENCHMARK = {
    "schema_version",
    "benchmark_id",
    "currency",
    "return_type",
    "source_ref",
    "effective_from",
    "observations",
}


class PerformanceInputError(ValueError):
    """Controlled input/schema failure, mapped to CLI exit code 2."""


class BenchmarkGap(ValueError):
    """A parseable benchmark that cannot be aligned with an account segment."""


@dataclass(frozen=True)
class Observation:
    valuation_at: datetime
    valuation_date: date
    equity: Decimal
    cash: Decimal | None
    inflow: Decimal
    outflow: Decimal
    coverage: str
    timing: str
    reconciliation: str
    source_ref: str


def _reject_constant(value: str) -> Any:
    raise PerformanceInputError(f"JSON不允许非有限数值: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PerformanceInputError(f"JSON重复键: {key}")
        result[key] = value
    return result


def loads_strict(raw: bytes | str) -> Any:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=Decimal,
        )
    except PerformanceInputError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PerformanceInputError(f"JSON输入非法: {exc}") from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PerformanceInputError(f"{label}必须是JSON对象")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerformanceInputError(f"{label}必须是非空字符串")
    return value


def _schema_version(value: Any, label: str) -> None:
    if type(value) is not int or value != 1:
        raise PerformanceInputError(f"{label}必须为整数1")


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not _MONEY.fullmatch(value):
        raise PerformanceInputError(f"{label}必须是最多两位小数的金额字符串")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PerformanceInputError(f"{label}金额非法") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        raise PerformanceInputError(f"{label}金额范围非法")
    return parsed


def _benchmark_decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not _BENCHMARK_NAV.fullmatch(value):
        raise PerformanceInputError(f"{label}必须是正Decimal字符串")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PerformanceInputError(f"{label}金额非法") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PerformanceInputError(f"{label}金额范围非法")
    return parsed


def _calendar_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise PerformanceInputError(f"{label}必须是YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PerformanceInputError(f"{label}日期非法") from exc


def _valuation_at(value: Any) -> tuple[datetime, date]:
    if not isinstance(value, str):
        raise PerformanceInputError("valuation_at必须是带时区的时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PerformanceInputError("valuation_at时间非法") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PerformanceInputError("valuation_at必须带时区")
    try:
        return parsed, parsed.astimezone(SHANGHAI).date()
    except (OverflowError, ValueError) as exc:
        raise PerformanceInputError("valuation_at超出上海日期范围") from exc


def _strict_dates(raw: Any, label: str) -> list[date]:
    if not isinstance(raw, list):
        raise PerformanceInputError(f"{label}必须是日期数组")
    dates = [
        _calendar_date(value, f"{label}[{index}]") for index, value in enumerate(raw)
    ]
    if dates != sorted(set(dates)):
        raise PerformanceInputError(f"{label}必须严格升序且唯一")
    return dates


def _parse_observation(raw: Any, index: int) -> Observation:
    row = _object(raw, f"observations[{index}]")
    missing = _REQUIRED_OBSERVATION - row.keys()
    if missing:
        raise PerformanceInputError(
            f"observations[{index}]缺少字段: {','.join(sorted(missing))}"
        )
    valuation_at, valuation_date = _valuation_at(row["valuation_at"])
    equity = _decimal(row["total_equity"], f"observations[{index}].total_equity")
    cash_raw = row["cash"]
    cash = (
        None if cash_raw is None else _decimal(cash_raw, f"observations[{index}].cash")
    )
    if cash is not None and cash > equity:
        raise PerformanceInputError(f"observations[{index}].cash不能超过total_equity")
    liabilities = _decimal(row["liabilities"], f"observations[{index}].liabilities")
    if liabilities != 0:
        raise PerformanceInputError("liabilities必须明确为0")
    inflow = _decimal(row["external_inflow"], f"observations[{index}].external_inflow")
    outflow = _decimal(
        row["external_outflow"], f"observations[{index}].external_outflow"
    )
    coverage = row["cashflow_coverage"]
    timing = row["flow_timing"]
    reconciliation = row["reconciliation_status"]
    if not isinstance(coverage, str) or coverage not in {"complete", "unknown"}:
        raise PerformanceInputError("cashflow_coverage取值非法")
    if not isinstance(timing, str) or timing not in {
        "none",
        "end_confirmed",
        "end_assumed",
        "unsupported",
    }:
        raise PerformanceInputError("flow_timing取值非法")
    if not isinstance(reconciliation, str) or reconciliation not in {
        "verified",
        "unverified",
        "conflicted",
    }:
        raise PerformanceInputError("reconciliation_status取值非法")
    _text(row["source_ref"], f"observations[{index}].source_ref")
    gross = inflow + outflow
    if (gross == 0) != (timing == "none"):
        raise PerformanceInputError("flow_timing必须与gross external flows一致")
    if timing == "end_confirmed" and coverage != "complete":
        raise PerformanceInputError("end_confirmed要求cashflow_coverage=complete")
    return Observation(
        valuation_at=valuation_at,
        valuation_date=valuation_date,
        equity=equity,
        cash=cash,
        inflow=inflow,
        outflow=outflow,
        coverage=coverage,
        timing=timing,
        reconciliation=reconciliation,
        source_ref=row["source_ref"],
    )


def parse_account(
    value: Any,
) -> tuple[dict[str, Any], list[Observation], list[date], bool]:
    account = _object(value, "account input")
    _schema_version(account.get("schema_version"), "schema_version")
    _text(account.get("account_scope"), "account_scope")
    if account.get("currency") != "CNY":
        raise PerformanceInputError("currency必须为CNY")
    if account.get("account_type") != "cash_long_only":
        raise PerformanceInputError("account_type必须为cash_long_only")
    if account.get("valuation_basis") != "broker_total_equity":
        raise PerformanceInputError("valuation_basis必须为broker_total_equity")
    _text(account.get("source_ref"), "source_ref")
    if (
        "expected_valuation_dates" in account
        and account["expected_valuation_dates"] is None
    ):
        raise PerformanceInputError("expected_valuation_dates必须是日期数组")
    raw_expected = account.get("expected_valuation_dates")
    expected = (
        []
        if raw_expected is None
        else _strict_dates(raw_expected, "expected_valuation_dates")
    )
    observations_raw = account.get("observations")
    if not isinstance(observations_raw, list) or not observations_raw:
        raise PerformanceInputError("observations必须是非空数组")
    observations = [
        _parse_observation(row, index) for index, row in enumerate(observations_raw)
    ]
    for row in observations_raw:
        for field in ("account_scope", "currency", "account_type", "valuation_basis"):
            if field in row and row[field] != account[field]:
                raise PerformanceInputError(
                    f"observations混合账户身份或计量口径: {field}"
                )
    times = [row.valuation_at for row in observations]
    dates = [row.valuation_date for row in observations]
    if times != sorted(times) or len(set(times)) != len(times):
        raise PerformanceInputError("observations必须严格按时间升序且唯一")
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise PerformanceInputError("同一上海日期不允许重复或冲突行")
    first = observations[0]
    if first.inflow != 0 or first.outflow != 0 or first.timing != "none":
        raise PerformanceInputError("首行只能是无外部流的基线")
    expected_has_source = bool(account.get("calendar_source_ref"))
    if "calendar_source_ref" in account and account["calendar_source_ref"] is not None:
        _text(account["calendar_source_ref"], "calendar_source_ref")
    return account, observations, expected, expected_has_source


def _parse_benchmark(value: Any) -> tuple[dict[str, Any], dict[date, Decimal]]:
    try:
        benchmark = _object(value, "benchmark")
        missing = _REQUIRED_BENCHMARK - benchmark.keys()
        if missing:
            raise BenchmarkGap(f"benchmark缺少字段: {','.join(sorted(missing))}")
        _schema_version(benchmark["schema_version"], "benchmark.schema_version")
        _text(benchmark["benchmark_id"], "benchmark_id")
        if benchmark["currency"] != "CNY":
            raise BenchmarkGap("benchmark_currency_mismatch")
        if not isinstance(benchmark["return_type"], str) or benchmark[
            "return_type"
        ] not in {"total_return", "price_return"}:
            raise BenchmarkGap("benchmark_return_type_invalid")
        _text(benchmark["source_ref"], "benchmark.source_ref")
        _calendar_date(benchmark["effective_from"], "benchmark.effective_from")
        rows = benchmark["observations"]
        if not isinstance(rows, list) or not rows:
            raise BenchmarkGap("benchmark_observations_missing")
        result: dict[date, Decimal] = {}
        previous: date | None = None
        for index, raw in enumerate(rows):
            row = _object(raw, f"benchmark.observations[{index}]")
            current = _calendar_date(
                row.get("date"), f"benchmark.observations[{index}].date"
            )
            nav = _benchmark_decimal(
                row.get("nav"), f"benchmark.observations[{index}].nav"
            )
            if previous is not None and current <= previous:
                raise BenchmarkGap("benchmark dates must be strictly ascending")
            result[current] = nav
            previous = current
        return benchmark, result
    except PerformanceInputError as exc:
        raise BenchmarkGap(str(exc)) from exc
    except TypeError as exc:
        raise BenchmarkGap(f"benchmark字段类型非法: {exc}") from exc


def _number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = max(
            50,
            len(value.as_tuple().digits) + 8,
            value.adjusted() + 3,
        )
        try:
            rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise PerformanceInputError("业绩结果超出JSON有限数值范围") from exc
    if rounded == rounded.to_integral_value():
        return int(rounded)
    try:
        result = float(rounded)
    except (OverflowError, ValueError) as exc:
        raise PerformanceInputError("业绩结果超出JSON有限数值范围") from exc
    if not math.isfinite(result):
        raise PerformanceInputError("业绩结果超出JSON有限数值范围")
    return result


def _missing_expected_dates(
    observations: list[Observation], expected: list[date]
) -> tuple[set[date], set[date]]:
    observed = {row.valuation_date for row in observations}
    missing = set(expected) - observed
    unexpected = observed - set(expected) if expected else set()
    return missing, unexpected


def _new_segment(row: Observation) -> dict[str, Any]:
    return {
        "start_date": row.valuation_date.isoformat(),
        "end_date": row.valuation_date.isoformat(),
        "status": "provisional" if row.reconciliation != "verified" else "exact",
        "return_pct": None,
        "max_drawdown_observed_pct": None,
        "daily_close_max_drawdown_pct": None,
        "daily_close_max_drawdown_reason": None,
        "benchmark_excess_return_pct": None,
        "_nav": Decimal("1"),
        "_return_ratio": None,
        "_peak": Decimal("1"),
        "_mdd": Decimal("0"),
        "_provisional": row.reconciliation != "verified",
        "_estimated": False,
        "_has_interval": False,
    }


def _finish_segment(segment: dict[str, Any]) -> dict[str, Any]:
    segment["status"] = (
        "provisional"
        if segment["_provisional"]
        else "estimated"
        if segment["_estimated"]
        else "exact"
    )
    segment["calculation_status"] = "estimated" if segment["_estimated"] else "exact"
    segment["return_pct"] = (
        _number((segment["_nav"] - 1) * 100) if segment["_has_interval"] else None
    )
    segment["_return_ratio"] = segment["_nav"] - 1 if segment["_has_interval"] else None
    segment["max_drawdown_observed_pct"] = (
        _number(segment["_mdd"] * 100) if segment["_has_interval"] else None
    )
    if not segment["_has_interval"]:
        segment["daily_close_max_drawdown_reason"] = "no_measurable_return_interval"
    for key in ("_nav", "_peak", "_mdd", "_provisional", "_estimated", "_has_interval"):
        del segment[key]
    return segment


def _benchmark_difference(
    segment: dict[str, Any],
    benchmark: dict[date, Decimal] | None,
    gaps: list[str],
    effective_from: date | None = None,
) -> None:
    if benchmark is None or segment["return_pct"] is None:
        return
    start = date.fromisoformat(segment["start_date"])
    end = date.fromisoformat(segment["end_date"])
    if effective_from is not None and start < effective_from:
        gaps.append(f"benchmark_effective_from_mismatch:{start}:{end}")
        return
    if start not in benchmark or end not in benchmark:
        gaps.append(
            f"benchmark_missing_aligned_date:{segment['start_date']}:{segment['end_date']}"
        )
        return
    account_return = segment["_return_ratio"]
    benchmark_return = benchmark[end] / benchmark[start] - 1
    segment["benchmark_excess_return_pct"] = _number(
        (account_return - benchmark_return) * 100
    )


def calculate_report(
    account: Any,
    benchmark: Any = None,
    *,
    allow_eod_flow_assumption: bool = False,
    input_hash: str = "",
    benchmark_hash: str | None = None,
    ledger: dict[str, Any] | None = None,
    benchmark_error: str | None = None,
) -> tuple[dict[str, Any], int]:
    account_obj, observations, expected, calendar_known = parse_account(account)
    benchmark_obj: dict[str, Any] | None = None
    benchmark_values: dict[date, Decimal] | None = None
    gaps: list[str] = []
    benchmark_requested = (
        benchmark is not None
        or benchmark_hash is not None
        or benchmark_error is not None
    )
    if benchmark is None and benchmark_hash is not None and benchmark_error is None:
        benchmark_error = "benchmark_missing_payload"
    if benchmark_error is not None:
        gaps.append(f"benchmark_invalid:{benchmark_error}")
    elif benchmark is not None:
        try:
            benchmark_obj, benchmark_values = _parse_benchmark(benchmark)
        except BenchmarkGap as exc:
            gaps.append(f"benchmark_invalid:{exc}")

    missing_expected, unexpected_dates = _missing_expected_dates(observations, expected)
    gaps.extend(
        f"missing_valuation_date:{value.isoformat()}"
        for value in sorted(missing_expected)
    )
    gaps.extend(
        f"unexpected_valuation_date:{value.isoformat()}"
        for value in sorted(unexpected_dates)
    )
    if not expected:
        gaps.append("valuation_calendar_unknown")
    elif not calendar_known:
        gaps.append("valuation_calendar_source_unknown")
    if observations[0].reconciliation != "verified":
        gaps.append(
            f"provisional_source:{observations[0].valuation_date.isoformat()}:{observations[0].reconciliation}"
        )

    with localcontext() as context:
        values = [
            value
            for row in observations
            for value in (row.equity, row.inflow, row.outflow)
        ]
        if benchmark_values:
            values.extend(benchmark_values.values())
        context.prec = max(
            50,
            max(len(value.as_tuple().digits) for value in values) + 28,
        )
        segments: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None
        previous = observations[0]
        for current in observations:
            if current is observations[0]:
                if current.equity > 0:
                    active = _new_segment(current)
                previous = current
                continue
            if current.reconciliation != "verified":
                gaps.append(
                    f"provisional_source:{current.valuation_date.isoformat()}:{current.reconciliation}"
                )
            date_gap = False
            if expected:
                # ponytail: O(rows * gaps); bisect missing dates if profiling warrants it.
                date_gap = any(
                    value > previous.valuation_date and value < current.valuation_date
                    for value in missing_expected
                )
            flow_gap: str | None = None
            if previous.equity == 0:
                if current.equity > 0:
                    active = _new_segment(current)
                previous = current
                continue
            if active is None:
                active = _new_segment(previous)
            if date_gap:
                flow_gap = "missing_valuation_date_between_observations"
            elif current.coverage != "complete":
                flow_gap = "cashflow_coverage_unknown"
            elif current.timing == "unsupported":
                flow_gap = "unsupported_cashflow_timing"
            elif current.timing == "end_assumed" and not allow_eod_flow_assumption:
                flow_gap = "end_assumed_requires_flag"
            if flow_gap:
                gaps.append(f"{flow_gap}:{current.valuation_date.isoformat()}")
                segments.append(_finish_segment(active))
                active = _new_segment(current) if current.equity > 0 else None
                previous = current
                continue
            flow = current.inflow - current.outflow
            after_flow = current.equity - flow
            if after_flow < 0:
                gaps.append(
                    f"invalid_interval_negative_after_flow:{current.valuation_date.isoformat()}"
                )
                segments.append(_finish_segment(active))
                active = _new_segment(current) if current.equity > 0 else None
                previous = current
                continue
            rate = after_flow / previous.equity - 1
            active["_nav"] *= 1 + rate
            active["_peak"] = max(active["_peak"], active["_nav"])
            active["_mdd"] = min(active["_mdd"], active["_nav"] / active["_peak"] - 1)
            active["end_date"] = current.valuation_date.isoformat()
            active["_has_interval"] = True
            active["_estimated"] = (
                active["_estimated"] or current.timing == "end_assumed"
            )
            active["_provisional"] = (
                active["_provisional"] or current.reconciliation != "verified"
            )
            if current.equity == 0:
                segments.append(_finish_segment(active))
                active = None
            previous = current
        if active is not None:
            segments.append(_finish_segment(active))
        for segment in segments:
            _benchmark_difference(
                segment,
                benchmark_values,
                gaps,
                date.fromisoformat(benchmark_obj["effective_from"])
                if benchmark_obj is not None
                else None,
            )
            del segment["_return_ratio"]

    if expected and calendar_known and not missing_expected and not unexpected_dates:
        for segment in segments:
            if segment["max_drawdown_observed_pct"] is not None:
                segment["daily_close_max_drawdown_pct"] = segment[
                    "max_drawdown_observed_pct"
                ]
                segment["daily_close_max_drawdown_reason"] = None
    else:
        for segment in segments:
            if segment["max_drawdown_observed_pct"] is not None:
                segment["daily_close_max_drawdown_reason"] = (
                    "valuation_calendar_unknown"
                    if not expected or not calendar_known
                    else "valuation_calendar_gap"
                )
    if ledger is not None:
        gaps.extend(ledger.get("gaps", []))
    if not any(segment["return_pct"] is not None for segment in segments):
        gaps.append("no_measurable_return_interval")

    for segment in segments:
        reasons = {
            "return_pct": "no_measurable_return_interval",
            "max_drawdown_observed_pct": "no_measurable_return_interval",
            "daily_close_max_drawdown_pct": segment.pop(
                "daily_close_max_drawdown_reason"
            )
            or "no_measurable_return_interval",
            "benchmark_excess_return_pct": (
                "benchmark_not_requested"
                if not benchmark_requested
                else "no_measurable_return_interval"
                if segment["return_pct"] is None
                else "benchmark_unavailable_see_gaps"
            ),
        }
        segment["null_reasons"] = {
            key: reason for key, reason in reasons.items() if segment[key] is None
        }

    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "account_performance",
        "account_scope": account_obj["account_scope"],
        "currency": "CNY",
        "method": "cash_flow_adjusted_time_weighted_return",
        "input_hash": input_hash,
        "coverage": {
            "status": "complete" if not gaps else "partial",
            "observed_from": observations[0].valuation_date.isoformat(),
            "observed_to": observations[-1].valuation_date.isoformat(),
            "daily_close": "complete"
            if expected
            and calendar_known
            and not missing_expected
            and not unexpected_dates
            else "unknown",
        },
        "gaps": sorted(set(gaps)),
        "segments": segments,
    }
    if benchmark_obj is not None:
        report["benchmark"] = {
            key: benchmark_obj[key]
            for key in (
                "benchmark_id",
                "currency",
                "return_type",
                "source_ref",
                "effective_from",
            )
        }
        report["benchmark"]["tradability_assessment"] = "not_performed"
        report["benchmark"]["comparison_kind"] = (
            "price_index_reference"
            if benchmark_obj["return_type"] == "price_return"
            else "total_return_reference"
        )
    if benchmark_hash is not None:
        report["benchmark_hash"] = benchmark_hash
    if ledger is not None:
        report["ledger_check"] = ledger
    return report, 4 if report["gaps"] else 0


def _ledger_check(database_path: Path) -> dict[str, Any]:
    if not database_path.exists():
        return {
            "status": "unavailable",
            "scope": "none",
            "account_mapping": "unavailable",
            "reconciliation": "not_checked",
            "gaps": ["ledger_database_missing"],
            "checked_rows": 0,
        }
    connection: sqlite3.Connection | None = None
    try:
        resolved = database_path.resolve()
        connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
        connection.execute("BEGIN")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "holding_events" not in tables:
            return {
                "status": "unavailable",
                "scope": "none",
                "account_mapping": "unavailable",
                "reconciliation": "not_checked",
                "gaps": ["ledger_history_table_missing"],
                "checked_rows": 0,
            }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(holding_events)")
        }
        if "inferred" not in columns:
            return {
                "status": "unavailable",
                "scope": "none",
                "account_mapping": "unavailable",
                "reconciliation": "not_checked",
                "gaps": ["ledger_history_schema_incomplete"],
                "checked_rows": 0,
            }
        count, inferred = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(inferred != 0), 0) FROM holding_events"
        ).fetchone()
        gaps = [
            "ledger_account_mapping_unavailable",
            "ledger_history_not_reconciled",
        ]
        if inferred:
            gaps.append("ledger_history_inferred")
        return {
            "status": "provisional",
            "scope": "limited",
            "account_mapping": "unavailable",
            "reconciliation": "not_reconciled",
            "gaps": gaps,
            "checked_rows": count,
            "inferred_rows": inferred,
        }
    except sqlite3.Error as exc:
        return {
            "status": "error",
            "gaps": [f"ledger_read_error:{exc.__class__.__name__}"],
            "checked_rows": 0,
        }
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()


def _dump(report: dict[str, Any]) -> None:
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _dump_text(report: dict[str, Any]) -> None:
    coverage = report["coverage"]
    print(f"account_performance: {report['account_scope']} ({report['currency']})")
    print(
        "coverage: "
        f"{coverage['status']} {coverage['observed_from']}..{coverage['observed_to']}"
    )
    if "benchmark" in report:
        benchmark = report["benchmark"]
        print(
            f"benchmark: {benchmark['benchmark_id']} ({benchmark['return_type']}; "
            f"{benchmark['comparison_kind']}; tradability not assessed)"
        )
    for segment in report["segments"]:
        metrics = " ".join(
            f"{key}={'N/A' if segment[key] is None else segment[key]}"
            for key in (
                "return_pct",
                "max_drawdown_observed_pct",
                "daily_close_max_drawdown_pct",
                "benchmark_excess_return_pct",
            )
        )
        print(
            f"segment {segment['start_date']}..{segment['end_date']}: "
            f"status={segment['status']} calculation_status={segment['calculation_status']} {metrics}"
        )
        for key, reason in segment["null_reasons"].items():
            print(f"  {key}: N/A ({reason})")
    if "ledger_check" in report:
        print(
            f"ledger_check: {report['ledger_check']['status']} (not account reconciliation)"
        )
    for gap in report["gaps"]:
        print(f"gap: {gap}")


def cmd_performance_report(
    argv: list[str], *, _database_path: Path | None = None
) -> int:
    """Report deterministic account performance from an external JSON file."""
    try:
        parser = argparse.ArgumentParser(prog="a-stock-cache performance-report")
        parser.add_argument("--input", required=True)
        parser.add_argument("--benchmark")
        parser.add_argument("--check-ledger", action="store_true")
        parser.add_argument("--allow-eod-flow-assumption", action="store_true")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(argv)
        input_path = Path(options.input)
        input_raw = input_path.read_bytes()
        account = loads_strict(input_raw)
        benchmark = None
        benchmark_error = None
        benchmark_raw = None
        if options.benchmark:
            benchmark_path = Path(options.benchmark)
            benchmark_raw = benchmark_path.read_bytes()
            try:
                benchmark = loads_strict(benchmark_raw)
                if benchmark is None:
                    benchmark_error = "benchmark must be an object, not null"
            except PerformanceInputError as exc:
                benchmark_error = str(exc)
        ledger = None
        if options.check_ledger:
            if _database_path is None:
                from a_share_research.policy.v0 import paths

                _database_path = paths.cache_db_path()
            ledger = _ledger_check(_database_path)
        report, code = calculate_report(
            account,
            benchmark,
            allow_eod_flow_assumption=options.allow_eod_flow_assumption,
            input_hash=hashlib.sha256(input_raw).hexdigest(),
            benchmark_hash=(
                hashlib.sha256(benchmark_raw).hexdigest()
                if benchmark_raw is not None
                else None
            ),
            ledger=ledger,
            benchmark_error=benchmark_error,
        )
        _dump(report) if options.json else _dump_text(report)
        return code
    except SystemExit as exc:
        return int(exc.code or 0)
    except (PerformanceInputError, ValueError) as exc:
        print(f"performance-report输入失败: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"performance-report运行失败: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"performance-report运行失败: {exc}", file=sys.stderr)
        return 1
