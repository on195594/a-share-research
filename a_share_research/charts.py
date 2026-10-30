"""Chart geometry for server-rendered SVG; pure functions over saved report and batch data.

Charts redraw values already saved in a report, batch or price reference: report charts reuse saved
display strings, while batch charts format saved candidate numbers (deducted profit in 亿元, 60-day
return in percent, and each selected company's return via the same screening.group_return as the
portfolios). Every chart also returns its readouts as `data`, the HTML equivalent listed under it.
The page CSP forbids inline styles, so templates draw with SVG attributes: x in percent of the
figure width (the chart follows the column on any screen) and y in pixels (text keeps its size).
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


def _plot_value(value: float) -> float | None:
    # Leave exponent headroom for tick rounding, differences and coordinate scaling.
    return value if math.isfinite(value) and (value == 0 or 1e-300 <= abs(value) <= 1e300) else None


def number(value) -> float | None:
    """A saved display string as a float for geometry; anything else is missing."""
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except DecimalException:
        return None
    if not parsed.is_finite():
        return None
    value = float(parsed)
    if value == 0 and parsed != 0:  # A nonzero decimal must not silently underflow to zero.
        return None
    return _plot_value(value)


def nice_ticks(lo: float, hi: float, count: int = 3) -> list[float]:
    """Round tick values spanning [lo, hi] and always including zero."""
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi == lo:
        hi = lo + 1.0
    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start, stop = math.floor(lo / step + 1e-9), math.ceil(hi / step - 1e-9)
    return [float(f"{i * step:.12g}") for i in range(start, stop + 1)]


def tick_text(value: float) -> str:
    if abs(value) >= 100_000:
        return f"{value:.2e}"
    return f"{value:,.0f}" if abs(value) >= 10 or value == 0 else f"{value:g}"


def _scale(values: list[float]):
    ticks = nice_ticks(min(values, default=0.0), max(values, default=0.0))
    lo, hi = ticks[0], ticks[-1]
    return ticks, lambda v: round(TOP + (hi - v) / (hi - lo) * PLOT, 2)


def _gutter(ticks: list[float]) -> float:
    # Reserve label space at a 300px plot width, including narrow desktop columns.
    return max(GUTTER, max(text_width(tick_text(t)) for t in ticks) / 3 + 2)


def columns(groups: list[dict], series: list[str]) -> dict | None:
    """Grouped columns: each group has a label and one point per series (value text or None).

    A point is {"text": saved display string or None, "tip": readout text, "note": optional}.
    Missing points draw nothing and say so in the readout. The last group is emphasised.
    """
    values = [number(p["text"]) for g in groups for p in g["points"]]
    if not any(v is not None for v in values):
        return None
    ticks, y = _scale([v for v in values if v is not None])
    gutter = _gutter(ticks)
    zero = y(0.0)
    slot = (100.0 - gutter) / len(groups)
    width = slot * (0.42 if len(series) == 1 else 0.3)
    gap = slot * 0.04
    out = []
    for index, group in enumerate(groups):
        center = gutter + slot * (index + 0.5)
        span = width * len(series) + gap * (len(series) - 1)
        bars = []
        for k, point in enumerate(group["points"]):
            value = number(point["text"])
            top = y(value) if value is not None else zero
            bars.append({
                "series": k, "x": round(center - span / 2 + k * (width + gap), 3), "w": round(width, 3),
                "y": min(top, zero), "h": round(abs(zero - top), 2),
                "negative": value is not None and value < 0, "missing": value is None,
                "text": point["text"], "label_y": round(top - 6 if value is None or value >= 0 else min(top + 13, TOP + PLOT - 4), 2),
            })
        out.append({"label": group["label"], "x": round(center, 3), "slot_x": round(center - slot / 2, 3),
                    "slot_w": round(slot, 3), "bars": bars, "tip": group["tip"],
                    "label_edge": center + text_width(group["label"]) / 6 > 99,
                    "value_edge": center + text_width(str(bars[0]["text"] or "")) / 6 > 99,
                    "outline": group.get("outline", False), "last": index == len(groups) - 1})
    return {"groups": out, "series": series, "zero": zero, "height": HEIGHT, "gutter": gutter,
            "data": [g["tip"] for g in groups],
            "ticks": [{"y": y(t), "text": tick_text(t)} for t in ticks], "label_y": TOP + PLOT + 17}


def lines(periods: list[str], labels: list[str], series: list[dict]) -> dict | None:
    """Point-in-time series on one axis; each series is {"label", "texts"} aligned to periods."""
    values = [number(t) for s in series for t in s["texts"]]
    if sum(v is not None for v in values) < 2:
        return None
    ticks, y = _scale([v for v in values if v is not None])
    gutter = _gutter(ticks)
    # Period ends are unevenly spaced (yearly, then quarterly), so x follows the calendar.
    days = [date.fromisoformat(p).toordinal() for p in periods]
    first, span = days[0], (days[-1] - days[0]) or 1
    inner = 100.0 - gutter - 6.0
    xs = [round(gutter + 3.0 + (d - first) / span * inner, 3) for d in days]
    bounds = [gutter] + [round((a + b) / 2, 3) for a, b in zip(xs, xs[1:])] + [100.0]
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
            "tips": tips, "data": [t["tip"] for t in tips], "latest_period": periods[-1], "height": HEIGHT, "gutter": gutter, "label_y": TOP + PLOT + 17}


def bars(rows: list[dict], start: float = 34.0, end: float = 84.0) -> dict | None:
    """Horizontal bars from a shared zero; rows are {"label", "value": float|None, "text", "tip"}."""
    rows = [r | {"value": _plot_value(r["value"]) if r["value"] is not None else None} for r in rows]
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


def waterfall(items: list[dict], start: float = 47.0, end: float = 82.0) -> dict | None:
    """Horizontal waterfall / bridge chart on one axis (e.g. profit -> operating cash -> capex -> FCF)."""
    items = [r | {k: _plot_value(r[k]) if r.get(k) is not None else None for k in ("from", "to")} for r in items]
    values = [v for r in items for v in (r.get("from"), r.get("to")) if v is not None]
    if len(values) < 2:
        return None
    ticks = nice_ticks(min(values), max(values), 3)
    lo, hi = ticks[0], ticks[-1]
    at = lambda v: round(start + (v - lo) / (hi - lo) * (end - start), 3)  # noqa: E731
    zero = at(0.0)
    out = []
    for i, r in enumerate(items):
        a, b = r.get("from"), r.get("to")
        missing = a is None or b is None
        x0, x1 = (at(a), at(b)) if not missing else (zero, zero)
        out.append({**r, "y": i * 32, "x": round(min(x0, x1), 3),
                    "w": round(abs(x1 - x0), 3) if not missing else 0.0,
                    "x_from": x0, "x_to": x1, "missing": missing})
    connectors = []
    for i, r in enumerate(out):
        if r.get("bridged") and not r["missing"]:
            if i > 0 and not out[i - 1]["missing"]:
                connectors.append({"x": out[i - 1]["x_to"], "y1": out[i - 1]["y"] + 23, "y2": r["y"] + 9})
            if i + 1 < len(out) and not out[i + 1]["missing"]:
                connectors.append({"x": out[i + 1]["x_to"], "y1": r["y"] + 23, "y2": out[i + 1]["y"] + 9})
    height = len(out) * 32
    return {"rows": out, "connectors": connectors, "zero": zero, "height": height + 18,
            "ticks": [{"x": at(t), "text": tick_text(t)} for t in ticks], "axis_y": height + 12,
            "data": [r["tip"] for r in items]}


def paired_bars(rows: list[dict], labels: tuple[str, str], mode: str = "period",
                start: float = 32.0, end: float = 63.0) -> dict | None:
    """Two horizontal bars per row on a shared zero-included scale (prior vs latest, or subject vs peer)."""
    rows = [r | {k: _plot_value(r[k]) if r.get(k) is not None else None for k in ("v0", "v1")} for r in rows]
    values = [v for r in rows for v in (r.get("v0"), r.get("v1")) if v is not None]
    if not values:
        return None
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = (hi - lo) or 1.0
    at = lambda v: round(start + (v - lo) / span * (end - start), 3)  # noqa: E731
    zero = at(0.0)
    out = []
    for i, r in enumerate(rows):
        v0, v1 = r.get("v0"), r.get("v1")
        x0, x1 = (at(v0) if v0 is not None else zero), (at(v1) if v1 is not None else zero)
        out.append({**r, "y": i * 36,
                    "x0": round(min(x0, zero), 3), "w0": round(abs(x0 - zero), 3),
                    "x1": round(min(x1, zero), 3), "w1": round(abs(x1 - zero), 3),
                    "m0": v0 is None, "m1": v1 is None})
    return {"rows": out, "zero": zero, "labels": list(labels), "mode": mode, "height": len(out) * 36,
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

    def ratios():
        chart = dumbbell(fin)
        return chart and {"kind": "dumbbell", "title": "毛利率、ROE 与负债率 · 同期对比", "chart": chart,
                          "headline": f"{chart['latest']} 对比上年同期 {chart['prior']}", "source": "财务数据表",
                          "note": "毛利率与加权 ROE 为年初至报告期末累计口径，ROE 不年化，只与上年同期比较；"
                                  "资产负债率为期末时点值。"}

    def cash_bridge():
        steps = (("net_profit", "归母净利润", "profit"), ("core_profit", "扣非归母净利润", "core"),
                 ("op_cash", "经营活动现金流净额", "cash"), ("capex", "购建长期资产支付的现金", "step"),
                 ("fcf", "经营现金流减购建长期资产现金", "fcf"))
        ttm_p = (series.get("ttm") or {}).get("periods") or []
        ann_p = (series.get("annual") or {}).get("periods") or []
        ttm_ok = bool(ttm_p and row("ttm", "op_cash")
                      and (row("ttm", "op_cash")["values"].get(ttm_p[-1]) or {}).get("display") is not None)
        kind = "ttm" if ttm_ok else ("annual" if ann_p else ("ttm" if ttm_p else None))
        if kind and row(kind, "op_cash") and row(kind, "fcf"):
            period = (ttm_p if kind == "ttm" else ann_p)[-1]
            period_text = f"截至 {period} 近12月" if kind == "ttm" else f"{period[:4]} 年"
            raw = {k: (row(kind, k)["values"].get(period) or {}) if row(kind, k) else {} for k, _, _ in steps}
            texts = {k: c.get("display") for k, c in raw.items()}
            tips = {k: _cell_tip(lb, period_text, raw[k], "亿元") for k, lb, _ in steps}
            source = "经营序列"
        else:
            if "T.op_cash" not in table or "T.fcf" not in table:
                return None
            period = max((p for k, _, _ in steps if f"T.{k}" in table for p in table[f"T.{k}"]["values"]), default=None)
            if not period:
                return None
            period_text = f"{period[:4]} 年{CUMULATIVE.get(period[5:], '')}"
            texts = {k: table[f"T.{k}"]["values"].get(period) if f"T.{k}" in table else None for k, _, _ in steps}
            tips = {k: f"{period} {lb}：{texts[k] + ' 亿元' if texts[k] is not None else '缺失'}" for k, lb, _ in steps}
            source = "财务数据表"
        v_ocf, v_fcf = number(texts.get("op_cash")), number(texts.get("fcf"))
        items = []
        for k, lb, role in steps:
            val = number(texts.get(k))
            bridged = bool(role == "step" and val is not None and v_ocf is not None and v_fcf is not None)
            if role == "step":
                a, b = (v_fcf, v_ocf) if bridged else (None, None)
            else:
                a, b = (0.0, val) if val is not None else (None, None)
            items.append({"label": lb, "from": a, "to": b, "role": role, "bridged": bridged,
                          "text": texts.get(k) or "—", "tip": tips[k]})
        chart = waterfall(items)
        return chart and {"kind": "waterfall", "title": f"盈利与现金流转化 · {period_text}",
                          "headline": f"归母净利润 {texts.get('net_profit') or '—'} → 经营活动现金流净额 {texts.get('op_cash') or '—'} → 经营现金流减购建长期资产现金 {texts.get('fcf') or '—'} 亿元",
                          "chart": chart, "source": source,
                          "note": "展示同期间利润、经营活动现金流净额、购建长期资产支付的现金与经营现金流减购建长期资产现金的转化关系；"
                                  "经营现金流为合并口径，归母与扣非净利为归属母公司股东口径。"}

    def balance_structure():
        group = series.get("balance") or {}
        periods = group.get("periods") or []
        if periods and any(row("balance", k) for k, _ in STOCKS):
            latest = periods[-1]
            prior = f"{int(latest[:4]) - 1}{latest[4:]}"
            if prior not in periods:
                return None
            pairs = [(lb, (row("balance", k)["values"].get(prior) or {}).get("display"),
                      (row("balance", k)["values"].get(latest) or {}).get("display"))
                     for k, lb in STOCKS if row("balance", k)]
            source = "经营序列"
        else:
            present = [table[f"T.{k}"] for k, _ in STOCKS if f"T.{k}" in table]
            all_p = sorted({p for r in present for p in r["values"]})
            if not all_p:
                return None
            latest = all_p[-1]
            prior = f"{int(latest[:4]) - 1}{latest[4:]}"
            if prior not in all_p:
                return None
            pairs = [(lb, table[f"T.{k}"]["values"].get(prior), table[f"T.{k}"]["values"].get(latest))
                     for k, lb in STOCKS if f"T.{k}" in table]
            source = "财务数据表"
        items = [{"label": lb, "v0": number(b), "v1": number(a), "t0": b, "t1": a,
                  "tip": f"{lb}：{prior} {b + ' 亿元' if number(b) is not None else '缺失'} → "
                         f"{latest} {a + ' 亿元' if number(a) is not None else '缺失'}"}
                 for lb, b, a in pairs if number(b) is not None or number(a) is not None]
        if len(items) < 2:
            return None
        chart = paired_bars(items, (_period_label(prior, "balance"), _period_label(latest, "balance")), mode="period")
        return chart and {"kind": "paired", "title": "资金与营运结构 · 同期对比",
                          "headline": f"{latest} 对比 {prior}（同一亿元刻度，可跨行比较规模）",
                          "chart": chart, "source": source,
                          "note": "同一水平刻度对比货币资金、四项债务合计及应收、存货、合同负债、商誉的绝对规模与同期变化。"}

    for figure in (
        cash_bridge(),
        ratios(),
        latest_growth(),
        balance_structure(),
        annual_columns(["revenue"], ["营业总收入"], "营业总收入 · 年度", ""),
        annual_columns(["net_profit", "op_cash"], ["归母净利润", "经营活动现金流净额"], "利润与经营现金流 · 年度",
                       "两者口径不同：经营现金流为合并口径，归母净利润只含母公司股东部分。"),
        annual_columns(["fcf"], ["经营现金流减购建长期资产现金"], "经营现金流减购建长期资产现金 · 年度",
                       "不等于可分配现金。"),
        balance(),
    ):
        if figure:
            figures.append(figure)
    return figures


# Sparklines in the vitals panel: a short row height, each scaled to its own values.
SPARK = 30
FLOWS = (("revenue", "营业总收入"), ("net_profit", "归母净利润"), ("core_profit", "扣非归母净利润"),
         ("op_cash", "经营活动现金流净额"), ("fcf", "经营现金流减购建长期资产现金"))
STOCKS = (("cash", "货币资金"), ("interest_debt", "四项债务合计"), ("receivables", "应收票据及应收账款"),
          ("inventory", "存货"), ("contract_liab", "合同负债"), ("goodwill", "商誉"))


def spark_columns(values: list[float | None]) -> dict | None:
    """Evenly spaced mini columns from a shared zero; the last one is the latest period."""
    values = [_plot_value(v) if v is not None else None for v in values]
    present = [v for v in values if v is not None]
    if not present:
        return None
    lo, hi = min(present + [0.0]), max(present + [0.0])
    span = (hi - lo) or 1.0
    y = lambda v: round(2 + (hi - v) / span * (SPARK - 4), 2)  # noqa: E731
    zero, slot = y(0.0), 100.0 / len(values)
    width = slot * 0.6
    out = []
    for i, v in enumerate(values):
        cell = {"slot_x": round(slot * i, 3), "slot_w": round(slot, 3), "missing": v is None,
                "x": round(slot * i + (slot - width) / 2, 3), "w": round(width, 3), "last": i == len(values) - 1}
        if v is not None:
            h = round(abs(zero - y(v)), 2)
            cell.update(y=zero if v < 0 else round(zero - h, 2), h=h)
        out.append(cell)
    return {"bars": out, "zero": zero, "height": SPARK, "negative": lo < 0}


def spark_line(periods: list[str], values: list[float | None]) -> dict | None:
    """A min–max scaled line over calendar time; gaps break the line instead of bridging it."""
    values = [_plot_value(v) if v is not None else None for v in values]
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return None
    lo, hi = min(present), max(present)
    span = (hi - lo) or 1.0
    days = [date.fromisoformat(p).toordinal() for p in periods]
    first, width = days[0], (days[-1] - days[0]) or 1
    xs = [round(4 + (d - first) / width * 92, 3) for d in days]
    points = [(xs[i], round(4 + (hi - v) / span * (SPARK - 8), 2)) if v is not None else None
              for i, v in enumerate(values)]
    bounds = [0.0] + [round((a + b) / 2, 3) for a, b in zip(xs, xs[1:])] + [100.0]
    return {"segments": [{"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]} for a, b in zip(points, points[1:]) if a and b],
            # Every value with no neighbour on either side has no segment, so it is drawn as a dot.
            "dots": [{"x": p[0], "y": p[1], "last": i == len(points) - 1,
                      "isolated": not (i and points[i - 1]) and not (i + 1 < len(points) and points[i + 1])}
                     for i, p in enumerate(points) if p],
            "slots": [{"x": bounds[i], "w": round(bounds[i + 1] - bounds[i], 3)} for i in range(len(periods))],
            "height": SPARK}


def vitals(body: dict, fin: list[dict] | None = None) -> dict | None:
    """Small multiples over the saved operating series: single-quarter flows as mini columns and
    period-end balances as lines, each with its latest saved value. When a report has no operating
    series and `fin` is supplied, falls back to the saved T.* financial table."""
    series = body.get("series") or {}
    if not series and fin:
        return _table_vitals(fin)

    def rows(kind, keys):
        group = series.get(kind) or {}
        periods = group.get("periods") or []
        if not periods:  # rows without periods have nothing to read; the group is left out
            return [], [], []
        indexed = {r.get("id"): r for r in group.get("rows", [])}
        return periods, periods[-8:], [(indexed[k], label) for k, label in keys if k in indexed]

    _, quarters, flows = rows("quarterly", FLOWS)
    flow_rows = []
    for r, label in flows:
        cells = [r["values"].get(p) or {} for p in quarters]
        tips = [_cell_tip(label, _period_label(p, "quarterly"), c, "亿元") for p, c in zip(quarters, cells)]
        spark = spark_columns([number(c.get("display")) for c in cells])
        for bar, tip in zip(spark["bars"] if spark else [], tips):
            bar["tip"] = tip
        last = cells[-1]
        flow_rows.append({"label": label, "spark": spark, "latest": last.get("display"),
                          "unreconciled": bool(last.get("unreconciled")), "present": spark is not None,
                          "yoy": (last.get("yoy") or {}).get("value"), "data": tips})

    every, balance, stocks = rows("balance", STOCKS)
    # The same period end a year earlier, when the series saved it, is shown beside the latest.
    year_ago = balance and f"{int(balance[-1][:4]) - 1}{balance[-1][4:]}"
    year_ago = year_ago if year_ago in every else None
    stock_rows = []
    for r, label in stocks:
        cells = [r["values"].get(p) or {} for p in balance]
        values = [number(c.get("display")) for c in cells]
        # A row with too few values keeps its place, so a gap reads as a gap rather than vanishing.
        spark = spark_line(balance, values)
        tips = [_cell_tip(label, p, c, "亿元") for p, c in zip(balance, cells)]
        for slot, tip in zip(spark["slots"] if spark else [], tips):
            slot["tip"] = tip
        last, before = cells[-1], (r["values"].get(year_ago) or {}) if year_ago else {}
        stock_rows.append({"label": label, "spark": spark, "latest": last.get("display"),
                           "unreconciled": bool(last.get("unreconciled")),
                           "present": any(v is not None for v in values),
                           "year_ago": before.get("display"), "data": tips})

    # The panel shows whenever some value can be read; a trend needs two, a reading only one.
    if not any(r["present"] for r in flow_rows + stock_rows):
        return None
    return {"flows": flow_rows, "stocks": stock_rows, "flow_heading": "单季", "source": "经营序列",
            "span_title": f"近 {max(len(quarters), len(balance))} 期",
            "flow_period": quarters and _period_label(quarters[-1], "quarterly"),
            "flow_span": quarters and f"{_period_label(quarters[0], 'quarterly')}–{_period_label(quarters[-1], 'quarterly')}",
            "stock_period": balance and _period_label(balance[-1], "balance"),
            "year_ago": year_ago and _period_label(year_ago, "balance"),
            "stock_span": balance and f"{_period_label(balance[0], 'balance')}–{_period_label(balance[-1], 'balance')}",
            "data": [d for r in flow_rows + stock_rows for d in r["data"]]}


def _table_vitals(fin: list[dict]) -> dict | None:
    """Fallback vitals panel built from saved T.* rows when a report has no operating series."""
    table = {r["id"]: r for r in fin if isinstance(r, dict) and isinstance(r.get("id"), str)}
    all_periods = sorted({p for r in table.values() for p in (r.get("values") or {})})
    if not all_periods:
        return None
    annual = [p for p in all_periods if p.endswith("-12-31")][-8:]
    latest = all_periods[-1]
    yoy_keys = {"revenue": "T.revenue_yoy", "net_profit": "T.profit_yoy", "core_profit": "T.core_profit_yoy"}
    flow_rows = []
    if annual:
        for key, label in FLOWS:
            row = table.get(f"T.{key}")
            if not row:
                continue
            vals = row.get("values") or {}
            unavail = row.get("unavailable") or {}
            tips = [f"{p[:4]} 年 {label}：{vals[p] + ' 亿元' if vals.get(p) is not None else ('更正版本待核对' if p in unavail else '缺失')}"
                    for p in annual]
            spark = spark_columns([number(vals.get(p)) for p in annual])
            for bar, tip in zip(spark["bars"] if spark else [], tips):
                bar["tip"] = tip
            yoy_row = table.get(yoy_keys.get(key, ""))
            yoy = (yoy_row.get("values") or {}).get(latest) if yoy_row else None
            flow_rows.append({"label": label, "spark": spark, "latest": vals.get(latest),
                              "unreconciled": latest in unavail, "present": spark is not None or vals.get(latest) is not None,
                              "yoy": yoy, "data": tips})
    stock_periods = all_periods[-8:]
    year_ago = f"{int(latest[:4]) - 1}{latest[4:]}"
    year_ago = year_ago if year_ago in all_periods else None
    stock_rows = []
    for key, label in STOCKS:
        row = table.get(f"T.{key}")
        if not row:
            continue
        vals = row.get("values") or {}
        unavail = row.get("unavailable") or {}
        values = [number(vals.get(p)) for p in stock_periods]
        spark = spark_line(stock_periods, values)
        tips = [f"{p} {label}：{vals[p] + ' 亿元' if vals.get(p) is not None else ('更正版本待核对' if p in unavail else '缺失')}"
                for p in stock_periods]
        for slot, tip in zip(spark["slots"] if spark else [], tips):
            slot["tip"] = tip
        stock_rows.append({"label": label, "spark": spark, "latest": vals.get(latest),
                           "unreconciled": latest in unavail, "present": any(v is not None for v in values),
                           "year_ago": vals.get(year_ago) if year_ago else None, "data": tips})
    if not any(r["present"] for r in flow_rows + stock_rows):
        return None
    flow_period = f"{latest[2:4]}年{CUMULATIVE.get(latest[5:], '')}"
    span_title = f"历年 {len(annual)} 期与最新期" if annual else f"近 {len(stock_periods)} 期"
    return {"flows": flow_rows, "stocks": stock_rows, "flow_heading": "历年", "source": "财务数据表",
            "span_title": span_title,
            "flow_period": flow_period,
            "flow_span": annual and f"{annual[0][:4]}–{annual[-1][:4]}",
            "stock_period": _period_label(latest, "balance"),
            "year_ago": year_ago and _period_label(year_ago, "balance"),
            "stock_span": stock_periods and f"{_period_label(stock_periods[0], 'balance')}–{_period_label(stock_periods[-1], 'balance')}",
            "data": [d for r in flow_rows + stock_rows for d in r["data"]]}


# Same-period comparison rows: (table id, label, cumulative over the year so far).
RATIOS = (("T.gross_margin", "毛利率"), ("T.roe", "加权ROE"), ("T.debt_ratio", "资产负债率"))


def dumbbell(fin: list[dict], start: float = 30.0, end: float = 72.0) -> dict | None:
    """Latest period against the same period a year earlier, on one percent axis."""
    table = {r["id"]: r for r in fin}
    present = [table[k] for k, _ in RATIOS if k in table]
    latest = max((p for r in present for p in r["values"]), default=None)
    if not latest:
        return None
    prior = f"{int(latest[:4]) - 1}{latest[4:]}"
    rows = []
    for key, label in RATIOS:
        if key not in table:
            continue
        before, after = (table[key]["values"].get(p) for p in (prior, latest))
        if number(after) is None:
            continue
        rows.append({"label": label, "before": before, "after": after,
                     "tip": f"{label}：{prior} {before + '%' if number(before) is not None else '缺失'} → "
                            f"{latest} {after}%"})
    if not rows:
        return None
    values = [number(v) for r in rows for v in (r["before"], r["after"]) if number(v) is not None]
    ticks = nice_ticks(min(values), max(values), 4)
    lo, hi = ticks[0], ticks[-1]
    at = lambda v: round(start + (v - lo) / (hi - lo) * (end - start), 3)  # noqa: E731
    for i, r in enumerate(rows):
        b, a = number(r["before"]), number(r["after"])
        r.update(y=i * 34, x_after=at(a), x_before=at(b) if b is not None else None)
        if b is not None:
            r.update(seg_x=min(r["x_before"], r["x_after"]), seg_w=round(abs(r["x_after"] - r["x_before"]), 3))
    height = len(rows) * 34
    return {"rows": rows, "latest": latest, "prior": prior, "track": (at(lo), at(hi)), "height": height + 18,
            "ticks": [{"x": at(t), "text": tick_text(t)} for t in ticks], "axis_y": height + 12,
            "data": [r["tip"] for r in rows]}


def peer_bars(peer: dict) -> dict | None:
    """Visual comparison for a validated peer snapshot; uses only the already-verified cell values."""
    rows = []
    for metric in peer.get("rows") or []:
        if metric.get("unit") != "亿元" or len(metric.get("cells") or []) != 2:
            continue
        c0, c1 = metric["cells"]
        v0, v1 = number(c0.get("value")), number(c1.get("value"))
        if v0 is None and v1 is None:
            continue
        t0, t1 = c0.get("value"), c1.get("value")
        rows.append({"label": metric["label"], "v0": v0, "v1": v1, "t0": t0, "t1": t1,
                     "tip": f"{metric['label']}：{peer['subject_name']} {t0 + ' 亿元' if t0 is not None else '—'}；"
                            f"{peer['name']} {t1 + ' 亿元' if t1 is not None else '—'}"})
    if not rows:
        return None
    return paired_bars(rows, (peer["subject_name"], peer["name"]), mode="peer")


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


def batch_overview(batch: dict) -> dict | None:
    """Industry distribution and 60-day pre-screening returns across the 20 batch candidates."""
    q20 = batch.get("q20") or []
    if not q20:
        return None
    l5 = set((batch.get("l5") or {}).get("codes") or [])
    counts: dict[str, int] = {}
    picked_industries: dict[str, int] = {}
    for r in q20:
        ind = r.get("industry") or "未分类"
        counts[ind] = counts.get(ind, 0) + 1
        if r.get("code") in l5:
            picked_industries[ind] = picked_industries.get(ind, 0) + 1
    ind_rows = []
    for ind, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        p = picked_industries.get(ind, 0)
        ind_rows.append({"label": ind, "value": float(n), "emphasis": p > 0,
                         "text": f"{n} 家" + (f"（选 {p}）" if p else ""),
                         "tip": f"{ind}：候选 {n} 家" + (f"，其中选定 {p} 家" if p else "")})
    mom_rows = []
    for r in q20:
        ret = r.get("return_60d")
        val = float(ret) * 100 if isinstance(ret, (int, float)) else None
        name = r.get("name") or r.get("code", "")
        mom_rows.append({"label": name, "value": val, "emphasis": r.get("code") in l5,
                         "text": f"{val:+.1f}%" if val is not None else "—",
                         "tip": f"{name}：初筛前近 60 个交易日 {f'{val:+.2f}%' if val is not None else '未记录'}"
                                + ("（选定）" if r.get("code") in l5 else "")})
    return {"industries": bars(ind_rows, start=30.0, end=78.0),
            "momentum": bars(mom_rows, start=28.0, end=82.0)}


def candidate_sparks(q20: list[dict]) -> dict[str, dict]:
    """Four-year deducted net profit mini columns for each candidate with full history."""
    out = {}
    for r in q20 or []:
        years, profits = r.get("years") or [], r.get("profit_dedt") or []
        if len(years) == 4 and len(profits) == 4 and all(isinstance(v, (int, float)) for v in profits):
            vals = [round(v / 1e8, 2) for v in profits]
            spark = spark_columns(vals)
            tips = [f"{y} 年扣非净利：{v:.2f} 亿元" for y, v in zip(years, vals)]
            for bar, tip in zip(spark["bars"] if spark else [], tips):
                bar["tip"] = tip
            roes = r.get("roe_dt") or []
            roe_text = " → ".join(f"{v:.1f}%" for v in roes) if len(roes) == 3 and all(isinstance(v, (int, float)) for v in roes) else None
            out[r["code"]] = {"spark": spark, "span": f"{years[0]}–{years[-1]}",
                              "latest": f"{vals[-1]:.2f}", "base": f"{vals[0]:.2f}",
                              "roe_text": roe_text, "data": tips}
    return out


def market_strip(batch: dict) -> dict | None:
    """Proportional breakdown of market boards in the screened universe."""
    markets = (batch.get("universe") or {}).get("markets") or {}
    items = [(k, v) for k, v in markets.items() if isinstance(v, int) and v > 0]
    total = sum(v for _, v in items)
    if not total:
        return None
    segments, cursor = [], 0.0
    for i, (k, v) in enumerate(items):
        pct = round(v / total * 100, 1)
        w = round(100.0 - cursor if i == len(items) - 1 else v / total * 100, 2)
        segments.append({"label": k, "count": v, "pct": pct, "x": round(cursor, 2), "w": max(w, 0.0),
                         "cls": f"b{i % 4}", "tip": f"{k}：{v} 家（占粗筛池 {pct:.1f}%）"})
        cursor += w
    return {"segments": segments, "total": total}


def verdict_summary(companies: list) -> dict | None:
    """Breakdown of latest research verdicts across actively researched companies on the home page."""
    if not companies:
        return None
    buckets = ("值得继续研究", "需要谨慎", "暂不值得", "复核未通过")
    counts = {label: 0 for label in buckets}
    by_code = {}
    for c in companies:
        key = c["verdict"] if c["passed"] and c["verdict"] in VERDICTS else "复核未通过"
        by_code[c["code"]] = key
        counts[key] += 1
    total = len(companies)
    active = [(label, counts[label]) for label in buckets if counts[label] > 0]
    items, cursor = [], 0.0
    for i, (label, count) in enumerate(active):
        pct = round(count / total * 100, 1) if total else 0.0
        w = round(100.0 - cursor if i == len(active) - 1 else count / total * 100, 2)
        items.append({"label": label, "count": count, "pct": pct,
                      "x": round(cursor, 2), "w": max(w, 0.0)})
        cursor += w
    return {"items": items, "total": total, "buckets": by_code}
