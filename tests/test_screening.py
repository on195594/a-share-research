"""Screen factors, ranking and simulated returns on real TuShare data captured 2026-10-24.

Factor inputs were checked against the 2025 annual reports: 格力电器 扣非净利润 27,706,292,436.94
(p7), 经营现金流 46,383,114,754.02 (p7), 净利润 28,862,746,016.16 (p88); 卫星化学 6,291,539,606.92
(p8), 9,606,885,402.76 (p8), 5,305,340,636.39 (p71).
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import screening

DATA = Path(__file__).parent / "data"
FIN = json.loads((DATA / "screen-financials.json").read_text())
ROWS = json.loads((DATA / "screen-rows.json").read_text())
PRICES = json.loads((DATA / "screen-prices.json").read_text())


def frames(code: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    company = FIN["companies"][code]
    return tuple(pd.DataFrame(company[k]) for k in ("indicator", "income", "cashflow"))


def test_factors_match_hand_calculation():
    # Expected values computed independently from the raw rows. 卫星化学: ROE (19.7104+21.6969+20.0605)/3; cash 28,193,673,736.13 / 16,151,053,615.02;
    # growth (6,291,539,606.92 / 3,044,182,919.30)^(1/3) − 1 over 2022→2025.
    out = screening.factors(*frames("002648.SZ"), FIN["as_of"])
    assert out["status"] == "ok" and out["years"] == ["2022", "2023", "2024", "2025"]
    assert out["roe"] == pytest.approx(20.48927, abs=1e-5)
    assert out["cash"] == pytest.approx(1.745624, abs=1e-6)
    assert out["growth"] == pytest.approx(0.273783, abs=1e-6)
    # 格力电器: cash 132,150,791,678.85 / 88,952,838,217.10; growth (27,706.29 / 23,986.25)^(1/3) − 1.
    out = screening.factors(*frames("000651.SZ"), FIN["as_of"])
    assert out["roe"] == pytest.approx(23.01787, abs=1e-5)
    assert out["cash"] == pytest.approx(1.485628, abs=1e-6)
    assert out["growth"] == pytest.approx(0.049233, abs=1e-6)


def test_only_reports_announced_by_the_screen_date_count():
    # On 2026-03-01 the 2025 annual report (announced 2026-03-24) was not yet public.
    out = screening.factors(*frames("002648.SZ"), "20260301")
    assert out["years"] == ["2021", "2022", "2023", "2024"]


def test_scores_reproduce_the_live_screen():
    rows = [{k: r[k] for k in r if k != "score"} for r in ROWS["rows"]]
    ranked = screening.score(rows)
    expected = {r["code"]: r["score"] for r in ROWS["rows"]}
    assert all(r["score"] == pytest.approx(expected[r["code"]]) for r in ranked)
    assert {r["group_size"] for r in ranked if r["industry"] == "食品饮料"} == {26}
    picked = screening.pick(ranked, 5, 2)  # two industries capped at two each
    assert len(picked) == 4 and max(sum(p["industry"] == i for p in picked) for i in ("食品饮料", "家用电器")) == 2


def test_percentiles_average_ties():
    pe = [r["pe"] for r in ROWS["rows"][:3]]
    assert screening.percentiles(pe + pe[:1]) == screening.percentiles(pe + pe[:1])  # deterministic
    tied = screening.percentiles([pe[0], pe[0]])
    assert tied == [0.5, 0.5] and screening.percentiles([pe[0]]) == [0.5]


def test_group_return_uses_adjusted_prices_and_cash_seats():
    entry, mark = PRICES["entry"], PRICES["mark"]
    codes = list(entry["prices"])
    # 601600 went ex-dividend after entry (factor 1.108 → 1.1447); adjusted ratio counts the dividend.
    assert screening.group_return(entry, mark, codes, 5, 0.002) == pytest.approx(-0.056287, abs=1e-5)
    # Two filled seats out of five: the empty seats stay at 1.0.
    two = screening.group_return(entry, mark, codes[:2], 5, 0.002)
    assert two == pytest.approx((24.9 / 25.28 + 39.43 / 44.67 + 3) / 5 * 0.998 - 1)
    unfilled = {**entry, "prices": {**entry["prices"], codes[0]: {"filled": False, "reason": "入场日停牌或无行情"}}}
    assert screening.group_return(unfilled, mark, codes[:1], 1, 0) == 0


def test_returns_compare_groups_with_benchmarks():
    batch = {"entry": PRICES["entry"], "marks": {"latest": PRICES["mark"]}, "rule": screening.RULE,
             "l5": {"codes": list(PRICES["entry"]["prices"])}, "q5": list(PRICES["entry"]["prices"]),
             "q20": [{"code": c} for c in PRICES["entry"]["prices"]]}
    out = screening.returns(batch)
    assert out["date"] == "20261023"
    assert out["values"]["L5"] == out["values"]["Q5"] == out["values"]["Q20"]
    assert out["values"]["H00906.CSI"] == pytest.approx(6660.1741 / 6705.857443967639 - 1)


def test_horizon_dates_clamp_to_month_end():
    assert screening._months_after("20260831", 6) == "20270228"
    assert screening._months_after("20261023", -36) == "20231023"


def test_selection_output_must_be_renderable():
    candidates = [{"code": r["code"], "industry": r["industry"], "report": {"verdict": "需要谨慎"}} for r in ROWS["rows"][:6]]
    pick = {"code": candidates[0]["code"], "return_source": "利润修复", "why_over_higher_ranked": "现金更好", "invalidation": "毛利率下滑"}
    assert screening.selection_problems({"picks": [pick], "rejected": [], "gaps": []}, candidates, screening.RULE) == []
    assert screening.selection_problems({"picks": [pick | {"code": []}]}, candidates, screening.RULE)
    assert screening.selection_problems({"picks": [pick], "rejected": [{"code": []}]}, candidates, screening.RULE)
    same = [c for c in candidates if c["industry"] == candidates[0]["industry"]][:3]
    three = [pick | {"code": c["code"]} for c in same]
    assert any("超过" in p for p in screening.selection_problems({"picks": three}, candidates, screening.RULE))


def test_freeze_records_who_froze_and_validates(tmp_path):
    from a_share_research import db, service
    conn = db.connect(tmp_path / "app.db")
    rows = [r | {"verdict": "需要谨慎"} for r in ROWS["rows"][:6]]
    draft = {"picks": [{"code": rows[0]["code"]}]}
    conn.execute("INSERT INTO batches (status, created_at, rule, q20, q5, selection) VALUES ('proposed', ?, ?, ?, ?, ?)",
                 (db.now(), json.dumps(screening.RULE), json.dumps(rows), "[]", json.dumps({"draft": draft})))
    with pytest.raises(service.RequestError):
        screening.freeze(conn, 1, ["000000.SZ"], "", by="auto")
    screening.freeze(conn, 1, [rows[0]["code"]], "", by="auto")
    l5 = json.loads(conn.execute("SELECT l5 FROM batches").fetchone()["l5"])
    assert l5["frozen_by"] == "auto" and l5["changed_from_draft"] is False
    with pytest.raises(service.RequestError):
        screening.freeze(conn, 1, [rows[0]["code"]], "")


def _proposed_batch(conn, draft_codes):
    from a_share_research import db
    rows = [r | {"verdict": "需要谨慎", "return_60d": 0.0, "drawdown": False} for r in ROWS["rows"][:6]]
    conn.execute("INSERT INTO batches (status, created_at, rule, q20, q5, selection) VALUES ('selecting', ?, ?, ?, ?, NULL)",
                 (db.now(), json.dumps(screening.RULE), json.dumps(rows), "[]"))
    return {"draft": {"picks": [{"code": c} for c in draft_codes]}}


def test_draft_is_saved_and_frozen_together(tmp_path):
    from a_share_research import db
    conn = db.connect(tmp_path / "app.db")
    code = ROWS["rows"][0]["code"]
    screening._auto_freeze(conn, 1, _proposed_batch(conn, [code]))
    row = conn.execute("SELECT status, l5 FROM batches").fetchone()
    assert row["status"] == "frozen" and json.loads(row["l5"])["frozen_by"] == "auto"
    # An unfreezable draft is kept, with the reason, for the owner.
    conn.execute("INSERT INTO batches (status, created_at, rule, q20, q5) SELECT 'selecting', created_at, rule, q20, q5 FROM batches")
    screening._auto_freeze(conn, 2, {"draft": {"picks": [{"code": "000000.SZ"}]}})
    row = conn.execute("SELECT status, selection FROM batches WHERE id = 2").fetchone()
    assert row["status"] == "proposed" and "freeze_error" in json.loads(row["selection"])


def test_a_proposed_draft_left_by_a_restart_is_frozen_by_the_worker(tmp_path):
    from a_share_research import db
    conn = db.connect(tmp_path / "app.db")
    selection = _proposed_batch(conn, [ROWS["rows"][0]["code"]])
    conn.execute("UPDATE batches SET status = 'proposed', selection = ?", (json.dumps(selection),))
    assert screening.tick(conn) is True
    assert conn.execute("SELECT status FROM batches").fetchone()["status"] == "frozen"
