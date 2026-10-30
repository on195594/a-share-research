"""Chart geometry and report folds on captured financial series, price bars and report records.

Charts must only redraw saved values: labels and readouts reuse saved display strings, and a
missing or unreconciled value draws no mark rather than a zero.
"""
import copy
import json
import math
import re
from datetime import date
from pathlib import Path

import pytest

from support.templates import ENV
from test_financial_series import CAPTURES, build, frames

from a_share_research import charts, reading, screening
from a_share_research.price import compute
from a_share_research.research import financial_table

ROOT = Path(__file__).resolve().parents[1]
RECORDS = json.loads((ROOT / "tests/data/continuation-records.json").read_text())["records"]


@pytest.mark.parametrize("invalid", ["NaN", "sNaN", "Infinity", "-Infinity", "1e9999", "1e-400", "1e-323", "-1e308", "1e308", "1.7e308"])
def test_invalid_saved_numbers_do_not_reach_svg_geometry(invalid):
    # Corrupt a captured display cell; malformed geometry must not crash the reading page.
    series = build("000858")
    series["annual"]["rows"][0]["values"]["2021-12-31"]["display"] = invalid
    assert charts.number(invalid) is None
    json.dumps(charts.financial_charts({"series": series}, []), allow_nan=False)


def test_corrupted_numeric_geometry_is_missing_in_every_chart_kind():
    for invalid in (-1e308, 1e308, 1.7e308, 1e-323):
        assert charts.columns([{"label": "", "tip": "", "points": [{"text": str(invalid)}]}], [""]) is None
        assert charts.bars([{"value": invalid, "tip": ""}]) is None
        assert charts.paired_bars([{"v0": invalid, "v1": -invalid, "tip": ""}], ("", "")) is None
        assert charts.waterfall([{"from": -invalid, "to": invalid, "tip": ""}]) is None
        assert charts.spark_columns([invalid]) is None
        assert charts.spark_line(["2025-12-31", "2026-03-31"], [invalid, -invalid]) is None


def test_two_year_column_labels_remain_centered_over_their_bars():
    series = build("000858")["annual"]
    row = series["rows"][0]
    groups = [{"label": p[:4], "tip": p, "points": [{"text": row["values"][p]["display"]}]}
              for p in series["periods"][-2:]]
    chart = charts.columns(groups, [row["id"]])
    last = chart["groups"][-1]
    assert not last["label_edge"] and not last["value_edge"]
    html = str(ENV.get_template("_charts.html").module.columns(chart, row["id"]))
    assert f'x="{last["x"]}%" y="{chart["label_y"]}" text-anchor="middle"' in html


@pytest.mark.parametrize("code", CAPTURES)
def test_captured_financial_geometry_stays_inside_plot(code):
    series = build(code)
    figures = charts.financial_charts({"series": series}, financial_table(frames(CAPTURES[code]), {}))
    for figure in figures:
        chart = figure["chart"]
        if figure["kind"] == "columns":
            for group in chart["groups"]:
                for bar in group["bars"]:
                    assert chart["gutter"] <= bar["x"] <= bar["x"] + bar["w"] <= 100
                    assert charts.TOP <= bar["y"] <= bar["y"] + bar["h"] <= charts.TOP + charts.PLOT + 0.01
                    assert bar["label_y"] <= chart["label_y"] - 16
        elif figure["kind"] == "waterfall":
            for row in chart["rows"]:
                assert row["x"] + row["w"] == pytest.approx(max(row["x_from"], row["x_to"]), abs=0.001)
        # A strict JSON encoding catches nonfinite geometry before it reaches SVG attributes.
        json.dumps(chart, allow_nan=False)
    for row in charts.vitals({"series": series})["flows"]:
        for bar in row["spark"]["bars"] if row["spark"] else []:
            if not bar["missing"]:
                assert 0 <= bar["y"] <= bar["y"] + bar["h"] <= charts.SPARK


def test_real_tiny_goodwill_and_negative_cash_keep_proportional_widths():
    series = build("000858")
    balance = {r["id"]: r for r in series["balance"]["rows"]}
    # 五粮液 2021: 0.02 亿元商誉 beside the saved cash balance; previously the 0.8% floor inflated it.
    period = "2021-12-31"
    small, large = [float(balance[k]["values"][period]["display"]) for k in ("goodwill", "cash")]
    rows = [{"v0": small, "v1": large, "tip": period}]
    paired = charts.paired_bars(rows, ("商誉", "货币资金"))
    row = paired["rows"][0]
    assert row["w0"] < 0.8 and row["w0"] / row["w1"] == pytest.approx(small / large, abs=0.0001)
    # An unchanged bridge has equal captured endpoints: its length must be zero.
    bridge = charts.waterfall([{"from": small, "to": small, "tip": period}])
    assert bridge["rows"][0]["w"] == 0
    cash = next(r for r in series["quarterly"]["rows"] if r["id"] == "op_cash")
    negative = cash["values"]["2026-03-31"]["display"]
    column = charts.columns([{"label": "26Q1", "tip": negative, "points": [{"text": negative}]}], ["经营现金流"])
    bar = column["groups"][0]["bars"][0]
    assert bar["negative"] and bar["y"] == column["zero"]
    assert bar["label_y"] <= column["label_y"] - 16
    # Express the captured small amount in 万亿元; tick rounding must not collapse the axis.
    ticks = charts.nice_ticks(0, small / 10000)
    assert ticks[-1] > ticks[0] and all(math.isfinite(t) for t in ticks)


def test_missing_and_zero_extent_keep_finite_geometry():
    # Empty captured report slots remain missing; a zero bridge comes from equal saved endpoints.
    series = build("000858")
    cash = next(r for r in series["balance"]["rows"] if r["id"] == "cash")
    value = float(cash["values"]["2021-12-31"]["display"])
    zero = value - value
    spark = charts.spark_columns([zero])
    assert spark["bars"][0]["h"] == 0 and not spark["bars"][0]["missing"]
    assert charts.spark_columns([None]) is None
    mixed = charts.spark_columns([zero, None, value])
    for bar in mixed["bars"]:
        bar["tip"] = "真实现金值相减" if not bar["missing"] else "缺失"
    html = str(ENV.get_template("_charts.html").module.spark_cols(mixed))
    assert html.count('class="gap"') == 1 and '>—</text>' in html
    column = charts.columns([{"label": "2021", "tip": "同值相减", "points": [{"text": str(zero)}]}], ["变化"])
    assert column["groups"][0]["bars"][0]["h"] == 0
    paired = charts.paired_bars([{"v0": zero, "v1": zero, "tip": "同值相减"}], ("前", "后"))
    assert paired["rows"][0]["w0"] == paired["rows"][0]["w1"] == 0
    json.dumps([spark, column, paired], allow_nan=False)


def figure(figures, title):
    return next(f for f in figures if f["title"].startswith(title))


def drawn(chart):
    return [b for g in chart["groups"] for b in g["bars"] if not b["missing"]]


def test_series_figures_reuse_saved_display_values_and_mark_ttm_apart():
    series = build("000858")
    fin = financial_table(frames(CAPTURES["000858"]), {})
    figures = charts.financial_charts({"series": series}, fin)
    revenue = figure(figures, "营业总收入")
    annual = series["annual"]
    row = next(r for r in annual["rows"] if r["id"] == "revenue")
    texts = [g["bars"][0]["text"] for g in revenue["chart"]["groups"]]
    assert texts[:len(annual["periods"])] == [row["values"][p]["display"] for p in annual["periods"]]
    ttm = series["ttm"]["periods"][-1]
    assert revenue["chart"]["groups"][-1]["outline"] is (ttm > annual["periods"][-1])
    last = row["values"][annual["periods"][-1]]
    assert revenue["headline"].startswith(f"{annual['periods'][-1][:4]} 年 {last['display']} 亿元")
    # Every bar sits on the zero line: positive bars end there, negative ones start there.
    zero = revenue["chart"]["zero"]
    for chart in (f["chart"] for f in figures if f["kind"] == "columns"):
        for bar in drawn(chart):
            assert abs((bar["y"] + bar["h"] if not bar["negative"] else bar["y"]) - chart["zero"]) < 0.02
    assert all(b["y"] + b["h"] <= zero + 0.02 for b in drawn(revenue["chart"]))


def vital(panel, label):
    return next(r for r in panel["flows"] + panel["stocks"] if r["label"] == label)


def test_missing_and_unreconciled_cells_draw_nothing_and_say_why():
    series = build("000858")
    quarterly = series["quarterly"]
    target = quarterly["periods"][-2]
    row = next(r for r in quarterly["rows"] if r["id"] == "net_profit")
    reconciled = copy.deepcopy(series)
    cell = next(r for r in reconciled["quarterly"]["rows"] if r["id"] == "net_profit")["values"][target]
    cell.update(display=None, value=None, unreconciled={target: "2026-01-01"})
    before = vital(charts.vitals({"series": series}), "归母净利润")
    after = vital(charts.vitals({"series": reconciled}), "归母净利润")
    index = quarterly["periods"][-8:].index(target)
    assert not before["spark"]["bars"][index]["missing"] and "h" in before["spark"]["bars"][index]
    assert before["spark"]["bars"][index]["tip"].startswith(f"{charts._period_label(target, 'quarterly')} 归母净利润："
                                                            f"{row['values'][target]['display']} 亿元")
    assert after["spark"]["bars"][index]["missing"] and "h" not in after["spark"]["bars"][index]
    assert after["spark"]["bars"][index]["tip"].endswith("更正版本待核对")
    html = str(ENV.get_template("_charts.html").module.vitals(charts.vitals({"series": reconciled})))
    assert html.count(after["spark"]["bars"][index]["tip"]) == 3  # data-tip, <title> and the data list


def test_vitals_reuse_saved_latest_values_and_sit_on_zero():
    series = build("000858")
    panel = charts.vitals({"series": series})
    periods = series["quarterly"]["periods"][-8:]
    for key, label in charts.FLOWS:
        saved = next(r for r in series["quarterly"]["rows"] if r["id"] == key)["values"]
        r = vital(panel, label)
        assert r["latest"] == saved[periods[-1]].get("display")
        assert r["yoy"] == (saved[periods[-1]].get("yoy") or {}).get("value")
        assert len(r["data"]) == len(periods)
        spark = r["spark"]
        for bar, p in zip(spark["bars"], periods):
            value = charts.number(saved[p].get("display"))
            assert bar["missing"] is (value is None)
            if value is not None:  # positive bars end on the zero line, negative ones start there
                assert abs((bar["y"] if value < 0 else bar["y"] + bar["h"]) - spark["zero"]) < 0.02
        assert spark["bars"][-1]["last"] and not any(b["last"] for b in spark["bars"][:-1])
    assert panel["flow_period"] == charts._period_label(periods[-1], "quarterly")


def test_vitals_keep_a_balance_row_with_too_few_values():
    # 卫星化学: 四项债务合计 has a complete input only at its first period end, before the last eight.
    series = build("002648")
    debt = vital(charts.vitals({"series": series}), "四项债务合计")
    assert debt["spark"] is None and debt["latest"] is None
    assert len(debt["data"]) == 8 and all(t.endswith("缺失") for t in debt["data"])
    cash = vital(charts.vitals({"series": series}), "货币资金")
    assert cash["latest"] == "142.15" and cash["spark"]["dots"][-1]["last"]
    html = str(ENV.get_template("_charts.html").module.vitals(charts.vitals({"series": series})))
    assert "不足两期" in html


def test_vitals_skip_groups_without_periods_and_keep_single_readings():
    series = build("000858")
    empty = copy.deepcopy(series)
    empty["balance"]["periods"] = []  # the generator keeps metric rows even with no period ends
    panel = charts.vitals({"series": empty})
    assert panel["stocks"] == [] and len(panel["flows"]) == len(charts.FLOWS)
    # No quarters, and each balance row keeps only its latest value: no trend, but still a reading.
    single = copy.deepcopy(series)
    single["quarterly"]["periods"] = []
    last = single["balance"]["periods"][-1]
    for row in single["balance"]["rows"]:
        for period, cell in row["values"].items():
            if period != last:
                cell.update(display=None, value=None)
    panel = charts.vitals({"series": single})
    assert panel["flows"] == [] and all(r["spark"] is None for r in panel["stocks"])
    saved = {r["id"]: r["values"][last]["display"] for r in series["balance"]["rows"]}
    assert vital(panel, "货币资金")["latest"] == saved["cash"]
    html = str(ENV.get_template("_charts.html").module.vitals(panel))
    assert "不足两期" in html and saved["cash"] in html and "<th scope=\"col\">单季</th>" not in html


def test_balance_values_without_neighbours_are_drawn_as_dots():
    series = build("000858")
    periods = series["balance"]["periods"][-8:]
    gapped = copy.deepcopy(series)
    cash = next(r for r in gapped["balance"]["rows"] if r["id"] == "cash")["values"]
    for period in periods[1::2]:  # keep every other period end, the latest among the blanks
        cash[period].update(display=None, value=None)
    spark = vital(charts.vitals({"series": gapped}), "货币资金")["spark"]
    assert spark["segments"] == [] and len(spark["dots"]) == 4 and all(d["isolated"] for d in spark["dots"])
    html = str(ENV.get_template("_charts.html").module.spark_line(spark))
    assert html.count("<circle") == 4
    full = vital(charts.vitals({"series": series}), "货币资金")["spark"]
    assert str(ENV.get_template("_charts.html").module.spark_line(full)).count("<circle") == 1


def test_balance_year_ago_column_reuses_the_saved_period_end():
    series = build("000858")
    periods = series["balance"]["periods"]
    year_ago = f"{int(periods[-1][:4]) - 1}{periods[-1][4:]}"
    panel = charts.vitals({"series": series})
    assert year_ago in periods and panel["year_ago"] == charts._period_label(year_ago, "balance")
    for key, label in charts.STOCKS:
        row = next((r for r in series["balance"]["rows"] if r["id"] == key), None)
        if row:
            assert vital(panel, label)["year_ago"] == (row["values"].get(year_ago) or {}).get("display")


def test_dumbbell_compares_saved_ratios_with_the_same_period_a_year_earlier():
    body = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 37)
    fin = [r for r in body["table"] if r["id"].startswith("T.")]
    chart = charts.dumbbell(fin)
    latest = max(p for r in fin for p in r["values"])
    prior = f"{int(latest[:4]) - 1}{latest[4:]}"
    assert (chart["latest"], chart["prior"]) == (latest, prior)
    table = {r["id"]: r["values"] for r in fin}
    for (key, label), row in zip([k for k in charts.RATIOS if k[0] in table], chart["rows"]):
        assert (row["label"], row["after"], row["before"]) == (label, table[key][latest], table[key].get(prior))
        assert row["x_after"] < row["x_before"] if float(row["after"]) < float(row["before"]) else row["x_after"] >= row["x_before"]
    # Without the year-earlier value only the latest dot is drawn and the readout says so.
    alone = copy.deepcopy(fin)
    next(r for r in alone if r["id"] == "T.gross_margin")["values"].pop(prior)
    row = charts.dumbbell(alone)["rows"][0]
    assert row["x_before"] is None and f"{prior} 缺失" in row["tip"]


def test_reports_without_series_fall_back_to_the_saved_financial_table():
    body = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 37)
    fin = [r for r in body["table"] if r["id"].startswith("T.")]
    figures = charts.financial_charts(body, fin)
    revenue = figure(figures, "营业总收入")
    assert revenue["source"] == "财务数据表"
    saved = next(r for r in fin if r["id"] == "T.revenue")["values"]
    years = sorted(p for p in saved if p.endswith("-12-31"))
    assert [g["bars"][0]["text"] for g in revenue["chart"]["groups"]] == [saved[p] for p in years]
    growth = figure(figures, "最新一期同比")
    latest = max(p for r in fin for p in r["values"])
    assert growth["chart"]["rows"][0]["text"] == next(r for r in fin if r["id"] == "T.revenue_yoy")["values"][latest] + "%"
    assert not any(f["title"].startswith("单季") for f in figures)


def test_line_positions_follow_calendar_and_legend_never_carries_an_older_value():
    # 卫星化学: 四项债务合计 has a complete input only at its first period end, cash at every one.
    figures = charts.financial_charts({"series": build("002648")}, [])
    balance = figure(figures, "货币资金与四项债务合计")
    periods = build("002648")["balance"]["periods"]
    chart = balance["chart"]
    cash, debt = chart["series"]
    xs = [d["x"] for d in cash["dots"]]
    days = [date.fromisoformat(p).toordinal() for p in periods]
    for i in range(1, len(periods) - 1):
        assert abs((xs[i] - xs[i - 1]) / (xs[i + 1] - xs[i]) - (days[i] - days[i - 1]) / (days[i + 1] - days[i])) < 0.01
    assert debt["segments"] == [] and len(debt["dots"]) == 1
    assert debt["latest"] is None and cash["latest"] == "142.15" and chart["latest_period"] == periods[-1]
    assert periods[-1] in balance["headline"] and "有息" not in balance["title"] + debt["label"]
    html = str(ENV.get_template("_charts.html").module.figure(balance))
    assert "164.15" not in html.split('<ul class="legend">')[1].split("</ul>")[0]
    assert "（该期缺失）" in html
    # The data list is the HTML equivalent: every period with both series, as in the readout.
    assert html.count("<li>" + periods[0]) == 1 and "四项债务合计 164.15" in html


# Every price reference saved in production by 2026-10-30 (prices rows 1–8), including 比亚迪
# with its close 0.39% above the invalidation line.
SAVED_LEVELS = {
    "600900.SH": {"close": 29.69, "ma20": 28.79, "ma60": 28.5, "trigger": 29.81, "stop": 28.33},
    "600519.SH": {"close": 1331.88, "ma20": 1272.7, "ma60": 1298.18, "trigger": 1348.61, "stop": 1236.05},
    "002594.SZ": {"close": 80.02, "ma20": 83.33, "ma60": 87.56, "trigger": 87.21, "stop": 79.71},
    "601138.SH": {"close": 48.85, "ma20": 54.58, "ma60": 60.06, "trigger": 64.95, "stop": 45.55},
    "300750.SZ": {"close": 237.6, "ma20": 279.38, "ma60": 340.81, "trigger": 310.01, "stop": 216.51},
    "688208.SH": {"close": 24.51, "ma20": 24.14, "ma60": 25.37, "trigger": 25.7, "stop": 21.96},
    "601899.SH": {"close": 27.47, "ma20": 29.73, "ma60": 31.95, "trigger": 31.78, "stop": 26.6},
    "603606.SH": {"close": 36.03, "ma20": 35.19, "ma60": 36.23, "trigger": 37.77, "stop": 32.01},
}


def spans(ladder):
    out = []
    for m in ladder["marks"]:
        width, center = charts.text_width(m["label"]), m["x"] * 3
        left = {"start": center, "end": center - width}.get(m["anchor"], center - width / 2)
        out.append((m["side"], m["lane"], left, left + width, center))
    return out


def test_price_ladder_labels_never_overlap_or_cross_leaders_on_saved_levels():
    data = json.loads((ROOT / "tests/data/600900-61d.json").read_text())
    computed = compute(data["stock"], data["benchmark"])
    assert {k: computed[k] for k in SAVED_LEVELS["600900.SH"]} == SAVED_LEVELS["600900.SH"]
    for code, levels in SAVED_LEVELS.items():
        ladder = charts.price_ladder(levels)
        close = next(m for m in ladder["marks"] if m["key"] == "close")
        assert (close["side"], close["lane"]) == ("above", 0), code
        placed = spans(ladder)
        for a in placed:
            for b in placed:
                if a is b or a[0] != b[0]:
                    continue
                if a[1] == b[1]:
                    assert a[3] <= b[2] or b[3] <= a[2], code
                if b[1] < a[1]:
                    assert not b[2] <= a[4] <= b[3], code
        assert {m["label"] for m in ladder["marks"]} == {
            f"{name} {levels[key]:.2f}" for key, name in
            (("close", "收盘"), ("stop", "失效线"), ("trigger", "观察线"), ("ma60", "MA60"), ("ma20", "MA20"))}


def test_member_returns_reuse_group_return_and_portfolio_matches():
    prices = json.loads((ROOT / "tests/data/screen-prices.json").read_text())
    codes = list(prices["entry"]["prices"])
    batch = {"entry": prices["entry"], "marks": {"latest": prices["mark"]},
             "rule": {"cost": 0.002, "picks": 5, "benchmarks": {"H00906.CSI": "中证800全收益"}},
             "l5": {"codes": codes}, "q5": codes, "q20": [{"code": c, "name": c} for c in codes]}
    results = screening.returns(batch)
    performance = charts.batch_returns(batch, results, {})
    members = performance["members"]["rows"]
    for code, row in zip(codes, members):
        expected = screening.group_return(prices["entry"], prices["mark"], [code], 1, 0.002)
        assert row["text"] == f"{expected * 100:+.2f}%"
    assert performance["groups"]["rows"][0]["text"] == f"{results['values']['L5'] * 100:+.2f}%"
    assert performance["groups"]["rows"][0]["emphasis"]
    assert (performance["entry"], performance["date"]) == (prices["entry"]["date"], prices["mark"]["date"])


def test_verdict_track_uses_reviewed_original_reports_with_their_questions():
    rows = []
    for record in RECORDS:
        report, run = record["report"], record["run"]
        if report["code"] != "600519.SH":
            continue
        rows.append({**{k: report[k] for k in ("id", "passed", "verdict", "created_at", "period")},
                     "replay_of": (run["identity"] or {}).get("replay_of"),
                     "question": (run["request"] or {}).get("question") or report["body"].get("question")})
    rows.sort(key=lambda r: r["id"], reverse=True)  # the company page lists newest first
    track = charts.verdict_track(rows)
    expected = [r for r in reversed(rows) if r["passed"] and not r["replay_of"]]
    assert expected and len(expected) < len(rows)  # the captured set includes replays
    assert [p["id"] for p in track["points"]] == [r["id"] for r in expected]
    for point, row in zip(track["points"], expected):
        assert point["y"] == 16 + 30 * (2 - charts.VERDICTS.index(row["verdict"]))
        assert ("问题：" + row["question"][:40]) in point["tip"] if row["question"] else "未记录具体问题" in point["tip"]


def test_ledger_counts_distinct_quotes_and_calculations_actually_cited():
    body = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 37)
    rows = {r["id"]: r for r in body["table"]}
    ledger = reading.evidence_ledger(body, rows, reading.evidence_context(body, None))
    quotes = {(c.get("source") or "D0", c.get("page"), c["quote"]) for key in ("answer", "risks", "business", "financial", "checkpoints", "methods")
              for item in body.get(key) or [] for c in item.get("cites") or [] if not c.get("ref") and c.get("quote")}
    assert ledger["quotes"] == len(quotes) > 0
    cited = {c["ref"] for key in ("answer", "risks", "business", "financial", "checkpoints", "methods")
             for item in body.get(key) or [] for c in item.get("cites") or [] if c.get("ref")}
    assert ledger["calculations"] == len({ref for ref in cited if rows[ref].get("calculation")}) == 3
    assert len(cited) > 3  # plain table rows are cited too and are not counted as calculations
    assert ledger["risks"] == len(body["risks"]) and ledger["gaps"] == len(dict.fromkeys(body.get("gaps") or []))
    doubled = copy.deepcopy(body)
    doubled["risks"].append(copy.deepcopy(doubled["risks"][0]))
    assert reading.evidence_ledger(doubled, rows, {})["quotes"] == ledger["quotes"]


def test_folded_items_keep_claim_ids_and_open_for_a_focused_claim():
    body = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 37)
    answers = [item for key in ("risks", "business", "financial", "checkpoints") for item in body.get(key) or []][:7]
    assert len(answers) == 7
    macros = ENV.get_template("_macros.html").module
    html = str(macros.items(answers, {}, "回答", {}, "answer", "", limit=4))
    assert re.findall(r'id="claim-answer-(\d+)"', html) == [str(i) for i in range(7)]
    assert '展开其余 3 条' in html and '<details class="more">' in html
    assert '<details class="more" open>' in str(macros.items(answers, {}, "回答", {}, "answer", "answer:5", limit=4))
    assert '<details class="more">' in str(macros.items(answers, {}, "回答", {}, "answer", "answer:2", limit=4))
    # Hiding a single entry saves nothing, so five entries stay unfolded.
    assert '<details class="more"' not in str(macros.items(answers[:5], {}, "回答", {}, "answer", "", limit=4))


def test_waterfall_cash_bridge_and_paired_balance_structure_on_real_series_and_table():
    series = build("000858")
    fin = financial_table(frames(CAPTURES["000858"]), {})
    figures = charts.financial_charts({"series": series}, fin)
    assert not any("近12月滚动" in f["title"] for f in figures)
    bridge = figure(figures, "盈利与现金流转化")
    assert bridge["kind"] == "waterfall"
    wf = bridge["chart"]
    assert [s["label"] for s in wf["rows"]] == [
        "归母净利润", "扣非归母净利润", "经营活动现金流净额", "购建长期资产支付的现金", "经营现金流减购建长期资产现金"
    ]
    assert wf["rows"][0]["role"] == "profit" and wf["rows"][3]["role"] == "step" and wf["rows"][4]["role"] == "fcf"
    assert len(wf["connectors"]) == 2
    html = str(ENV.get_template("_charts.html").module.figure(bridge))
    assert "chart waterfall" in html and 'class="connector"' in html
    # Mutation check: a missing step or missing FCF in waterfall draws no capex bridge bar and suppresses connectors.
    gapped = copy.deepcopy(series)
    latest_ann = gapped["annual"]["periods"][-1]
    next(r for r in gapped["annual"]["rows"] if r["id"] == "capex")["values"][latest_ann].update(display=None, value=None)
    wf_gapped = figure(charts.financial_charts({"series": gapped}, fin), "盈利与现金流转化")["chart"]
    assert wf_gapped["rows"][3]["missing"] and wf_gapped["rows"][3]["w"] == 0.0
    assert wf_gapped["connectors"] == []
    no_fcf = copy.deepcopy(series)
    next(r for r in no_fcf["annual"]["rows"] if r["id"] == "fcf")["values"][latest_ann].update(display=None, value=None)
    wf_no_fcf = figure(charts.financial_charts({"series": no_fcf}, fin), "盈利与现金流转化")["chart"]
    assert wf_no_fcf["rows"][3]["missing"] and wf_no_fcf["connectors"] == []
    # Paired balance structure on series and legacy table fallback; omitted when year-earlier period is absent.
    struct = figure(figures, "资金与营运结构")
    assert struct["kind"] == "paired" and len(struct["chart"]["rows"]) >= 3
    no_prior = copy.deepcopy(series)
    latest_bal = no_prior["balance"]["periods"][-1]
    prior_bal = f"{int(latest_bal[:4]) - 1}{latest_bal[4:]}"
    no_prior["balance"]["periods"] = [p for p in no_prior["balance"]["periods"] if p != prior_bal]
    assert not any(f["title"].startswith("资金与营运结构") for f in charts.financial_charts({"series": no_prior}, []))
    legacy_body = next(r["report"]["body"] for r in RECORDS if r["report"]["id"] == 37)
    legacy_fin = [r for r in legacy_body["table"] if r["id"].startswith("T.")]
    legacy_figs = charts.financial_charts(legacy_body, legacy_fin)
    assert figure(legacy_figs, "盈利与现金流转化")["source"] == "财务数据表"
    assert figure(legacy_figs, "资金与营运结构")["source"] == "财务数据表"


def test_vitals_falls_back_to_financial_table_and_stock_template_labels_source():
    rec = next(r for r in RECORDS if r["report"]["id"] == 37)
    body = rec["report"]["body"]
    fin = [r for r in body["table"] if r["id"].startswith("T.")]
    assert charts.vitals(body) is None
    panel = charts.vitals(body, fin)
    assert panel["flow_heading"] == "历年" and panel["source"] == "财务数据表"
    assert panel["flow_period"] == "26年上半年" and panel["span_title"] == "历年 3 期与最新期"
    saved_rev = next(r for r in fin if r["id"] == "T.revenue")["values"]
    rev = vital(panel, "营业总收入")
    latest = max(saved_rev)
    assert rev["latest"] == saved_rev[latest]
    # Mutation check: removing a period's value marks the spark slot missing rather than zero.
    mutated = copy.deepcopy(fin)
    years = sorted(p for p in saved_rev if p.endswith("-12-31"))
    next(r for r in mutated if r["id"] == "T.revenue")["values"].pop(years[0])
    rev_after = vital(charts.vitals(body, mutated), "营业总收入")
    assert rev_after["spark"]["bars"][0]["missing"] and "h" not in rev_after["spark"]["bars"][0]
    # Stock page template must report the vitals source ("财务数据表") rather than hardcoding "经营序列".
    report_meta = {**rec["report"], "replay_of": None, "question": body.get("question"),
                   "parent_report_id": None, "feedback_text": None, "prior_report_id": None}
    stock_html = ENV.get_template("stock.html").render(
        reports=[report_meta], code=rec["report"]["code"], vitals=panel, vitals_report=report_meta,
        comparison={"old": None, "reason": "未记录明确关联", "diff": None, "excluded": []},
        price=None, csrf="t", request_id="r", track=None, due=None,
        card=reading.card(rec["report"], body) | {"coverage": "2026-06-30", "created_at": rec["report"]["created_at"]},
    )
    assert "保存的财务数据表" in stock_html and "保存的经营序列" not in stock_html


def test_batch_overview_sparks_market_strip_and_verdict_summary_on_captured_data():
    matrix = json.loads((ROOT / "tests/data/batch-candidate-matrix.json").read_text())
    labels = json.loads((ROOT / "tests/data/batch-factor-labels.json").read_text())
    batch = {"q20": matrix["rows"], "l5": {"codes": matrix["l5"]},
             "universe": {"markets": matrix["markets"]}}
    overview = charts.batch_overview(batch)
    assert overview and len(overview["momentum"]["rows"]) == len(matrix["rows"])
    assert any(r["emphasis"] for r in overview["industries"]["rows"])
    sparks = charts.candidate_sparks(labels["rows"])
    assert set(sparks) == {r["code"] for r in labels["rows"]}
    for code, sp in sparks.items():
        assert len(sp["spark"]["bars"]) == 4 and sp["spark"]["bars"][-1]["last"]
    strip = charts.market_strip(batch)
    assert strip["total"] == sum(matrix["markets"].values())
    assert abs(sum(s["w"] for s in strip["segments"]) - 100.0) < 0.05
    # Verify batch.html keeps the board color legend outside .readout so hovering never erases it.
    batch_tpl = (ROOT / "a_share_research/templates/batch.html").read_text()
    start = batch_tpl.index("{% if market_strip is defined and market_strip %}")
    end = batch_tpl.index("{% set gaps =", start)
    strip_html = ENV.from_string(batch_tpl[start:end]).render(
        market_strip=strip, batch={"universe": {"markets": matrix["markets"]}}
    )
    readout_part = strip_html.split('<p class="readout')[1].split("</p>")[0]
    assert "seg-dot" in strip_html and "seg-dot" not in readout_part
    # Verdict summary on real captured reports from RECORDS, using sqlite3.Row as web.latest_reports() does.
    import sqlite3
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE t (code TEXT, name TEXT, id INT, verdict TEXT, passed INT, coverage TEXT, created_at TEXT, from_batch INT, checkpoint INT)")
    for r in RECORDS[:5]:
        rep = r["report"]
        mem.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
                    (rep["code"], rep["name"], rep["id"], rep["verdict"], rep["passed"], rep["period"], rep["created_at"]))
    companies = mem.execute("SELECT * FROM t").fetchall()
    summary = charts.verdict_summary(companies)
    assert summary["total"] == len(companies)
    assert all(summary["buckets"][c["code"]] in ("值得继续研究", "需要谨慎", "暂不值得", "复核未通过") for c in companies)
    js = (ROOT / "a_share_research/static/app.js").read_text()
    assert ":scope > .items details.source, :scope > details.more details.source" in js
