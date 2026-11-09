"""Screen factors, ranking and simulated returns on real TuShare data captured 2026-10-24.

Factor inputs were checked against the 2025 annual reports: 格力电器 扣非净利润 27,706,292,436.94
(p7), 经营现金流 46,383,114,754.02 (p7), 净利润 28,862,746,016.16 (p88); 卫星化学 6,291,539,606.92
(p8), 9,606,885,402.76 (p8), 5,305,340,636.39 (p71).
"""

from collections import Counter
import copy
import json
import math
from pathlib import Path

import pandas as pd
import pytest

from a_share_research import batches, screening
from a_share_research.batches import NEW_RULE, RULE, small_rule
from a_share_research.screening_factors import (
    liquidity_mask, market_cap_mask, market_filters, pick, rank_candidates,
)

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
    pe = ROWS["rows"][0]["pe"]
    tied = screening.percentiles([pe, pe])
    assert tied == [0.5, 0.5] and screening.percentiles([pe]) == [0.5]


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
    row = conn.execute("SELECT status, selection, l5 FROM batches WHERE id = 2").fetchone()
    assert row["status"] == "proposed" and row["l5"] is None and "freeze_error" in json.loads(row["selection"])


def test_a_proposed_draft_left_by_a_restart_is_frozen_by_the_worker(tmp_path):
    from a_share_research import db
    conn = db.connect(tmp_path / "app.db")
    selection = _proposed_batch(conn, [ROWS["rows"][0]["code"]])
    conn.execute("UPDATE batches SET status = 'proposed', selection = ?", (json.dumps(selection),))
    assert screening.tick(conn) is True
    assert conn.execute("SELECT status FROM batches").fetchone()["status"] == "frozen"


# TuShare income for 002102.SZ FY2023, captured 2026-10-25: the restatement keeps the original ann_date,
# both rows carry update_flag 1, and only f_ann_date tells them apart.
RESTATED = pd.DataFrame([
    {"ann_date": "20240419", "f_ann_date": "20260429", "end_date": "20231231", "report_type": "1",
     "n_income": 248361059.72, "update_flag": "1"},
    {"ann_date": "20240419", "f_ann_date": "20240419", "end_date": "20231231", "report_type": "1",
     "n_income": 269479234.80, "update_flag": "1"},
])


def test_restated_statement_is_used_only_after_it_was_announced():
    pick = lambda as_of: screening.annual_rows(screening.announced_by(RESTATED, as_of), consolidated=True)
    assert pick("20260430").loc["20231231", "n_income"] == 248361059.72
    assert pick("20250430").loc["20231231", "n_income"] == 269479234.80


def test_windfall_flag_needs_both_a_profit_jump_and_a_low_multiple():
    # Factor values of the 2026-10 batch (as of 2026-10-23).
    assert screening.windfall({"growth": 1.5721894490314785, "pe": 8.3101}) == {"min_growth": 0.5, "max_pe": 10}  # 株冶集团
    assert screening.windfall({"growth": 0.27378284819584753, "pe": 7.9025}) is None  # 卫星化学: low PE, modest growth
    assert screening.windfall({"growth": 0.7764, "pe": 16.2376}) is None  # 燕京啤酒: jump at a normal multiple


def test_comparison_keeps_real_calculations_and_marks_legacy_gaps(tmp_path):
    from a_share_research import db
    from a_share_research.research_tools import calculate
    from support.reports import MATERIAL, report

    conn = db.connect(tmp_path / 'app.db')
    calculation = calculate(MATERIAL.table, '经营现金流差额', 'difference',
                            {'ref': 'T.op_cash', 'period': '2025-12-31'},
                            {'ref': 'T.op_cash', 'period': '2024-12-31'})
    body = report() | {'table': MATERIAL.table + [calculation]}
    run = db.create_run(conn, MATERIAL.code)
    db.save_report(conn, run, MATERIAL.code, MATERIAL.name, MATERIAL.doc.period, True, body['verdict'], body)
    report_id = conn.execute('SELECT id FROM reports').fetchone()[0]
    digest = screening._report_digest(conn, report_id)
    assert digest['business'] == [r['text'] for r in body['business']]
    assert digest['checkpoints'] == [r['text'] for r in body['checkpoints']]
    assert next(r for r in digest['metrics'] if r['id'] == calculation['id']) == calculation
    assert digest['missing_metrics']['T.capex'] == ['2024-12-31', '2025-12-31']
    conn.close()


def test_forced_ranking_must_cover_every_reviewed_candidate_once():
    candidates = [{"code": r["code"], "report": {"verdict": "需要谨慎"}} for r in ROWS["rows"][:5]]
    candidates[4]["report"] = {"verdict": "暂不值得"}  # still ranked: spotting losers is the point
    candidates.append({"code": ROWS["rows"][5]["code"], "report": None})  # failed review: nothing to judge
    codes = [c["code"] for c in candidates[:5]]
    assert screening.rankable(candidates) == codes
    assert screening.ranking_problems({"order": codes[::-1], "basis": "估值与现金流"}, candidates) == []
    assert screening.ranking_problems({"order": codes[:4], "basis": "x"}, candidates) == [f"order 缺少：{codes[4]}"]
    assert screening.ranking_problems({"order": codes + [codes[0]], "basis": "x"}, candidates) == ["order 中代码不能重复"]
    assert screening.ranking_problems({"order": codes + [candidates[5]["code"]], "basis": "x"}, candidates)
    assert screening.ranking_problems({"order": codes, "basis": ""}, candidates) == ["basis 必须是非空字符串"]
    assert screening.ranking_problems(None, candidates) == ["order 必须是候选代码字符串数组"]
    prompt = screening.ranking_prompt(candidates, screening.RULE)
    assert candidates[5]["code"] not in prompt and "中证800全收益" in prompt


def test_returns_add_ranking_halves_and_skip_the_middle_of_an_odd_count():
    codes = list(PRICES["entry"]["prices"])
    batch = {"entry": PRICES["entry"], "marks": {"latest": PRICES["mark"]}, "rule": screening.RULE,
             "l5": {"codes": codes}, "q5": codes, "q20": [{"code": c} for c in codes],
             "selection": {"ranking": {"order": codes}}}
    half = len(codes) // 2
    assert batches.ranking_halves(batch) == (codes[:half], codes[-half:])
    out = screening.returns(batch)["values"]
    assert out["TOP"] == pytest.approx(screening.group_return(PRICES["entry"], PRICES["mark"], codes[:half], half, 0.002))
    assert out["BOTTOM"] == pytest.approx(screening.group_return(PRICES["entry"], PRICES["mark"], codes[-half:], half, 0.002))
    unranked = screening.returns(batch | {"selection": {"ranking": {"order": None, "error": "x"}}})["values"]
    assert "TOP" not in unranked and "BOTTOM" not in unranked


# --- Small-Cap Screening Tests (absorbed from test_screening_small) ---

CAPTURED_SMALL = json.loads((DATA / "screen-small-rows.json").read_text())
SMALL_RULE_OBJ = small_rule(NEW_RULE)


def _codes(rows):
    return [r["code"] for r in rows]


def test_small_units_include_30_yi_floor_and_exclude_100_yi_ceiling():
    low, high = 30e8 / 1e4, 100e8 / 1e4
    values = pd.Series([math.nextafter(low, -math.inf), low,
                        math.nextafter(high, -math.inf), high])
    assert market_cap_mask(values, SMALL_RULE_OBJ).tolist() == [False, True, True, False]
    assert market_cap_mask(values, RULE).tolist() == [False, False, False, True]


def test_small_liquidity_3000_wan_is_inclusive_in_tushare_thousands():
    boundary = 3000e4 / 1e3
    values = pd.Series([math.nextafter(boundary, -math.inf), boundary,
                        math.nextafter(boundary, math.inf), 1e8 / 1e3])
    assert liquidity_mask(values, SMALL_RULE_OBJ).tolist() == [False, True, True, True]
    assert liquidity_mask(values, RULE).tolist() == [False, False, False, True]


def test_real_market_rows_stay_in_separate_pools():
    frame = pd.DataFrame(CAPTURED_SMALL["large_market_rows"] + CAPTURED_SMALL["small_market_rows"])
    args = (frame, set(CAPTURED_SMALL["suspended"]), CAPTURED_SMALL["as_of"])
    large, large_funnel, _ = market_filters(*args, RULE)
    small, small_funnel, _ = market_filters(*args, SMALL_RULE_OBJ)
    assert set(large.ts_code) == {r["ts_code"] for r in CAPTURED_SMALL["large_market_rows"]}
    assert set(small.ts_code) == {r["ts_code"] for r in CAPTURED_SMALL["small_market_rows"]}
    assert set(large.ts_code).isdisjoint(small.ts_code)
    assert large_funnel["listed"] == small_funnel["listed"] == len(frame)
    assert large_funnel["after_industry_mapping"] == len(large)
    assert small_funnel["after_industry_mapping"] == len(small)


def test_frozen_v11_rankings_and_candidates_are_unchanged():
    rows = copy.deepcopy(CAPTURED_SMALL["large_companies"])
    before = copy.deepcopy(rows)
    ranked, q20, q5 = rank_candidates(rows, RULE)
    assert [{"code": r["code"], "score": r["score"]} for r in ranked] == CAPTURED_SMALL["frozen_large_ranked"]
    assert q20 == pick(ranked, 20, 3)
    assert q5 == _codes(pick(q20, 5, 2))
    assert rows == before
    live = json.loads((DATA / "screen-rows.json").read_text())["rows"]
    ranked, _, _ = rank_candidates([r | {"status": "ok"} for r in live], RULE)
    assert _codes(ranked) == _codes(live)
    assert [r["score"] for r in ranked] == [r["score"] for r in live]


def test_small_ranks_are_local_and_do_not_change_large_ranks():
    small_rows = copy.deepcopy(CAPTURED_SMALL["small_companies"])
    large_rows = copy.deepcopy(CAPTURED_SMALL["large_companies"])
    small, _, _ = rank_candidates(small_rows, SMALL_RULE_OBJ)
    large, _, _ = rank_candidates(large_rows, NEW_RULE)
    assert [{"code": r["code"], "score": r["score"]} for r in small] == CAPTURED_SMALL["frozen_small_ranked"]
    assert [{"code": r["code"], "score": r["score"]} for r in large] == CAPTURED_SMALL["frozen_large_ranked"]
    counts = Counter(r["industry"] for r in small_rows)
    assert all(r["group_size"] == counts[r["industry"]] for r in small)
    assert all(r["small_group"] == (counts[r["industry"]] < 10) for r in small)
    combined = {r["code"]: r for r in screening.score(small_rows + large_rows, RULE)}
    assert any(r["score"] != combined[r["code"]]["score"] for r in small)


def test_s10_and_sq3_have_their_own_seats_and_industry_limits():
    _, s10, sq3 = rank_candidates(CAPTURED_SMALL["small_companies"], SMALL_RULE_OBJ)
    assert len(s10) == 10 and max(Counter(r["industry"] for r in s10).values()) == 2
    assert len(sq3) == 3 and sq3 == _codes(pick(s10, 3, 1))
    industry = {r["code"]: r["industry"] for r in s10}
    assert max(Counter(industry[c] for c in sq3).values()) == 1
    assert SMALL_RULE_OBJ["mechanical_per_industry"] == SMALL_RULE_OBJ["picks_per_industry"] == 1


def test_sq3_and_sl3_share_industry_constraints_and_leave_the_same_cash_seat():
    rows = [r for r in CAPTURED_SMALL["small_companies"] if r["industry"] in {"食品饮料", "家用电器"}]
    _, s10, sq3 = rank_candidates(rows, SMALL_RULE_OBJ)
    assert len(sq3) == 2
    assert sq3 == _codes(pick(s10, 3, SMALL_RULE_OBJ["picks_per_industry"]))


def test_saved_v12_rule_retains_its_original_sq3_and_s10():
    legacy = NEW_RULE | {"version": "screen-v1.2", "small": NEW_RULE["small"] | {"mechanical_per_industry": 2}}
    rows = CAPTURED_SMALL["small_companies"]
    old_ranked, old_s10, old_sq3 = rank_candidates(rows, small_rule(legacy))
    ranked, s10, _ = rank_candidates(rows, SMALL_RULE_OBJ)
    assert NEW_RULE["version"] == "screen-v1.3"
    assert ranked == old_ranked and s10 == old_s10
    assert old_sq3 == _codes(pick(old_s10, 3, 2))
    two_industries = [r for r in rows if r["industry"] in {"食品饮料", "家用电器"}]
    assert len(rank_candidates(two_industries, small_rule(legacy))[2]) == 3
    assert len(rank_candidates(two_industries, SMALL_RULE_OBJ)[2]) == 2
