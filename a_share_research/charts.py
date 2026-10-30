"""Chart geometry for server-rendered SVG; pure functions over saved report and batch data.

Charts redraw values already saved in a report, batch or price reference, and labels reuse the
saved display strings; besides geometry (scales and ticks), the one value computed here is each
selected company's own batch return, through the same screening.group_return as the portfolios.
Every chart also returns its readouts as `data`, the HTML equivalent listed under it. The page
CSP forbids inline styles, so templates draw with SVG attributes: x in percent of the figure
width (the chart follows the column on any screen) and y in pixels (text keeps its size).
"""
import math
from datetime import date
from decimal import Decimal, DecimalException

from a_share_research import screening

# Column charts: label band above the plot, plot, then period labels.
TOP, PLOT, BOTTOM = 22, 92, 26
HEIGHT = TOP + PLOT + BOTTOM
# Left gutter (percent) for tick labels.
GUTTER = 11.0
QUARTER = {"03-31": 1, "06-30": 2, "09-30": 3, "12-31": 4}
CUMULATIVE = {"03-31": "一季度", "06-30": "上半年", "09-30": "前三季度", "12-31": "全年"}
VERDICTS = ("暂不值得", "需要谨慎", "值得继续研究")


def number(value) -> float | None:
    """A saved display string as a float for geometry; anything else is missing."""
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except DecimalException:
        return None
    return float(parsed) if parsed.is_finite() else None


def nice_ticks(lo: float, hi: float, count: int = 3) -> list[float]:
    """Round tick values spanning [lo, hi] and always including zero."""
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi == lo:
        hi = lo + 1.0
    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start, stop = math.floor(lo / step + 1e-9), math.ceil(hi / step - 1e-9)
    return [round(i * step, 10) for i in range(start, stop + 1)]


def tick_text(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 10 or value == 0 else f"{value:g}"


def _scale(values: list[float]):
    ticks = nice_ticks(min(values, default=0.0), max(values, default=0.0))
    lo, hi = ticks[0], ticks[-1]
    return ticks, lambda v: round(TOP + (hi - v) / (hi - lo) * PLOT, 2)


def columns(groups: list[dict], series: list[str]) -> dict | None:
    """Grouped columns: each group has a label and one point per series (value text or None).

    A point is {"text": saved display string or None, "tip": readout text, "note": optional}.
    Missing points draw nothing and say so in the readout. The last group is emphasised.
    """
    values = [number(p["text"]) for g in groups for p in g["points"]]
    if not any(v is not None for v in values):
        return None
    ticks, y = _scale([v for v in values if v is not None])
    zero = y(0.0)
    slot = (100.0 - GUTTER) / len(groups)
    width = slot * (0.42 if len(series) == 1 else 0.3)
    gap = slot * 0.04
    out = []
    for index, group in enumerate(groups):
        center = GUTTER + slot * (index + 0.5)
        span = width * len(series) + gap * (len(series) - 1)
        bars = []
        for k, point in enumerate(group["points"]):
            value = number(point["text"])
            top = y(value) if value is not None else zero
            bars.append({
                "series": k, "x": round(center - span / 2 + k * (width + gap), 3), "w": round(width, 3),
                "y": min(top, zero), "h": max(round(abs(zero - top), 2), 1.0 if value is not None else 0),
                "negative": value is not None and value < 0, "missing": value is None,
                "text": point["text"], "label_y": round(top - 6 if value is None or value >= 0 else top + 13, 2),
            })
        out.append({"label": group["label"], "x": round(center, 3), "slot_x": round(center - slot / 2, 3),
                    "slot_w": round(slot, 3), "bars": bars, "tip": group["tip"],
                    "outline": group.get("outline", False), "last": index == len(groups) - 1})
    return {"groups": out, "series": series, "zero": zero, "height": HEIGHT, "gutter": GUTTER,
            "data": [g["tip"] for g in groups],
            "ticks": [{"y": y(t), "text": tick_text(t)} for t in ticks], "label_y": TOP + PLOT + 17}


def lines(periods: list[str], labels: list[str], series: list[dict]) -> dict | None:
    """Point-in-time series on one axis; each series is {"label", "texts"} aligned to periods."""
    values = [number(t) for s in series for t in s["texts"]]
    if sum(v is not None for v in values) < 2:
        return None
    ticks, y = _scale([v for v in values if v is not None])
    # Period ends are unevenly spaced (yearly, then quarterly), so x follows the calendar.
    days = [date.fromisoformat(p).toordinal() for p in periods]
    first, span = days[0], (days[-1] - days[0]) or 1
    inner = 100.0 - GUTTER - 6.0
    xs = [round(GUTTER + 3.0 + (d - first) / span * inner, 3) for d in days]
    bounds = [GUTTER] + [round((a + b) / 2, 3) for a, b in zip(xs, xs[1:])] + [100.0]
    out = []
    for k, s in enumerate(series):
        points = [(xs[i], y(number(t))) if number(t) is not None else None for i, t in enumerate(s["texts"])]
        segments = [{"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]} for a, b in zip(points, points[1:]) if a and b]
        # The legend reads every series at the same, latest period; an older value is not carried forward.
        out.append({"series": k, "label": s["label"], "segments": segments,
                    "dots": [{"x": p[0], "y": p[1]} for p in points if p], "latest": s["texts"][-1]})
    # Label the latest period, then walk back keeping labels at least 14% apart.
    axis = []
    for i in range(len(periods) - 1, -1, -1):
        if not axis or axis[-1]["x"] - xs[i] >= 14:
            axis.append({"x": xs[i], "text": labels[i], "anchor": "end" if xs[i] > 92 else "middle"})
    tips = [{"x": bounds[i], "w": round(bounds[i + 1] - bounds[i], 3),
             "tip": f"{periods[i]}：" + "；".join(f"{s['label']} {s['texts'][i] or '—'}" for s in series)}
            for i in range(len(periods))]
    return {"series": out, "ticks": [{"y": y(t), "text": tick_text(t)} for t in ticks], "axis": axis,
            "tips": tips, "data": [t["tip"] for t in tips], "latest_period": periods[-1], "height": HEIGHT, "gutter": GUTTER, "label_y": TOP + PLOT + 17}


def bars(rows: list[dict], start: float = 34.0, end: float = 84.0) -> dict | None:
    """Horizontal bars from a shared zero; rows are {"label", "value": float|None, "text", "tip"}."""
    values = [r["value"] for r in rows if r["value"] is not None]
    if not values:
        return None
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = (hi - lo) or 1.0
    at = lambda v: start + (v - lo) / span * (end - start)  # noqa: E731
    zero = at(0.0)
    out = []
    for i, r in enumerate(rows):
        x = at(r["value"]) if r["value"] is not None else zero
        out.append({**r, "y": i * 32, "x": round(min(x, zero), 3), "w": round(abs(x - zero), 3),
                    "missing": r["value"] is None})
    return {"rows": out, "zero": round(zero, 3), "height": len(rows) * 32, "start": start,
            "data": [r["tip"] for r in rows]}


def _period_label(period: str, kind: str) -> str:
    if kind == "annual":
        return period[:4]
    if kind == "quarterly":
        return f"{period[2:4]}Q{QUARTER.get(period[5:], '?')}"
    return period[2:7].replace("-", ".")


def _cell_tip(label: str, period_text: str, cell: dict | None, unit: str) -> str:
    cell = cell or {}
    if cell.get("display") is None:
        return f"{period_text} {label}：{'更正版本待核对' if cell.get('unreconciled') else '缺失'}"
    yoy = (cell.get("yoy") or {}).get("value")
    return f"{period_text} {label}：{cell['display']} {unit}" + (f"，同比 {yoy}%" if yoy else "")


def _headline(label: str, period_text: str, text: str | None, yoy: str | None, unit: str) -> str:
    if text is None:
        return f"{period_text} {label}：缺失"
    return f"{period_text} {text} {unit}" + (f" · 同比 {yoy}%" if yoy else "")


def financial_charts(body: dict, fin: list[dict]) -> list[dict]:
    """Figures for the report's financial section, from the saved operating series when present,
    otherwise from the saved financial table (annual and latest cumulative values only)."""
    series = body.get("series") or {}
    table = {r["id"]: r for r in fin}
    figures = []

    def row(kind, key):
        return next((r for r in (series.get(kind) or {}).get("rows", []) if r.get("id") == key), None)

    def annual_columns(keys, labels, title, note):
        periods = (series.get("annual") or {}).get("periods") or []
        ttm = (series.get("ttm") or {}).get("periods") or []
        rows = [row("annual", k) for k in keys]
        if periods and all(rows):
            groups = []
            for p in periods:
                cells = [r["values"].get(p) or {} for r in rows]
                groups.append({"label": p[:4], "points": [{"text": c.get("display")} for c in cells],
                               "tip": "；".join(_cell_tip(lb, f"{p[:4]} 年", c, "亿元") for lb, c in zip(labels, cells))})
            ttm_rows = [row("ttm", k) for k in keys]
            if ttm and all(ttm_rows) and ttm[-1] > periods[-1]:
                cells = [r["values"].get(ttm[-1]) or {} for r in ttm_rows]
                groups.append({"label": "近12月", "outline": True, "points": [{"text": c.get("display")} for c in cells],
                               "tip": "；".join(_cell_tip(lb, f"截至 {ttm[-1]} 近 12 个月", c, "亿元")
                                               for lb, c in zip(labels, cells))})
            if len(rows) == 1:
                last = rows[0]["values"].get(periods[-1]) or {}
                headline = _headline(labels[0], f"{periods[-1][:4]} 年", last.get("display"),
                                     (last.get("yoy") or {}).get("value"), "亿元")
            else:
                headline = f"{periods[-1][:4]} 年 " + "；".join(
                    f"{lb} {(r['values'].get(periods[-1]) or {}).get('display') or '缺失'}" for lb, r in zip(labels, rows)) + " 亿元"
            if groups[-1].get("outline"):
                note += f"末根为截至 {ttm[-1]} 的近 12 个月（TTM），用空心柱区分。"
            source = "经营序列"
        else:
            trows = [table.get(f"T.{k}") for k in keys]
            if not all(trows):
                return None
            annual = sorted({p for r in trows for p in r["values"] if p.endswith("-12-31")})
            if len(annual) < 2:
                return None
            unit = trows[0]["unit"]
            yoy_row = table.get({"revenue": "T.revenue_yoy", "net_profit": "T.profit_yoy"}.get(keys[0], ""))
            groups = [{"label": p[:4], "points": [{"text": r["values"].get(p)} for r in trows],
                       "tip": "；".join(f"{p[:4]} 年 {lb}：{r['values'].get(p) or '缺失'} {unit}"
                                       for lb, r in zip(labels, trows))} for p in annual]
            if len(trows) == 1:
                yoy = yoy_row["values"].get(annual[-1]) if yoy_row else None
                headline = _headline(labels[0], f"{annual[-1][:4]} 年", trows[0]["values"].get(annual[-1]), yoy, unit)
            else:
                headline = f"{annual[-1][:4]} 年 " + "；".join(
                    f"{lb} {r['values'].get(annual[-1]) or '缺失'}" for lb, r in zip(labels, trows)) + f" {unit}"
            source = "财务数据表"
        chart = columns(groups, labels)
        return chart and {"kind": "columns", "title": title, "headline": headline, "chart": chart,
                          "note": note, "source": source}

    def latest_growth():
        latest = max((p for r in fin for p in r["values"]), default=None)
        rows = [(key, label) for key, label in (("T.revenue_yoy", "营业收入"), ("T.profit_yoy", "归母净利润"),
                                                 ("T.core_profit_yoy", "扣非净利润")) if key in table]
        if not latest or not rows:
            return None
        items = [{"label": label, "value": number(table[key]["values"].get(latest)),
                  "text": (table[key]["values"].get(latest) or "—") + ("%" if table[key]["values"].get(latest) else ""),
                  "tip": f"{latest} {label}同比：{table[key]['values'].get(latest) or '缺失'}%"} for key, label in rows]
        chart = bars(items)
        cumulative = CUMULATIVE.get(latest[5:], "")
        return chart and {"kind": "bars", "title": f"最新一期同比 · {latest[:4]} 年{cumulative}",
                          "headline": f"{latest} 累计，较上年同期", "chart": chart, "source": "财务数据表",
                          "note": "年初至报告期末累计口径，来自财务数据表的同比指标；不是单季增速。"}

    def quarterly(key, label):
        group = series.get("quarterly") or {}
        r = row("quarterly", key)
        if not r or not group.get("periods"):
            return None
        groups = []
        for p in group["periods"]:
            cell = r["values"].get(p) or {}
            groups.append({"label": _period_label(p, "quarterly"), "points": [{"text": cell.get("display")}],
                           "tip": _cell_tip(label, _period_label(p, "quarterly"), cell, "亿元")})
        chart = columns(groups, [label])
        last = r["values"].get(group["periods"][-1]) or {}
        return chart and {"kind": "columns", "title": f"单季{label}", "chart": chart, "source": "经营序列",
                          "headline": _headline(label, _period_label(group["periods"][-1], "quarterly"),
                                                last.get("display"), (last.get("yoy") or {}).get("value"), "亿元"),
                          "note": "单季由同年相邻累计期相减，一季度直接取值；同比为与上年同季比较。"}

    def balance():
        group = series.get("balance") or {}
        cash, debt = row("balance", "cash"), row("balance", "interest_debt")
        if cash and debt and group.get("periods"):
            periods = group["periods"]
            texts = [[r["values"].get(p, {}).get("display") for p in periods] for r in (cash, debt)]
            source = "经营序列"
        else:
            cash, debt = table.get("T.cash"), table.get("T.interest_debt")
            if not cash or not debt:
                return None
            periods = sorted(set(cash["values"]) | set(debt["values"]))
            texts = [[r["values"].get(p) for p in periods] for r in (cash, debt)]
            source = "财务数据表"
        chart = lines(periods, [_period_label(p, "balance") for p in periods],
                      [{"label": "货币资金", "texts": texts[0]}, {"label": "四项债务合计", "texts": texts[1]}])
        return chart and {"kind": "lines", "title": "货币资金与四项债务合计", "chart": chart, "source": source,
                          "headline": f"期末时点值，亿元；图例为 {periods[-1]} 的值",
                          "note": "四项债务合计只含短期借款、一年内到期的非流动负债、长期借款与应付债券，不代表全部有息负债；"
                                  "时点值不代表期间流量，某期缺失时图例留空，不沿用更早的值。"}

    for figure in (
        latest_growth(),
        annual_columns(["revenue"], ["营业总收入"], "营业总收入 · 年度", ""),
        annual_columns(["net_profit", "op_cash"], ["归母净利润", "经营活动现金流净额"], "利润与经营现金流 · 年度",
                       "两者口径不同：经营现金流为合并口径，归母净利润只含母公司股东部分。"),
        quarterly("net_profit", "归母净利润"),
        annual_columns(["fcf"], ["经营现金流减购建长期资产现金"], "经营现金流减购建长期资产现金 · 年度",
                       "不等于可分配现金。"),
        balance(),
    ):
        if figure:
            figures.append(figure)
    return figures


def price_ladder(levels: dict) -> dict:
    """Positions of the saved price levels on one axis, with labels assigned to non-colliding lanes."""
    # Close first: it takes the lane next to the axis and the others move around it.
    marks = [("close", "收盘", levels["close"], "above"), ("stop", "失效线", levels["stop"], "above"),
             ("trigger", "观察线", levels["trigger"], "above"),
             ("ma60", "MA60", levels["ma60"], "below"), ("ma20", "MA20", levels["ma20"], "below")]
    values = [m[2] for m in marks]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    at = lambda v: round(4 + (v - lo) / (hi - lo) * 92, 3)  # noqa: E731
    items = [{"key": k, "label": f"{name} {value:.2f}", "x": at(value), "side": side} for k, name, value, side in marks]
    for i in items:
        i["anchor"] = "start" if i["x"] < 12 else "end" if i["x"] > 88 else "middle"
    assign_lanes(items)
    above = 1 + max((i["lane"] for i in items if i["side"] == "above"), default=0)
    below = 1 + max((i["lane"] for i in items if i["side"] == "below"), default=0)
    axis = 18 * above + 10
    for i in items:
        i["y"] = axis - 12 - 18 * i["lane"] if i["side"] == "above" else axis + 22 + 18 * i["lane"]
    return {"marks": items, "axis": axis, "height": axis + 18 * below + 12,
            "band": {"x": at(levels["stop"]), "w": round(at(levels["trigger"]) - at(levels["stop"]), 3)}}


def text_width(text: str) -> float:
    """Rough pixel width of a 12px label: CJK glyphs are square, Latin and digits about half."""
    return sum(12 if ord(ch) > 0x2E80 else 6.8 for ch in text)


def assign_lanes(items: list[dict], reference: float = 300.0, depth: int = 3) -> None:
    """Place labels in list order (priority first) on their preferred side of the axis, else the
    other side, in the lane nearest the axis where the label overlaps nothing and its leader line
    passes through no label nearer the axis; with no such lane, the nearest lane without overlap."""
    placed: list[tuple[str, int, float, float]] = []

    def fits(side, lane, left, right, center, leader):
        if any(s == side and n == lane and a < right and left < b for s, n, a, b in placed):
            return False
        return not leader or not any(s == side and n < lane and a <= center <= b for s, n, a, b in placed)

    for item in items:
        width = text_width(item["label"]) + 12
        center = item["x"] / 100 * reference
        left = {"start": center - 6, "end": center - width + 6}.get(item.get("anchor"), center - width / 2)
        right = left + width
        other = "below" if item["side"] == "above" else "above"
        choice = next(((side, lane) for side in (item["side"], other) for lane in range(depth)
                       if fits(side, lane, left, right, center, True)), None)
        if choice is None:
            choice = item["side"], next(n for n in range(len(items) + 1)
                                        if fits(item["side"], n, left, right, center, False))
        item["side"], item["lane"] = choice
        placed.append((choice[0], choice[1], left, right))


def verdict_track(reports: list) -> dict | None:
    """Reviewed verdicts across a company's reports, oldest first, on the three-step scale."""
    points = [r for r in reversed(reports) if r["passed"] and r["verdict"] in VERDICTS and not r["replay_of"]]
    if len(points) < 2:
        return None
    # Row labels take the left 30%; points spread over the rest.
    slot = 68.0 / len(points)
    out = []
    for i, r in enumerate(points):
        out.append({"id": r["id"], "x": round(30 + slot * (i + 0.5), 3), "y": 16 + 30 * (2 - VERDICTS.index(r["verdict"])),
                    "verdict": r["verdict"], "date": r["created_at"][5:10].replace("-", "/"),
                    "tip": f"{r['created_at'][:16].replace('T', ' ')} · {r['verdict']} · 资料至 {r['period']}"
                           + (f" · 问题：{_brief(r['question'])}" if r["question"] else " · 未记录具体问题")})
    segments = [{"x1": a["x"], "y1": a["y"], "x2": b["x"], "y2": b["y"]} for a, b in zip(out, out[1:])]
    stride = math.ceil(len(out) / 6)
    days = [p["date"] for p in out]
    for i, (p, r) in enumerate(zip(out, points)):
        p["show_date"] = (len(out) - 1 - i) % stride == 0
        # Two reports on one day are told apart by time when there is room for it.
        if days.count(p["date"]) > 1 and len(out) <= 4:
            p["date"] += " " + r["created_at"][11:16]
    return {"points": out, "segments": segments, "rows": [{"label": v, "y": 16 + 30 * (2 - n)} for n, v in enumerate(VERDICTS)],
            "height": 16 + 60 + 34}


def _brief(text: str, limit: int = 40) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


def batch_returns(batch: dict, results: dict | None, names: dict) -> dict | None:
    """Cumulative portfolio returns and each selected company's own net return since entry.

    A company's return is computed here, on request, with screening.group_return over the saved
    entry and latest mark as a one-seat group, so it carries the same cost as the portfolios."""
    if not results:
        return None
    labels = [("L5", "选定组合"), ("Q5", "机械前五"), ("Q20", "全部候选")] + list(batch["rule"]["benchmarks"].items())
    rows = []
    for key, label in labels:
        value = results["values"].get(key)
        rows.append({"label": label, "value": value * 100 if value is not None else None, "emphasis": key == "L5",
                     "text": f"{value * 100:+.2f}%" if value is not None else "—",
                     "tip": f"{label}：{f'{value * 100:+.2f}%' if value is not None else '未计算'}"})
    mark = batch["marks"]["latest"]
    members = []
    for code in batch["l5"]["codes"]:
        value = screening.group_return(batch["entry"], mark, [code], 1, batch["rule"]["cost"])
        filled = batch["entry"]["prices"].get(code, {}).get("filled")
        members.append({"label": names.get(code, code), "value": value * 100 if filled else None,
                        "text": f"{value * 100:+.2f}%" if filled else "未成交",
                        "tip": f"{names.get(code, code)}：{f'{value * 100:+.2f}%' if filled else '未成交，按现金计'}"})
    return {"groups": bars(rows), "members": bars(members), "entry": batch["entry"]["date"], "date": results["date"]}


def funnel(batch: dict, labels: list[tuple[str, str]]) -> dict | None:
    counts = batch["universe"]["funnel"]
    rows = [{"label": label, "value": float(counts[key]), "text": f"{counts[key]:,}",
             "tip": f"{label}：{counts[key]:,} 家"} for key, label in labels if isinstance(counts.get(key), int)]
    return bars(rows, start=40.0, end=86.0)
