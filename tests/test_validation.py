"""V0 judgment validation: entry day, dedup and excess return on real TuShare rows and production report metadata."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from a_share_research import validation

DATA = json.loads(Path(__file__).with_name("data").joinpath("validation_v0.json").read_text(encoding="utf-8"))
REPORTS = {r["id"]: r for r in DATA["reports"]}
CAL = DATA["trade_cal"]


def _completed(report_id: int) -> datetime:
    return datetime.fromisoformat(REPORTS[report_id]["created_at"])


def test_source_separates_batches_active_research_and_feedback_checks():
    assert validation.source(REPORTS[29]["request"]) == "batch"
    assert validation.source(REPORTS[34]["request"]) == "active"
    assert validation.source(REPORTS[36]["request"]) == "active"  # empty question is still owner research
    assert validation.source(REPORTS[10]["request"]) is None  # feedback check is not a new judgment


def test_entry_is_first_full_session_after_completion():
    assert validation.entry_day(_completed(29), CAL) == "20261026"  # Saturday evening → Monday
    assert validation.entry_day(_completed(38), CAL) == "20261028"  # finished mid-session: that day is not full
    assert validation.entry_day(_completed(36), CAL) == "20261027"  # after the close → next session
    assert validation.entry_day(_completed(47), CAL[:9]) is None  # calendar ends before the next session


def test_first_report_per_source_code_and_month_is_kept():
    reports = [{"id": i, "code": REPORTS[i]["code"], "source": validation.source(REPORTS[i]["request"]),
                "completed": _completed(i)} for i in (38, 34, 35, 36)]
    assert [r["id"] for r in validation.first_per_month(reports)] == [34, 36]


def test_excess_matches_tushare_back_adjusted_bars():
    stock = {d: DATA["daily"][d] * DATA["adj_factor"][d] for d in DATA["daily"]}
    result = validation.excess(stock, DATA["benchmark"], "20261026", "20261030")
    assert result is not None
    hfq = DATA["pro_bar_hfq"]
    independent = hfq["20261030"] / hfq["20261026"] - 1 - (DATA["benchmark"]["20261030"] / DATA["benchmark"]["20261026"] - 1)
    assert result["excess"] == pytest.approx(independent, abs=1e-3)  # pro_bar rounds hfq closes to 2 decimals
    assert result["excess"] < validation.LEFT_TAIL / 2


def test_suspended_stock_keeps_last_close_and_missing_entry_is_unscored():
    stock = {d: DATA["daily"][d] * DATA["adj_factor"][d] for d in ("20261026", "20261027")}
    carried = validation.excess(stock, DATA["benchmark"], "20261026", "20261030")
    assert carried is not None
    assert carried["stock"] == pytest.approx(stock["20261027"] / stock["20261026"] - 1)
    assert validation.excess(stock, DATA["benchmark"], "20261028", "20261030") is None


def test_horizons_only_when_matured():
    days = validation.horizon_dates(CAL, "20261026", "20261030")
    assert days == {"latest": "20261030"}
    assert validation.horizon_dates(CAL, "20261030", "20261030") == {}


def test_summary_counts_beat_and_left_tail_per_group():
    rows = [{"source": "batch", "verdict": "需要谨慎", "returns": {"latest": {"excess": e}}} for e in (-0.35, 0.02, 0.10)]
    (group,) = validation.summarize(rows, "verdict")
    assert group["n"] == 3 and group["beat"] == pytest.approx(2 / 3) and group["left_tail"] == pytest.approx(1 / 3)
    assert group["median"] == pytest.approx(0.02)


# ---------- V1: checkpoint forecasts ----------

from a_share_research import forecasts  # noqa: E402
from a_share_research.checking import check  # noqa: E402
from tests.support.reports import MATERIAL, report  # noqa: E402

TABLE = DATA["forecast_table"]  # T.* rows of production report 49 (东方电缆, through 2026-09-30)


def _forecast(**changes):
    return {"metric": "T.revenue", "period": "2026-12-31", "low": 130, "high": 150, "confidence": 75} | changes


def test_next_period_rolls_quarters_and_years():
    assert forecasts.next_period("2026-09-30") == "2026-12-31"
    assert forecasts.next_period("2026-12-31") == "2027-03-31"
    assert forecasts.base_period(TABLE) == "2026-09-30"


def test_forecast_structure_problems():
    assert forecasts.problems(_forecast(), TABLE) == []
    assert len(forecasts.problems(_forecast(period="2026-09-30"), TABLE)) == 1  # must be the next period
    assert len(forecasts.problems(_forecast(metric="M.close"), TABLE)) == 1  # market rows cannot be scored
    assert len(forecasts.problems(_forecast(low=160), TABLE)) == 1
    assert len(forecasts.problems(_forecast(high=True), TABLE)) == 1
    assert len(forecasts.problems(_forecast(confidence=70), TABLE)) == 1
    assert forecasts.problems("130-150", TABLE) == ["forecast 必须是对象"]


def test_check_requires_forecasts_and_validates_them():
    assert check(report(), MATERIAL) == []
    plain = report()
    del plain["checkpoints"][0]["forecast"]
    assert [p["where"] for p in check(plain, MATERIAL)] == ["checkpoints"]
    wrong = report()
    wrong["checkpoints"][0]["forecast"]["period"] = "2025-12-31"
    assert [p["where"] for p in check(wrong, MATERIAL)] == ["checkpoints[0]"]
    two = report()
    two["checkpoints"].append({"text": "下一期营业总收入是否继续低于1720.54亿元。", "cites": [{"ref": "T.revenue", "period": "2025-12-31"}]})
    assert [p["problem"] for p in check(two, MATERIAL)] == ["至少 2 条检查点须带 forecast（当前 1 条）"]


def test_score_on_real_disclosed_quarter():
    rows = {r["id"]: r for r in TABLE}
    # As if written on the half-year report: forecast the Q3 cumulative figures that were later disclosed.
    revenue = forecasts.score(_forecast(period="2026-09-30", low=95, high=105), "2026-06-30", rows["T.revenue"],
                              rows["T.revenue"])
    assert revenue["status"] == "scored" and revenue["hit"] is True
    # Naive: 2025-09-30 × (2026-06-30 / 2025-06-30)
    v = {p: float(x) for p, x in rows["T.revenue"]["exact_values"].items()}
    assert revenue["baseline"] == pytest.approx(v["2025-09-30"] * v["2026-06-30"] / v["2025-06-30"])
    assert revenue["beat_baseline"] == (abs(100 - v["2026-09-30"]) < abs(revenue["baseline"] - v["2026-09-30"]))
    margin = forecasts.score(_forecast(metric="T.gross_margin", period="2026-09-30", low=0, high=1), "2026-06-30",
                             rows["T.gross_margin"], rows["T.gross_margin"])
    assert margin["hit"] is False and margin["baseline"] == pytest.approx(float(rows["T.gross_margin"]["exact_values"]["2026-06-30"]))
    assert forecasts.score(_forecast(), "2026-09-30", rows["T.revenue"], rows["T.revenue"]) == {"status": "pending"}


def test_naive_baseline_stays_on_the_table_the_report_was_written_on():
    rows = {r["id"]: r for r in TABLE}
    later = rows["T.revenue"]
    # The report was written before the half-year figure was restated: its frozen copy carries the old value.
    written = later | {"exact_values": later["exact_values"] | {"2026-06-30": str(float(later["exact_values"]["2026-06-30"]) * 0.9)}}
    v = {p: float(x) for p, x in written["exact_values"].items()}
    result = forecasts.score(_forecast(period="2026-09-30", low=95, high=105), "2026-06-30", later, written)
    assert result["baseline"] == pytest.approx(v["2025-09-30"] * v["2026-06-30"] / v["2025-06-30"])
    assert result["restated"] is True
    assert forecasts.score(_forecast(period="2026-09-30", low=95, high=105), "2026-06-30", later, later)["restated"] is False


def test_forecast_summary_reports_calibration_and_baseline_share():
    scored = [{"code": c, "first": True, "forecast": {"confidence": 75, "period": "2026-12-31"},
               "result": {"status": "scored", "hit": h, "beat_baseline": b, "restated": False}}
              for c, h, b in (("A", True, True), ("A", False, None), ("B", True, False))] + [
             {"code": "C", "first": True, "forecast": {"confidence": 90, "period": "2026-12-31"}, "result": {"status": "pending"}},
             {"code": "B", "first": False, "forecast": {"confidence": 90, "period": "2026-12-31"},
              "result": {"status": "scored", "hit": False, "beat_baseline": False}}]
    s = validation.summarize_forecasts(scored)
    assert s["scored"] == 3 and s["pending"] == 1 and s["updates"] == 1
    assert s["companies"] == 3 and s["periods"] == 1
    assert s["by_confidence"] == {75: {"n": 3, "hit": pytest.approx(2 / 3)}}
    assert s["beat_baseline"] == 0.5 and s["compared"] == 2


def test_first_forecast_per_company_metric_and_period():
    def item(report, created, metric="T.revenue", period="2026-12-31", code="300750.SZ"):
        return {"report": report, "created": created, "index": 0, "code": code,
                "forecast": {"metric": metric, "period": period}}
    items = validation.mark_first([item(9, "2026-11-20"), item(3, "2026-11-01"), item(5, "2026-11-05", metric="T.op_cash"),
                                   item(7, "2026-11-10", period="2027-03-31"), item(8, "2026-11-12", code="600900.SH")])
    assert {i["report"]: i["first"] for i in items} == {3: True, 9: False, 5: True, 7: True, 8: True}


def test_report_item_shows_forecast_with_row_label_and_unit():
    from support.templates import ENV
    rows = {r["id"]: r for r in TABLE}
    entry = {"text": "观察第四季度收入。", "cites": [], "forecast": _forecast()}
    html = str(ENV.get_template("_macros.html").module.item(entry, 0, rows, {}, "checkpoints", ""))
    assert "预测 · 营业总收入" in html and "130–150</span>亿元，把握 75%" in html
    del entry["forecast"]
    assert "预测" not in str(ENV.get_template("_macros.html").module.item(entry, 0, rows, {}, "checkpoints", ""))


# ---------- V2: single-stock relative call removed from write path; historical display kept ----------

def test_output_schema_does_not_contain_relative():
    from a_share_research.prompts import output_schema
    schema = output_schema(MATERIAL)
    assert '"relative"' not in schema
    feedback = MATERIAL.__class__(**{**MATERIAL.__dict__, "extra": {"feedback": {"location": "risks[0]", "text": "x"}}})
    assert '"relative"' not in output_schema(feedback)


def test_repair_patch_rejects_relative_call():
    from a_share_research.research import merge_repair_patch, RepairFormatError
    with pytest.raises(RepairFormatError, match="只允许 ops、verdict、summary、gaps、valuation、stance、rebuttals"):
        merge_repair_patch(report(), {"relative": {"call": "跑赢", "confidence": 55}}, [], MATERIAL)


def test_report_header_shows_relative_call():
    from support.templates import ENV
    template = (Path(__file__).resolve().parents[1] / "a_share_research/templates/report.html").read_text()
    line = next(l for l in template.splitlines() if 'class="relative-call"' in l)
    html = ENV.from_string(line).render(body={"relative": {"call": "跑输", "confidence": 65}})
    assert "6 个月相对判断：跑输中证800全收益，把握 65%" in html
    assert "记分" not in html  # single relative calls are no longer scored
    assert ENV.from_string(line).render(body={}).strip() == ""


def test_passed_report_saves_checkpoints_and_strips_extra_relative():
    from a_share_research.research import published
    draft = report(relative={"call": "跑输", "confidence": 60})
    assert check(draft, MATERIAL) == []
    saved = published(draft)
    assert "relative" not in saved and saved["checkpoints"][0]["forecast"]
    legacy = report()
    del legacy["checkpoints"]
    assert "checkpoints" not in published(legacy)



@pytest.mark.parametrize("today,ended", [("2026-12-31", False), ("2027-01-01", True)])
def test_forecast_period_must_end_before_fetch(today, ended):
    # Every production forecast targets 2026-12-31 (reports 70-78); none can be disclosed before 2027.
    assert validation.period_ended("2026-12-31", datetime.fromisoformat(today).date()) is ended


def test_forecast_missing_disclosed_period_is_not_pending():
    # Real corrected row from the saved evaluation, not a new forecast outcome.
    from support.valuation import STORED
    row = next(r for r in STORED[1]['table'] if r['id'] == 'T.net_profit')
    f = _forecast(metric='T.net_profit', period='2025-03-31')
    result = forecasts.score(f, '2024-12-31', row, row)
    assert result['status'] == 'unavailable' and '更正' in result['reason']
    # Remove only explanatory metadata: later saved periods still prove this is not pending.
    without_reason = {k: v for k, v in row.items() if k != 'unavailable'}
    assert forecasts.score(f, '2024-12-31', without_reason, row)['status'] == 'unavailable'
    summary = validation.summarize_forecasts([{'first': True, 'code': '000858', 'forecast': f, 'result': result}])
    assert summary['pending'] == 0 and summary['unavailable'] == 1 and summary['compared'] == 0
    # Unknown future values remain pending; a missing baseline must not hide a known actual.
    assert forecasts.score(_forecast(), '2026-09-30', row, row)['status'] == 'pending'
    known = forecasts.score(_forecast(metric='T.net_profit', period='2026-09-30'), '2026-06-30', row, {})
    assert known['status'] == 'scored' and known['baseline'] is None


# ---------- V4 stance scoring ----------

LOWS = {bar["trade_date"]: bar["low"] for bar in
        json.loads(Path(__file__).with_name("data").joinpath("600900-61d.json").read_text())["stock"]}


def test_price_wait_hits_on_first_touching_low_and_misses_only_after_expiry():
    # 600900 captured unadjusted lows: the window minimum is 27.51 on 2026-08-07.
    assert validation.price_wait(LOWS, 27.55, "20260720", "2027-01-20", "20261020") == {"status": "hit", "date": "20260807"}
    assert validation.price_wait(LOWS, 27.51, "20260720", "2027-01-20", "20261020")["date"] == "20260807"
    # Touching after the validity window does not count; an open window is pending, a closed one missed.
    assert validation.price_wait(LOWS, 27.55, "20260720", "2026-08-06", "20261020") == {"status": "missed"}
    assert validation.price_wait(LOWS, 27.50, "20260720", "2027-01-20", "20261020") == {"status": "pending"}
    assert validation.price_wait(LOWS, 27.50, "20260720", "2026-10-20", "20261020") == {"status": "missed"}
    # Entry-day low counts; lows before entry never do.
    assert validation.price_wait(LOWS, 27.55, "20260808", "2027-01-20", "20261020")["status"] == "pending"


def test_evidence_wait_reads_disclosed_table_text():
    row = next(r for r in DATA["forecast_table"] if r["id"] == "T.revenue")  # 2026-09-30 = '101.00'
    trigger = {"metric": "T.revenue", "period": "2026-09-30", "op": ">=", "value": 100}
    today = datetime(2026, 11, 4).date()
    assert validation.evidence_wait(trigger, row, today) == {"status": "met", "actual": "101.00"}
    assert validation.evidence_wait(trigger | {"value": 101.01}, row, today)["status"] == "not_met"
    assert validation.evidence_wait(trigger | {"op": "<="}, row, today)["status"] == "not_met"
    assert validation.evidence_wait(trigger | {"period": "2026-12-31"}, row, today) == {"status": "pending"}
    assert validation.evidence_wait(trigger, {"values": {}}, today)["status"] == "pending"


def test_stance_summary_groups_outcomes_with_latest_excess():
    rows = [{"stance": "等价格", "stance_outcome": {"status": "missed"}, "returns": {"latest": {"excess": 0.08}}},
            {"stance": "等价格", "stance_outcome": {"status": "missed"}, "returns": {"latest": {"excess": -0.02}}},
            {"stance": "等价格", "stance_outcome": {"status": "hit", "date": "20261101"}, "returns": {}},
            {"stance": "现在分批", "returns": {"latest": {"excess": 0.01}}}]
    summary = validation.summarize_stances(rows)
    assert summary["等价格·missed"] == {"n": 2, "scored": 2, "mean_excess": pytest.approx(0.03)}
    assert summary["等价格·hit"] == {"n": 1, "scored": 0, "mean_excess": None}
    assert summary["现在分批"]["mean_excess"] == pytest.approx(0.01)


def test_frozen_add_checkpoint_and_legacy_wait_share_disclosure_scoring():
    table = {r['id']: r for r in DATA['forecast_table']}
    trigger = {'metric': 'T.revenue', 'period': '2026-09-30', 'op': '>=', 'value': 100}
    today = datetime(2026, 11, 4).date()
    for action, field in [('现在分批', 'add_trigger'), ('等证据', 'trigger')]:
        row = {'stance': action, 'stance_calc': {field: trigger}, 'trigger': trigger | {'value': 101.01}}
        assert validation.checkpoint_outcome(row, table, today) == {'status': 'met', 'actual': '101.00'}
        row['stance_calc'][field] = trigger | {'value': 101.01}
        assert validation.checkpoint_outcome(row, table, today)['status'] == 'not_met'
        row['stance_calc'][field] = trigger | {'period': '2026-12-31'}
        assert validation.checkpoint_outcome(row, table, today)['status'] == 'pending'
    assert validation.checkpoint_outcome({'stance': '现在分批'}, table, today) == {}


def test_load_legacy_wait_and_new_frozen_add_without_migration():
    from a_share_research import db
    captured = json.loads(Path(__file__).with_name('data').joinpath('legacy-stance-20261104.json').read_text())
    with db.connect() as conn:
        run = db.create_run(conn, captured['code'], status='done')
        db.save_report(conn, run, captured['code'], captured['name'], captured['body']['document']['period'],
                       True, captured['verdict'], captured['body'])
        loaded = validation.load_reports(str(db.DATA / 'app.db'))
        assert loaded[0]['stance'] == '等证据'
        assert loaded[0]['trigger'] == captured['body']['stance_calc']['trigger']
        assert loaded[0]['add_trigger'] is None
        body = captured['body']
        # Contract migration of the same captured threshold, not a new financial observation.
        body['stance'] = {k: v for k, v in body['stance'].items() if k != 'trigger'} | {
            'action': '现在分批', 'add_trigger': loaded[0]['trigger']}
        body['stance_calc'] = {'action': '现在分批', 'add_trigger': loaded[0]['trigger']}
        conn.execute('UPDATE reports SET body=? WHERE run_id=?', (json.dumps(body), run))
        loaded = validation.load_reports(str(db.DATA / 'app.db'))
        assert loaded[0]['stance'] == '现在分批' and loaded[0]['add_trigger'] == body['stance_calc']['add_trigger']


def test_future_checkpoint_is_pending_without_fetching_financials():
    captured = json.loads(Path(__file__).with_name('data').joinpath('legacy-stance-20261104.json').read_text())
    for action, field in [('等证据', 'trigger'), ('现在分批', 'add_trigger')]:
        row = {'stance': action, 'stance_calc': {field: captured['body']['stance']['trigger']},
               'trigger': captured['body']['stance']['trigger'], 'returns': {}, 'code': captured['code']}
        assert validation.score_stances(None, [row], datetime(2026, 11, 4).date())[0]['stance_outcome'] == {'status': 'pending'}
