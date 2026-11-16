"""Real disclosure captures; explicitly marked variants are not investment evidence."""
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from a_share_research import companies, disclosure_checks as checks, forecasts

DATA = Path(__file__).with_name("data")
CAPTURE = json.loads((DATA / "disclosure-checks.json").read_text())
ROWS = CAPTURE["tushare"]


def body(report_id):
    # The capture projects body.stance into action and trigger columns.
    report = copy.deepcopy(next(r for r in CAPTURE["reports"] if r["report_id"] == report_id))
    report["stance"] = {"action": report["stance"], "trigger": report.pop("stance_trigger")}
    return report


def shifted_item(report_id=156, index=0):
    # Explicit period-shift variant: frozen 2026 items paired with 2025 disclosures.
    # Cross-company pairings below exercise arithmetic only, not report performance.
    return checks.items(body(report_id))[index] | {"period": "2025-12-31"}


def forecast(code="603960.SH"):
    return checks.normalize_forecast(ROWS["forecast"][code], code[:6])[0]


def test_forecast_units_identity_and_duplicates():
    rows = ROWS["forecast"]["603960.SH"]
    observation, = checks.normalize_forecast(rows + rows, "603960")
    assert observation == {
        "source": "forecast", "period": "2025-12-31", "announced": "2026-01-23",
        "identity": "forecast:603960.SH:20251231:20260123",
        "values": {"T.net_profit": {"low": Decimal("0.2691"), "high": Decimal("0.3341")}},
        "type": "预减", "first_announced": "2026-01-23",
    }
    # Conflicting same-version numeric variant must not silently overwrite evidence.
    changed = rows[0] | {"net_profit_min": 2692}
    with pytest.raises(companies.SourceError, match="冲突"):
        checks.normalize_forecast(rows + [changed], "603960")


@pytest.mark.parametrize("source,code", [("forecast", "603960.SH"), ("express", "605016.SH")])
def test_wrong_company_rejected(source, code):
    kwargs = {"revenue_basis_matches": True} if source == "express" else {}
    with pytest.raises(companies.SourceError, match="其他公司"):
        getattr(checks, f"normalize_{source}")(ROWS[source][code], "000001", **kwargs)


@pytest.mark.parametrize("low,high,outcome,direction", [
    (18, 22, "inside", None), (1, 2, "deviated", "above"),
    (22, 23, "deviated", "below"), (20, 22, "undetermined", None),
    (18.5, 21.5, "inside", None), (21.5, 23, "undetermined", None),
])
def test_forecast_interval_relations(low, high, outcome, direction):
    # Threshold-only variants of report 156, against real 688256 interval [18.5, 21.5].
    item = shifted_item() | {"low": low, "high": high}
    result = checks.judge(item, forecast("688256.SH"))
    assert (result["outcome"], result["direction"], result["reason"]) == (outcome, direction, None)
    assert result["observed"] == {"low": "18.5", "high": "21.5"}
    assert {k: result[k] for k in ("ref", "kind", "metric", "period")} == {
        k: item[k] for k in ("ref", "kind", "metric", "period")}


@pytest.mark.parametrize("missing,low,high,outcome,direction", [
    ("net_profit_max", 1, 2, "deviated", "above"),
    ("net_profit_min", 22, 23, "deviated", "below"),
    ("net_profit_max", 18, 22, "undetermined", None),
    ("net_profit_min", 18, 22, "undetermined", None),
])
def test_one_sided_variants(missing, low, high, outcome, direction):
    # Missing-bound variant of the captured revision; NaN follows DataFrame records.
    row = ROWS["forecast"]["688256.SH"][0] | {missing: float("nan")}
    observation, = checks.normalize_forecast([row], "688256")
    result = checks.judge(shifted_item() | {"low": low, "high": high}, observation)
    assert (result["outcome"], result["direction"]) == (outcome, direction)


def test_no_numbers_and_noncovering_observations():
    observation = forecast("688082.SH")
    assert observation["type"] == "不确定"
    result = checks.judge(shifted_item(), observation)
    assert result["outcome"] == "undetermined"
    assert result["reason"] == "预告未给出数值区间"
    assert result["observed"] == {"low": None, "high": None}
    assert checks.judge(checks.items(body(156))[0], observation) is None
    assert checks.judge(shifted_item(53, 0), observation) is None
    # Percentage-only variant must not generate profit bounds.
    row = ROWS["forecast"]["603960.SH"][0] | {"net_profit_min": None, "net_profit_max": float("nan")}
    assert checks.normalize_forecast([row], "603960")[0]["values"] == observation["values"]


@pytest.mark.parametrize("code,dates", [
    ("300024.SZ", ["2026-01-14", "2026-01-30"]),
    ("688256.SH", ["2025-08-29", "2026-01-31"]),
])
def test_revisions_and_latest(code, dates):
    observations = checks.normalize_forecast(ROWS["forecast"][code], code[:6])
    assert sorted(o["announced"] for o in observations) == dates
    results = [checks.judge(shifted_item(), o) for o in observations]
    original = copy.deepcopy(results)
    assert checks.latest(results) == checks.latest(list(reversed(results))) == [results[0]]
    assert results[1]["outcome"] == "undetermined"
    # The 688256 revision partially overlaps the frozen interval; it remains
    # undetermined, but now carries numbers and supersedes the numberless version.
    assert results[0]["outcome"] == ("deviated" if code == "300024.SZ" else "undetermined")
    assert results[0]["observed"]["low"] is not None
    assert results[0]["reason"] is None
    assert results == original


@pytest.mark.parametrize("code,profit,revenue", [
    ("605016.SH", "3.657832", "13.790537"), ("601009.SH", "218.07", "555.4"),
])
def test_express_both_metrics_and_income_basis(code, profit, revenue):
    assert checks.revenue_basis_matches(ROWS["income"][code]) is True
    observation, = checks.normalize_express(ROWS["express"][code], code[:6], revenue_basis_matches=True)
    for metric, expected in (("T.net_profit", profit), ("T.revenue", revenue)):
        assert observation["values"][metric] == {"low": Decimal(expected), "high": Decimal(expected)}
        # Metric/interval variant isolates exact closed-point comparison.
        item = shifted_item() | {"metric": metric, "low": expected, "high": expected}
        assert checks.judge(item, observation)["outcome"] == "inside"
    assert observation["identity"] == f"express:{code}:20251231:20260123"


@pytest.mark.parametrize("basis", [False, None])
def test_express_basis_warning_is_metric_local(basis):
    observation, = checks.normalize_express(ROWS["express"]["605016.SH"], "605016", revenue_basis_matches=basis)
    result = checks.judge(shifted_item(53, 0), observation)
    assert result["outcome"] == "undetermined"
    assert result["reason"] == "口径不符：快报营业收入不等于营业总收入口径"
    assert result["observed"]["low"] == "13.790537"
    assert checks.judge(shifted_item(), observation)["outcome"] == "deviated"


def test_income_basis_missing_mismatch_and_latest_variants():
    row = ROWS["income"]["601009.SH"][0]
    # Minimal basis, missing-value, period and report-type variants.
    mismatch = row | {"revenue": row["revenue"] + 1}
    assert checks.revenue_basis_matches([mismatch]) is False
    assert checks.revenue_basis_matches([row | {"revenue": None}]) is None
    assert checks.revenue_basis_matches([row | {"total_revenue": float("nan")}]) is None
    assert checks.revenue_basis_matches([row | {"report_type": "2"}]) is None
    assert checks.revenue_basis_matches([row, mismatch | {"end_date": "20241231"}]) is True
    assert checks.revenue_basis_matches([row | {"end_date": "20241231"}, mismatch]) is False
    assert checks.revenue_basis_matches([row, mismatch]) is None


def test_items_order_action_gating_and_no_mutation():
    for report_id, count in ((53, 3), (156, 4), (90, 4), (86, 3), (88, 3), (166, 2)):
        report = body(report_id)
        original = copy.deepcopy(report)
        extracted = checks.items(report)
        assert len(extracted) == count
        assert extracted[0]["ref"] == "checkpoints[0]"
        assert report == original
    report = body(156)
    assert checks.items(report)[-1]["kind"] == "add_trigger"
    # Action-only variant must suppress an otherwise valid add trigger.
    report["stance"]["action"] = "等价格"
    assert len(checks.items(report)) == 3


@pytest.mark.parametrize("report_id", [86, 88])
def test_legacy_trigger_precedence_and_fallback(report_id):
    report = body(report_id)
    trigger = checks.items(report)[-1]
    assert trigger["ref"] == trigger["kind"] == "trigger"
    # Different fallback value makes precedence observable.
    report["stance"]["trigger"]["value"] += 1
    assert checks.items(report)[-1] == trigger
    del report["stance_calc"]["trigger"]
    assert checks.items(report)[-1]["value"] == trigger["value"] + 1
    # An explicitly present null is not replaced by fallback (V1 precedence).
    report["stance_calc"]["trigger"] = None
    assert all(i["kind"] == "forecast" for i in checks.items(report))


@pytest.mark.parametrize("patch", [{"low": None}, {"low": True}, {"high": float("nan")},
                                   {"low": 100}, {"confidence": 42}, {"metric": []}, {"period": "bad"}])
def test_malformed_forecast_variants_skipped(patch):
    report = body(156)
    report["checkpoints"][0]["forecast"].update(patch)
    assert "checkpoints[0]" not in [i["ref"] for i in checks.items(report)]


@pytest.mark.parametrize("patch", [{"value": None}, {"value": "45"}, {"op": ">"}, {"extra": 1}, {"period": "bad"}])
def test_malformed_trigger_variants_skipped(patch):
    report = body(156)
    report["stance_calc"]["add_trigger"].update(patch)
    assert all(i["kind"] == "forecast" for i in checks.items(report))


@pytest.mark.parametrize("op,value,outcome", [
    (">=", 18.5, "met"), (">=", 22, "missed"), (">=", 20, "undetermined"),
    ("<=", 21.5, "met"), ("<=", 18, "missed"), ("<=", 20, "undetermined"),
])
@pytest.mark.parametrize("report_id", [156, 86])
def test_trigger_interval_variants(report_id, op, value, outcome):
    # Metric/threshold variants of real add/legacy triggers against real profit bounds.
    item = shifted_item(report_id, -1) | {"metric": "T.net_profit", "op": op, "value": value}
    result = checks.judge(item, forecast("688256.SH"))
    assert result["outcome"] == outcome
    assert result["direction"] is None


@pytest.mark.parametrize("missing,op,value,outcome", [
    ("net_profit_max", ">=", 18.5, "met"),
    ("net_profit_min", ">=", 22, "missed"),
    ("net_profit_min", "<=", 21.5, "met"),
    ("net_profit_max", "<=", 18, "missed"),
    ("net_profit_max", ">=", 20, "undetermined"),
    ("net_profit_min", "<=", 20, "undetermined"),
])
def test_one_sided_trigger_variants(missing, op, value, outcome):
    # One missing bound and a metric/threshold variant of the real add trigger.
    row = ROWS["forecast"]["688256.SH"][0] | {missing: None}
    observation, = checks.normalize_forecast([row], "688256")
    item = shifted_item(156, -1) | {"metric": "T.net_profit", "op": op, "value": value}
    assert checks.judge(item, observation)["outcome"] == outcome


def real_table():
    return json.loads((DATA / "k7a-forecast-report.json").read_text())["report"]["body"]["table"]


def test_periodic_exact_table_and_unavailable_variant():
    table = real_table()
    original = copy.deepcopy(table)
    period = forecasts.base_period(table)
    observation = checks.periodic_observation(table, period, identity="captured-report:103", announced="2026-11-05")
    for row in table:
        if row["id"].startswith("T.") and period in forecasts._values(row):
            actual = forecasts._values(row)[period]
            assert observation["values"][row["id"]] == {"low": actual, "high": actual}
    item = checks.items(body(53))[0] | {"period": period}
    assert checks.judge(item, observation) is not None
    assert table == original
    # Unavailable and rounded-display variants of a real captured table row.
    row = next(r for r in table if r["id"] == "T.revenue")
    exact = forecasts._values(row)[period]
    row["exact_values"] = {period: str(exact)}
    row["values"][period] = str(round(exact))
    assert checks.periodic_observation(table, period, identity="x", announced="2026-11-05")["values"]["T.revenue"]["low"] == exact
    row["unavailable"] = {period: "同年利润表有更正，须补读披露核对。"}
    unavailable = checks.periodic_observation(table, period, identity="x", announced="2026-11-05")
    result = checks.judge(item, unavailable)
    assert result["outcome"] == "undetermined"
    assert result["reason"] == row["unavailable"][period]
    assert checks.periodic_observation(table, "2099-12-31", identity="x", announced="2026-11-05")["values"] == {}


def test_latest_source_identity_ties_and_ref_order():
    result = checks.judge(shifted_item(), forecast())
    # Source/date/identity-only variants isolate tie breaking, not economic outcomes.
    express = result | {"source": "express", "identity": "express:a"}
    periodic = result | {"source": "periodic", "identity": "periodic:a"}
    second = result | {"ref": "checkpoints[1]"}
    assert checks.latest([result, second, express, periodic]) == [periodic, second]
    assert checks.latest([periodic, express, result]) == [periodic]
    assert checks.latest([result, express]) == [express]
    newer_identity = periodic | {"identity": "periodic:b"}
    assert checks.latest([newer_identity, periodic]) == [newer_identity]
    revision = result | {"announced": "2026-01-24"}
    assert checks.latest([periodic, revision]) == [revision]
    assert checks.latest([]) == []
