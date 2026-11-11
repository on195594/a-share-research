from __future__ import annotations

import json
import sqlite3

import pytest

from a_share_research.policy.v0 import cache, performance


def _account(
    *, equities=("100.00", "160.00"), flows=(("0.00", "0.00"), ("50.00", "0.00"))
):
    dates = ["2026-09-15", "2026-09-16"]
    observations = []
    for index, (equity, (inflow, outflow)) in enumerate(zip(equities, flows)):
        observations.append(
            {
                "valuation_at": f"{dates[index]}T16:00:00+08:00",
                "total_equity": equity,
                "cash": "0.00",
                "liabilities": "0.00",
                "external_inflow": inflow,
                "external_outflow": outflow,
                "cashflow_coverage": "complete",
                "flow_timing": "none"
                if inflow == outflow == "0.00"
                else "end_confirmed",
                "reconciliation_status": "verified",
                "source_ref": f"fixture:day-{index + 1}",
            }
        )
    return {
        "schema_version": 1,
        "account_scope": "fixture-account",
        "currency": "CNY",
        "account_type": "cash_long_only",
        "valuation_basis": "broker_total_equity",
        "source_ref": "fixture:statement",
        "expected_valuation_dates": dates,
        "calendar_source_ref": "fixture:calendar",
        "observations": observations,
    }


def _report(account, **kwargs):
    return performance.calculate_report(account, **kwargs)[0]


def test_p01_and_p02_use_gross_flows_and_decimal_math():
    assert _report(_account())["segments"][0]["return_pct"] == 10.0
    account = _account(
        equities=("160.00", "150.00"), flows=(("0.00", "0.00"), ("0.00", "10.00"))
    )
    assert _report(account)["segments"][0]["return_pct"] == 0.0


def test_p08_drawdown_uses_normalized_chain():
    account = _account(
        equities=("100.00", "110.00"), flows=(("0.00", "0.00"), ("0.00", "0.00"))
    )
    account["observations"].append(
        {
            **account["observations"][1],
            "valuation_at": "2026-09-17T16:00:00+08:00",
            "total_equity": "99.00",
            "source_ref": "fixture:day-3",
        }
    )
    account["expected_valuation_dates"].append("2026-09-17")
    result = _report(account)
    assert result["segments"][0]["return_pct"] == -1.0
    assert result["segments"][0]["max_drawdown_observed_pct"] == -10.0


def test_eod_assumption_is_opt_in_and_estimated():
    account = _account()
    account["observations"][1]["flow_timing"] = "end_assumed"
    without_flag, code = performance.calculate_report(account)
    assert code == 4
    assert without_flag["segments"][0]["return_pct"] is None
    with_flag, code = performance.calculate_report(
        account, allow_eod_flow_assumption=True
    )
    assert code == 0
    assert with_flag["segments"][0]["status"] == "estimated"
    assert with_flag["segments"][0]["return_pct"] == 10.0


def test_unsupported_flow_and_missing_calendar_are_business_gaps():
    account = _account()
    account["observations"][1]["flow_timing"] = "unsupported"
    result, code = performance.calculate_report(account)
    assert code == 4
    assert result["segments"][0]["return_pct"] is None

    observed_only = _account()
    observed_only.pop("expected_valuation_dates")
    observed_only.pop("calendar_source_ref")
    result, code = performance.calculate_report(observed_only)
    assert code == 4
    assert result["segments"][0]["return_pct"] == 10.0
    assert result["segments"][0]["daily_close_max_drawdown_pct"] is None


def test_zero_asset_restart_is_a_new_segment():
    account = _account(
        equities=("100.00", "0.00"), flows=(("0.00", "0.00"), ("0.00", "100.00"))
    )
    account["expected_valuation_dates"].append("2026-09-17")
    account["observations"].append(
        {
            **account["observations"][1],
            "valuation_at": "2026-09-17T16:00:00+08:00",
            "total_equity": "55.00",
            "external_inflow": "50.00",
            "external_outflow": "0.00",
            "flow_timing": "end_confirmed",
            "source_ref": "fixture:day-3",
        }
    )
    account["expected_valuation_dates"].append("2026-09-18")
    account["observations"].append(
        {
            **account["observations"][1],
            "valuation_at": "2026-09-18T16:00:00+08:00",
            "total_equity": "60.00",
            "external_inflow": "0.00",
            "external_outflow": "0.00",
            "flow_timing": "none",
            "source_ref": "fixture:day-4",
        }
    )
    result = _report(account)
    assert [segment["return_pct"] for segment in result["segments"]] == [0.0, 9.09]


def test_unknown_extensions_do_not_create_double_adjustments():
    account = _account()
    account["observations"][1]["dividend"] = "50.00"
    account["observations"][1]["positions"] = [{"symbol": "fixture"}]
    assert _report(account)["segments"][0]["return_pct"] == 10.0


@pytest.mark.parametrize(
    "field", ["cashflow_coverage", "flow_timing", "reconciliation_status"]
)
@pytest.mark.parametrize("value", [[], {}])
def test_enum_arrays_and_objects_are_controlled_input_errors(field, value):
    account = _account()
    account["observations"][1][field] = value
    with pytest.raises(performance.PerformanceInputError):
        performance.calculate_report(account)


def test_benchmark_return_type_arrays_and_objects_keep_absolute_report():
    for return_type in ([], {}):
        benchmark = {
            "schema_version": 1,
            "benchmark_id": "fixture-index",
            "currency": "CNY",
            "return_type": return_type,
            "source_ref": "fixture:benchmark",
            "effective_from": "2026-09-15",
            "observations": [
                {"date": "2026-09-15", "nav": "100.000000"},
                {"date": "2026-09-16", "nav": "101.123456"},
            ],
        }
        result, code = performance.calculate_report(_account(), benchmark)
        assert code == 4
        assert result["segments"][0]["return_pct"] == 10.0
        assert result["segments"][0]["benchmark_excess_return_pct"] is None


def test_benchmark_nav_accepts_precision_beyond_account_money():
    benchmark = {
        "schema_version": 1,
        "benchmark_id": "fixture-index",
        "currency": "CNY",
        "return_type": "total_return",
        "source_ref": "fixture:benchmark",
        "effective_from": "2026-09-15",
        "observations": [
            {"date": "2026-09-15", "nav": "100.123456"},
            {"date": "2026-09-16", "nav": "101.123456"},
        ],
    }
    result, code = performance.calculate_report(_account(), benchmark)
    assert code == 0
    assert result["segments"][0]["benchmark_excess_return_pct"] == 9.0


def test_benchmark_effective_date_mismatch_suppresses_comparison():
    benchmark = {
        "schema_version": 1,
        "benchmark_id": "fixture-index",
        "currency": "CNY",
        "return_type": "total_return",
        "source_ref": "fixture:benchmark",
        "effective_from": "2026-09-16",
        "observations": [
            {"date": "2026-09-15", "nav": "100"},
            {"date": "2026-09-16", "nav": "101"},
        ],
    }
    result, code = performance.calculate_report(_account(), benchmark)
    assert code == 4
    assert result["segments"][0]["return_pct"] == 10.0
    assert result["segments"][0]["benchmark_excess_return_pct"] is None
    assert any(
        gap.startswith("benchmark_effective_from_mismatch:") for gap in result["gaps"]
    )


def test_provisional_source_is_not_presented_as_exact():
    account = _account()
    account["observations"][0]["reconciliation_status"] = "unverified"
    result, code = performance.calculate_report(account)
    assert code == 4
    assert result["segments"][0]["status"] == "provisional"


def test_zero_withdrawal_and_zero_loss_are_distinct():
    withdrawal = _account(
        equities=("100.00", "0.00"), flows=(("0.00", "0.00"), ("0.00", "100.00"))
    )
    loss = _account(
        equities=("100.00", "0.00"), flows=(("0.00", "0.00"), ("0.00", "0.00"))
    )
    assert _report(withdrawal)["segments"][0]["return_pct"] == 0.0
    assert _report(loss)["segments"][0]["return_pct"] == -100.0


def test_missing_expected_date_breaks_chain_and_keeps_observed_only_mdd():
    account = _account()
    account["observations"][1]["valuation_at"] = "2026-09-17T16:00:00+08:00"
    account["expected_valuation_dates"] = ["2026-09-15", "2026-09-16", "2026-09-17"]
    result, code = performance.calculate_report(account)
    assert code == 4
    assert result["segments"][0]["return_pct"] is None
    assert result["segments"][0]["daily_close_max_drawdown_pct"] is None
    assert "missing_valuation_date:2026-09-16" in result["gaps"]


def test_unknown_calendar_does_not_fabricate_unexpected_dates():
    account = _account()
    account.pop("expected_valuation_dates")
    account.pop("calendar_source_ref")
    result, code = performance.calculate_report(account)
    assert code == 4
    assert "valuation_calendar_unknown" in result["gaps"]
    assert not any(
        gap.startswith("unexpected_valuation_date:") for gap in result["gaps"]
    )


def test_single_observation_has_no_measurable_drawdown():
    account = _account()
    account["observations"] = account["observations"][:1]
    account["expected_valuation_dates"] = account["expected_valuation_dates"][:1]
    result, code = performance.calculate_report(account)
    assert code == 4
    assert "no_measurable_return_interval" in result["gaps"]
    assert result["segments"][0]["return_pct"] is None
    assert result["segments"][0]["max_drawdown_observed_pct"] is None
    assert result["segments"][0]["daily_close_max_drawdown_pct"] is None
    assert (
        result["segments"][0]["null_reasons"]["daily_close_max_drawdown_pct"]
        == "no_measurable_return_interval"
    )


def test_strict_json_rejects_duplicate_keys_and_nonfinite():
    with pytest.raises(performance.PerformanceInputError):
        performance.loads_strict(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(performance.PerformanceInputError):
        performance.loads_strict(b'{"x":NaN}')


def test_gross_flow_zero_and_invalid_interval_are_rejected():
    account = _account()
    account["observations"][1]["external_inflow"] = "50.00"
    account["observations"][1]["external_outflow"] = "50.00"
    account["observations"][1]["flow_timing"] = "none"
    with pytest.raises(performance.PerformanceInputError):
        performance.calculate_report(account)

    invalid = _account()
    invalid["observations"][1]["liabilities"] = "1.00"
    with pytest.raises(performance.PerformanceInputError):
        performance.calculate_report(invalid)

    duplicate_day = _account()
    duplicate_day["observations"][1]["valuation_at"] = "2026-09-15T17:00:00+08:00"
    with pytest.raises(performance.PerformanceInputError):
        performance.calculate_report(duplicate_day)


def test_repeated_execution_is_deterministic():
    account = _account()
    assert json.dumps(_report(account), sort_keys=True) == json.dumps(
        _report(account), sort_keys=True
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_equity", True),
        ("cash", "101.00"),
        ("liabilities", "1.00"),
        ("external_inflow", "-1.00"),
        ("external_outflow", "NaN"),
        ("total_equity", "100.001"),
        ("valuation_at", "2026-09-15T16:00:00"),
        ("valuation_at", "9999-12-31T23:59:59-12:00"),
    ],
)
def test_bad_money_and_dates_are_controlled_at_public_cli(
    field, value, tmp_path, capsys
):
    account = _account()
    account["observations"][0][field] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(account))
    assert cache.main(["performance-report", "--input", str(path), "--json"]) == 2
    assert capsys.readouterr().out == ""


def test_duplicate_shanghai_date_is_rejected():
    account = _account()
    account["observations"][1]["valuation_at"] = "2026-09-15T09:00:00+00:00"
    with pytest.raises(performance.PerformanceInputError, match="上海日期"):
        performance.calculate_report(account)


def test_repeat_file_cli_is_byte_deterministic_and_does_not_change_input(
    tmp_path, capsys
):
    path = tmp_path / "account.json"
    raw = json.dumps(_account()).encode()
    path.write_bytes(raw)
    args = ["performance-report", "--input", str(path), "--json"]
    assert cache.main(args) == 0
    first = capsys.readouterr().out
    assert cache.main(args) == 0
    assert capsys.readouterr().out == first
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_account_is_controlled_and_stdout_stays_empty(tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert cache.main(["performance-report", "--input", str(path), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


def test_file_only_cli_does_not_resolve_database_path(tmp_path, monkeypatch, capsys):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(_account()), encoding="utf-8")

    def fail_database_path():
        raise AssertionError("file-only performance command resolved database path")

    monkeypatch.setattr("a_share_research.policy.v0.paths.cache_db_path", fail_database_path)
    assert cache.main(["performance-report", "--input", str(path), "--json"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert captured.err == ""


def test_default_cli_output_is_text(tmp_path, capsys):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(_account()), encoding="utf-8")
    assert cache.main(["performance-report", "--input", str(path)]) == 0
    output = capsys.readouterr().out
    assert not output.lstrip().startswith("{")
    assert "account_performance" in output


def test_invalid_benchmark_json_keeps_absolute_report(tmp_path, capsys):
    account_path = tmp_path / "account.json"
    benchmark_path = tmp_path / "benchmark.json"
    account_path.write_text(json.dumps(_account()), encoding="utf-8")
    benchmark_path.write_text("{not-json", encoding="utf-8")
    assert (
        cache.main(
            [
                "performance-report",
                "--input",
                str(account_path),
                "--benchmark",
                str(benchmark_path),
                "--json",
            ]
        )
        == 4
    )
    report = json.loads(capsys.readouterr().out)
    assert report["segments"][0]["return_pct"] == 10.0
    assert report["segments"][0]["benchmark_excess_return_pct"] is None
    assert any(gap.startswith("benchmark_invalid:") for gap in report["gaps"])


def test_input_file_oserror_is_exit_one(tmp_path, capsys):
    assert (
        cache.main(["performance-report", "--input", str(tmp_path / "missing.json")])
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


def test_check_ledger_missing_database_is_explicit_gap(tmp_path, monkeypatch, capsys):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(_account()), encoding="utf-8")
    database = tmp_path / "missing.db"
    monkeypatch.setenv("CACHE_DB_PATH", str(database))
    assert (
        cache.main(
            ["performance-report", "--input", str(path), "--check-ledger", "--json"]
        )
        == 4
    )
    report = json.loads(capsys.readouterr().out)
    assert report["ledger_check"]["status"] == "unavailable"
    assert report["ledger_check"]["account_mapping"] == "unavailable"
    assert "ledger_database_missing" in report["gaps"]


def test_check_ledger_existing_database_without_schema_is_explicit_gap(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(_account()), encoding="utf-8")
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()
    monkeypatch.setenv("CACHE_DB_PATH", str(database))
    assert (
        cache.main(
            ["performance-report", "--input", str(path), "--check-ledger", "--json"]
        )
        == 4
    )
    report = json.loads(capsys.readouterr().out)
    assert report["ledger_check"]["status"] == "unavailable"
    assert report["ledger_check"]["account_mapping"] == "unavailable"
    assert "ledger_history_table_missing" in report["gaps"]


def test_check_ledger_marks_inferred_history_as_limited_provisional(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "account.json"
    path.write_text(json.dumps(_account()), encoding="utf-8")
    database = tmp_path / "history.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE holding_events (id INTEGER PRIMARY KEY, inferred INTEGER NOT NULL)"
    )
    connection.execute("INSERT INTO holding_events (inferred) VALUES (1)")
    connection.commit()
    connection.close()
    monkeypatch.setenv("CACHE_DB_PATH", str(database))
    assert (
        cache.main(
            ["performance-report", "--input", str(path), "--check-ledger", "--json"]
        )
        == 4
    )
    report = json.loads(capsys.readouterr().out)
    ledger = report["ledger_check"]
    assert ledger["status"] == "provisional"
    assert ledger["scope"] == "limited"
    assert ledger["account_mapping"] == "unavailable"
    assert ledger["reconciliation"] == "not_reconciled"
    assert ledger["inferred_rows"] == 1
    assert "ledger_history_inferred" in report["gaps"]
    assert "ledger_account_mapping_verified" not in ledger


def test_extreme_decimal_values_remain_json_finite():
    huge = "9" * 400
    account = _account(equities=("1", huge), flows=(("0.00", "0.00"), ("0.00", "0.00")))
    result, code = performance.calculate_report(account)
    assert code == 0
    encoded = json.dumps(result, allow_nan=False)
    assert "Infinity" not in encoded


@pytest.mark.parametrize(
    "raw", ["null", "", "[]", '{"schema_version":1,"schema_version":1}']
)
def test_requested_invalid_benchmark_never_looks_unrequested(raw, tmp_path, capsys):
    account_path = tmp_path / "account.json"
    benchmark_path = tmp_path / "benchmark.json"
    account_path.write_text(json.dumps(_account()))
    benchmark_path.write_text(raw)
    code = cache.main(
        [
            "performance-report",
            "--input",
            str(account_path),
            "--benchmark",
            str(benchmark_path),
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 4
    assert report["segments"][0]["return_pct"] == 10
    assert report["segments"][0]["benchmark_excess_return_pct"] is None
    assert any(gap.startswith("benchmark_invalid") for gap in report["gaps"])
    assert report["benchmark_hash"]


def test_price_reference_is_supported_and_named_not_total_return(capsys):
    benchmark = {
        "schema_version": 1,
        "benchmark_id": "fixture-price-index",
        "currency": "CNY",
        "return_type": "price_return",
        "source_ref": "fixture:benchmark",
        "effective_from": "2000-01-01",
        "observations": [
            {"date": "2026-09-15", "nav": "100.0000"},
            {"date": "2026-09-16", "nav": "101.0000"},
        ],
    }
    report, code = performance.calculate_report(_account(), benchmark)
    assert code == 0
    assert report["benchmark"]["return_type"] == "price_return"
    assert report["benchmark"]["comparison_kind"] == "price_index_reference"
    assert report["benchmark"]["tradability_assessment"] == "not_performed"
    assert report["segments"][0]["benchmark_excess_return_pct"] == 9
    performance._dump_text(report)
    text = capsys.readouterr().out
    assert "fixture-price-index" in text and "price_return" in text
    assert "benchmark_excess_return_pct=9" in text


def test_provisional_source_does_not_hide_approximate_flow_math():
    account = _account()
    account["observations"][1].update(
        flow_timing="end_assumed", reconciliation_status="unverified"
    )
    report, code = performance.calculate_report(account, allow_eod_flow_assumption=True)
    assert code == 4
    assert report["segments"][0]["status"] == "provisional"
    assert report["segments"][0]["calculation_status"] == "estimated"


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_scope", "other"),
        ("currency", "USD"),
        ("account_type", "margin"),
        ("valuation_basis", "reconstructed"),
    ],
)
def test_explicit_row_identity_conflicts_are_not_ignored(field, value):
    account = _account()
    account["observations"][1][field] = value
    with pytest.raises(performance.PerformanceInputError, match="混合"):
        performance.calculate_report(account)


def test_ledger_uri_cannot_be_changed_by_a_literal_filename(tmp_path):
    path = tmp_path / "ledger?mode=rwc&other=fixture"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE holding_events (inferred INTEGER)")
        connection.execute("INSERT INTO holding_events VALUES (1)")
    result = performance._ledger_check(path)
    assert result["checked_rows"] == 1
    assert result["inferred_rows"] == 1
    assert not (tmp_path / "ledger").exists()


def test_benchmark_effectivity_is_checked_per_segment():
    account = _account(
        equities=("100.00", "0.00"), flows=(("0.00", "0.00"), ("0.00", "100.00"))
    )
    for day, equity, inflow in [("17", "55.00", "50.00"), ("18", "60.00", "0.00")]:
        account["expected_valuation_dates"].append(f"2026-09-{day}")
        account["observations"].append(
            {
                **account["observations"][1],
                "valuation_at": f"2026-09-{day}T16:00:00+08:00",
                "total_equity": equity,
                "external_inflow": inflow,
                "external_outflow": "0.00",
                "flow_timing": "none" if inflow == "0.00" else "end_confirmed",
            }
        )
    benchmark = {
        "schema_version": 1,
        "benchmark_id": "fixture-index",
        "currency": "CNY",
        "return_type": "total_return",
        "source_ref": "fixture:benchmark",
        "effective_from": "2026-09-17",
        "observations": [
            {"date": "2026-09-17", "nav": "100"},
            {"date": "2026-09-18", "nav": "101"},
        ],
    }
    report, code = performance.calculate_report(account, benchmark)
    assert code == 4
    assert report["segments"][0]["benchmark_excess_return_pct"] is None
    assert report["segments"][1]["benchmark_excess_return_pct"] == 8.09
    assert "benchmark_effective_from_mismatch:2026-09-15:2026-09-16" in report["gaps"]


def test_broker_net_equity_already_includes_dividends_fees_and_taxes():
    account = _account(
        equities=("100.00", "105.00"), flows=(("0.00", "0.00"), ("0.00", "0.00"))
    )
    account["observations"][1].update(dividends="10.00", fees="3.00", taxes="2.00")
    assert _report(account)["segments"][0]["return_pct"] == 5


def test_stock_split_does_not_mix_adjusted_prices_with_broker_equity():
    account = _account(
        equities=("100.00", "100.00"), flows=(("0.00", "0.00"), ("0.00", "0.00"))
    )
    account["observations"][0]["positions"] = [{"shares": 50, "price": "2.00"}]
    account["observations"][1].update(
        positions=[{"shares": 100, "price": "1.00"}],
        qfq_price="0.50",
        corporate_action="split",
    )
    assert _report(account)["segments"][0]["return_pct"] == 0


def test_missing_history_begins_only_at_sourced_baseline():
    account = _account()
    account["source_ref"] = "fixture:verified_baseline_after_unknown_history"
    report, code = performance.calculate_report(account)
    assert code == 0
    assert report["coverage"]["observed_from"] == "2026-09-15"
    assert len(report["segments"]) == 1
    assert report["segments"][0]["start_date"] == "2026-09-15"


def test_declared_source_conflict_stays_provisional_with_unmapped_ledger(
    tmp_path, capsys
):
    account = _account()
    account["observations"][1]["reconciliation_status"] = "conflicted"
    path = tmp_path / "account.json"
    path.write_text(json.dumps(account))
    database = tmp_path / "ledger.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE holding_events (inferred INTEGER)")
        connection.execute("INSERT INTO holding_events VALUES (0)")
    connection.close()
    code = performance.cmd_performance_report(
        ["--input", str(path), "--check-ledger", "--json"], _database_path=database
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 4
    assert report["segments"][0]["status"] == "provisional"
    assert report["segments"][0]["return_pct"] == 10
    assert "provisional_source:2026-09-16:conflicted" in report["gaps"]
    assert report["ledger_check"]["account_mapping"] == "unavailable"
    assert report["ledger_check"]["reconciliation"] == "not_reconciled"


def test_benchmark_alignment_gap_keeps_absolute_report():
    benchmark = {
        "schema_version": 1,
        "benchmark_id": "fixture-index",
        "currency": "CNY",
        "return_type": "total_return",
        "source_ref": "fixture:benchmark",
        "effective_from": "2026-09-15",
        "observations": [
            {"date": "2026-09-15", "nav": "100.00"},
            {"date": "2026-09-17", "nav": "101.00"},
        ],
    }
    result, code = performance.calculate_report(_account(), benchmark)
    assert code == 4
    assert result["segments"][0]["return_pct"] == 10.0
    assert result["segments"][0]["benchmark_excess_return_pct"] is None
