"""A3 deterministic checks using captured TuShare prices, factors, index and PE.

Only report metadata/valuation thresholds are test parameters. No invented market
history is used to make the 250-session PE requirement pass.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from a_share_research import validation, zones

DATA = Path(__file__).with_name("data")
V0 = json.loads((DATA / "validation_v0.json").read_text())
LONG = json.loads((DATA / "600900-61d.json").read_text())
PE_CAPTURE = json.loads((DATA / "since-prior-600900.json").read_text())
PE = {day.replace("-", ""): float(value)
      for table in PE_CAPTURE["tables"].values() for row in table if row["id"] == "M.pe_ttm"
      for day, value in row["values"].items()}
METADATA = {r["id"]: r for r in V0["reports"]}
CAL = V0["trade_cal"]
RAW = V0["daily"]
ADJUSTED = {day: price * V0["adj_factor"][day] for day, price in RAW.items()}
BOUNDS = {"support_below": 30.0, "stretched_above": 40.0, "inverted": False}


def report(report_id=29, **changes):
    meta = METADATA[report_id]
    return {"id": report_id, "code": meta["code"], "name": meta["code"],
            "completed": datetime.fromisoformat(meta["created_at"]),
            "source": validation.source(meta["request"]), "period": None,
            "valuation_calc": {"rule": "valuation-v1", "zones": BOUNDS}} | changes


def scored():
    rows = zones.observations([report()], CAL, RAW, {})
    return zones.score_observations(rows, CAL, ADJUSTED, V0["benchmark"])


def test_unadjusted_boundaries_and_segment_starts():
    bounds = {"support_below": RAW["20261027"], "stretched_above": RAW["20261026"]}
    labels = {day: zones.zone_of(close, bounds) for day, close in RAW.items()}
    assert labels["20261026"] == "测算透支区"  # inclusive upper bound
    assert labels["20261027"] == "测算支持区"  # inclusive lower bound
    assert labels["20261028"] == "中性区"
    assert zones.first_days(labels) == ["20261026", "20261027", "20261028", "20261029"]
    assert zones.zone_of(None, bounds) is None
    # Missing observations are never classified as neutral.
    assert zones.first_days({d: labels.get(d) for d in CAL}) == zones.first_days(labels)


def test_suspension_does_not_restart_the_same_zone():
    days = sorted(RAW)[:4]
    labels = {days[0]: "测算支持区", days[1]: None, days[2]: "测算支持区", days[3]: "中性区"}
    assert zones.first_days(labels) == [days[0], days[3]]


def test_report_change_does_not_restart_unchanged_zone():
    first = report()
    replacement = report(34, code=first["code"])
    rows = zones.observations([replacement, first], CAL, RAW, {})
    assert [(r["observation"], r["report"]) for r in rows] == [("20261026", 29)]
    # A changed threshold still does not create a new observation if the label stays the same.
    replacement["valuation_calc"] = {"rule": "valuation-v1", "zones": BOUNDS | {"support_below": 25.0}}
    rows = zones.observations([first, replacement], CAL, RAW, {})
    assert [(r["observation"], r["report"], r["zone"]) for r in rows] == [
        ("20261026", 29, "测算支持区"), ("20261028", 34, "中性区"), ("20261029", 34, "测算支持区")]


def test_latest_report_wins_when_two_reports_enter_on_same_day():
    first = report(34)
    later = report(35, valuation_calc={"rule": "valuation-v1", "zones": BOUNDS | {"support_below": 24.0}})
    rows = zones.observations([later, first], CAL, RAW, {})
    assert rows[0]["observation"] == "20261027"
    assert rows[0]["report"] == 35 and rows[0]["zone"] == "中性区"
    assert all(r["report"] == 35 for r in rows)


def test_return_starts_at_next_session_close_not_observation_close():
    (row,) = scored()
    assert row["observation"] == "20261026" and row["entry"] == "20261027"
    expected = (RAW["20261030"] * V0["adj_factor"]["20261030"]
                / (RAW["20261027"] * V0["adj_factor"]["20261027"]) - 1
                - (V0["benchmark"]["20261030"] / V0["benchmark"]["20261027"] - 1))
    assert row["returns"]["latest"]["excess"] == pytest.approx(expected)
    same_close = validation.excess(ADJUSTED, V0["benchmark"], "20261026", "20261030")
    assert row["returns"]["latest"]["excess"] != pytest.approx(same_close["excess"])
    assert set(row["returns"]) == {"latest"}  # no invented matured horizons


def test_missing_entry_is_not_shifted_to_a_later_available_price():
    rows = zones.observations([report()], CAL, RAW, {})
    missing = {d: value for d, value in ADJUSTED.items() if d != "20261027"}
    assert zones.score_observations(rows, CAL, missing, V0["benchmark"])[0]["returns"] == {}
    # A signal on the final known session is pending, not bought at its own close.
    rows = zones.observations([report()], ["20261026"], RAW, {})
    assert rows[0]["entry"] is None
    assert zones.score_observations(rows, CAL, ADJUSTED, V0["benchmark"])[0]["returns"] == {}


def test_horizon_clock_uses_sessions_after_the_delayed_entry():
    days = [r["trade_date"] for r in LONG["stock"]]
    raw = {r["trade_date"]: r["close"] for r in LONG["stock"]}
    # Real 61-session capture: one session of signal delay leaves only 59 elapsed sessions.
    first = report(completed=datetime.fromisoformat("2026-07-20T09:00:00+08:00"), code="600900.SH")
    (row,) = zones.observations([first], days, raw, {})
    assert row["entry"] == days[1]
    assert validation.horizon_dates(days, row["entry"], days[-1]) == {"20d": days[21], "latest": days[-1]}
    assert "60d" in validation.horizon_dates(days, days[0], days[-1])  # wrong same-close clock would mature early


def test_pe_under_250_valid_prior_sessions_is_unclassified():
    assert zones.PE_WINDOW == 1250 and zones.PE_MINIMUM == 250
    assert set(zones.pe_percentile_labels(PE, CAL).values()) == {None}
    assert zones.pe_percentile_labels({}, CAL) == dict.fromkeys(CAL)


def test_pe_ranking_excludes_today_and_ages_by_calendar_not_available_rows():
    days = sorted(PE)
    # Short-window arithmetic check only; the production minimum remains 250.
    labels = zones.pe_percentile_labels(PE, days, window=1, minimum=1)
    assert labels == {"20261022": None, "20261023": "分位低位"}
    # After a gap longer than the window, old valid PE must not substitute for recent sessions.
    later_days = CAL[CAL.index(days[0]):]
    assert zones.pe_percentile_labels(PE, later_days, window=1, minimum=1)["20261027"] is None


def test_disagreements_are_opposing_directions_on_union_of_signal_days():
    bounds = BOUNDS | {"support_below": RAW["20261027"], "stretched_above": RAW["20261026"]}
    # These are rule labels, not fabricated prices or PE history.
    labels = {"20261026": "分位低位", "20261027": "分位高位", "20261028": "分位高位",
              "20261029": "分位高位", "20261030": "分位中位"}
    rows = zones.observations([report(valuation_calc={"rule": "valuation-v1", "zones": bounds})], CAL, RAW, labels)
    result = zones.diagnostics(zones.score_observations(rows, CAL, ADJUSTED, V0["benchmark"]))
    assert result["observation_days"] == 5 and result["comparable_days"] == 5
    assert result["disagreements"] == 3  # 26, 27, 29; two rows on 26/27 count only once
    assert result["both_first_days"] == 2 and result["both_first_share"] == pytest.approx(2 / 5)
    assert zones.diagnostics(scored())["disagreements"] == 0  # unavailable PE is not disagreement


def test_summary_counts_distinct_companies_and_reports_pending_horizons():
    (row,) = scored()
    # Repeated observations of one company do not become additional companies.
    rows = [row, row | {"report": 35}, row | {"code": METADATA[34]["code"]}]
    summary = {g["horizon"]: g for g in zones.summarize(rows)}
    latest = summary["latest"]
    assert latest["n"] == 3 and latest["companies"] == 2
    assert latest["mean"] == pytest.approx(row["returns"]["latest"]["excess"])
    assert latest["median"] == latest["mean"]
    assert latest["beat"] == 0 and latest["left_tail"] == 0
    assert summary["120d"]["n"] == 0 and summary["120d"]["pending"] == 3
    assert summary["120d"]["mean"] is None and summary["120d"]["companies"] == 0
    overlap = next(g for g in zones.diagnostics(rows)["return_overlap"] if g["horizon"] == "latest")
    assert overlap["overlapping"] == 1 and overlap["pairs"] == 1


def test_offline_diagnosis_is_serializable_and_keeps_raw_and_adjusted_separate():
    code = report()["code"]
    result = zones.diagnose([report()], CAL, {code: {"close": RAW, "adjusted": ADJUSTED, "pe": {}}}, V0["benchmark"])
    assert result["rows"] == scored()
    assert result["rows"][0]["zone"] == "测算支持区"  # adjusted closes exceed the 30-yuan boundary
    assert result["benchmark"] == "H00906.CSI" and result["benchmark_last"] == "20261030"
    assert result["diagnostics"]["evidence"] == "证据不足"
    assert json.loads(json.dumps(result)) == result
    empty = zones.diagnose([], [], {}, {})
    assert empty["rows"] == [] and empty["summary"] == []
    assert empty["diagnostics"]["evidence"] == "证据不足"


def test_load_reports_read_only_eligibility_and_no_monthly_dedup(tmp_path, monkeypatch):
    database = tmp_path / "app.db"
    calc = {"rule": "valuation-v1", "zones": BOUNDS}
    with sqlite3.connect(database) as conn:
        conn.executescript("CREATE TABLE runs (id INTEGER, kind TEXT, replay_of INTEGER, request TEXT);"
                           "CREATE TABLE reports (id INTEGER, run_id INTEGER, code TEXT, name TEXT, "
                           "created_at TEXT, passed INTEGER, body TEXT);")
        cases = [(34, "research", None, 1, calc), (35, "research", None, 1, calc),
                 (38, "research", None, 1, calc), (10, "research", None, 1, calc),  # feedback
                 (29, "research", 1, 1, calc), (29, "screening", None, 1, calc),
                 (29, "research", None, 0, calc), (29, "research", None, 1, {"rule": "valuation-v1"}),
                 (29, "research", None, 1, calc | {"not_priced": True}),
                 (29, "research", None, 1, calc | {"error": "failed"}),
                 (29, "research", None, 1, calc | {"zones": BOUNDS | {"inverted": True}}),
                 (29, "research", None, 1, calc)]
        for run_id, (report_id, kind, replay, passed, valuation) in enumerate(cases):
            meta = METADATA[report_id]
            conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?)", (run_id, kind, replay, json.dumps(meta["request"])))
            conn.execute("INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (report_id, run_id, meta["code"], meta["code"], meta["created_at"], passed,
                          json.dumps({"valuation_calc": valuation})))
    before = database.read_bytes()
    connect = sqlite3.connect

    def readonly_connection(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro") and kwargs == {"uri": True}
        connection = connect(database_uri, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")
        return connection

    monkeypatch.setattr(zones.sqlite3, "connect", readonly_connection)
    loaded = zones.load_reports(str(database))
    assert [r["id"] for r in loaded] == [29, 34, 35, 38]
    assert [r["source"] for r in loaded] == ["batch", "active", "active", "active"]
    assert database.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        zones.load_reports(str(path))
    assert not path.exists()


def test_v2_shadow_rule_scores_beside_v1_without_touching_v1_and_pe_diagnostics():
    # Captured closes 20.01–25.65 are all v1 support (<=30); a 22/30 v2 band makes most of them neutral.
    v2 = {"support_below": 22.0, "stretched_above": 30.0}
    labels = {"20261026": "分位低位"}
    base = zones.observations([report()], CAL, RAW, labels)
    rows = zones.observations([report(valuation_calc={"rule": "valuation-v1", "zones": BOUNDS, "v2": v2})], CAL, RAW, labels)
    assert [r for r in rows if r["rule"] != "valuation-v2"] == base
    shadow = [r for r in rows if r["rule"] == "valuation-v2"]
    assert shadow and all(r["v2_zone"] == r["zone"] == zones.zone_of(RAW[r["observation"]], v2) for r in shadow)
    scored_rows = zones.score_observations(rows, CAL, ADJUSTED, V0["benchmark"])
    with_v2, without = zones.diagnostics(scored_rows), zones.diagnostics(zones.score_observations(base, CAL, ADJUSTED, V0["benchmark"]))
    for key in ("observation_days", "comparable_days", "disagreements", "both_first_days", "matured_120d"):
        assert with_v2[key] == without[key]
    assert with_v2["v2_observations"] == len(shadow)
    assert with_v2["v2_differs_from_v1"] == sum(r["zone"] != r["valuation_zone"] for r in shadow) > 0
    assert any(g["rule"] == "valuation-v2" for g in zones.summarize(scored_rows))
