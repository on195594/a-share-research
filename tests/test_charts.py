"""Chart geometry and report folds on captured financial series, price bars and report records.

Charts must only redraw saved values: labels and readouts reuse saved display strings, and a
missing or unreconciled value draws no mark rather than a zero.
"""
import copy
import json
import re
from datetime import date
from pathlib import Path

from support.templates import ENV
from test_financial_series import CAPTURES, build, frames

from a_share_research import charts, reading, screening
from a_share_research.price import compute
from a_share_research.research import financial_table

ROOT = Path(__file__).resolve().parents[1]
RECORDS = json.loads((ROOT / "tests/data/continuation-records.json").read_text())["records"]


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


def test_missing_and_unreconciled_cells_draw_nothing_and_say_why():
    series = build("000858")
    quarterly = series["quarterly"]
    target = quarterly["periods"][-2]
    row = next(r for r in quarterly["rows"] if r["id"] == "net_profit")
    reconciled = copy.deepcopy(series)
    cell = next(r for r in reconciled["quarterly"]["rows"] if r["id"] == "net_profit")["values"][target]
    cell.update(display=None, value=None, unreconciled={target: "2026-01-01"})
    before = figure(charts.financial_charts({"series": series}, []), "单季归母净利润")["chart"]
    after = figure(charts.financial_charts({"series": reconciled}, []), "单季归母净利润")["chart"]
    index = quarterly["periods"].index(target)
    assert not before["groups"][index]["bars"][0]["missing"]
    assert before["groups"][index]["bars"][0]["text"] == row["values"][target]["display"]
    assert after["groups"][index]["bars"][0]["missing"] and after["groups"][index]["bars"][0]["h"] == 0
    assert after["groups"][index]["tip"].endswith("更正版本待核对")


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
