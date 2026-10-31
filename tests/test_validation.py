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
    revenue = forecasts.score(_forecast(period="2026-09-30", low=95, high=105), "2026-06-30", rows["T.revenue"])
    assert revenue["status"] == "scored" and revenue["hit"] is True
    # Naive: 2025-09-30 × (2026-06-30 / 2025-06-30)
    v = {p: float(x) for p, x in rows["T.revenue"]["exact_values"].items()}
    assert revenue["baseline"] == pytest.approx(v["2025-09-30"] * v["2026-06-30"] / v["2025-06-30"])
    assert revenue["beat_baseline"] == (abs(100 - v["2026-09-30"]) < abs(revenue["baseline"] - v["2026-09-30"]))
    margin = forecasts.score(_forecast(metric="T.gross_margin", period="2026-09-30", low=0, high=1), "2026-06-30",
                             rows["T.gross_margin"])
    assert margin["hit"] is False and margin["baseline"] == pytest.approx(float(rows["T.gross_margin"]["exact_values"]["2026-06-30"]))
    assert forecasts.score(_forecast(), "2026-09-30", rows["T.revenue"]) == {"status": "pending"}


def test_forecast_summary_reports_calibration_and_baseline_share():
    scored = [{"forecast": {"confidence": 75}, "result": {"status": "scored", "hit": h, "beat_baseline": b}}
              for h, b in ((True, True), (False, None), (True, False))] + [
             {"forecast": {"confidence": 90}, "result": {"status": "pending"}}]
    s = validation.summarize_forecasts(scored)
    assert s["scored"] == 3 and s["pending"] == 1
    assert s["by_confidence"] == {75: {"n": 3, "hit": pytest.approx(2 / 3)}}
    assert s["beat_baseline"] == 0.5 and s["compared"] == 2


def test_report_item_shows_forecast_with_row_label_and_unit():
    from support.templates import ENV
    rows = {r["id"]: r for r in TABLE}
    entry = {"text": "观察第四季度收入。", "cites": [], "forecast": _forecast()}
    html = str(ENV.get_template("_macros.html").module.item(entry, 0, rows, {}, "checkpoints", ""))
    assert "预测 · 营业总收入" in html and "130–150</span>亿元，把握 75%" in html
    del entry["forecast"]
    assert "预测" not in str(ENV.get_template("_macros.html").module.item(entry, 0, rows, {}, "checkpoints", ""))
