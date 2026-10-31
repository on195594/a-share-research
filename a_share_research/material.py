"""Research material: TuShare tables, the annual report's MD&A pages and the persisted snapshot."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Self

import pandas as pd

from a_share_research import db, sources, evidence
from a_share_research.sources import Document
from a_share_research.evidence import material_hash

# (id, label, unit, statement, column, divisor)
METRICS = [
    ("revenue", "营业总收入", "亿元", "income", "total_revenue", 1e8),
    ("net_profit", "归母净利润", "亿元", "income", "n_income_attr_p", 1e8),
    ("op_cash", "经营活动现金流净额", "亿元", "cashflow", "n_cashflow_act", 1e8),
    ("revenue_yoy", "营业收入同比", "%", "indicator", "or_yoy", 1),
    ("profit_yoy", "归母净利同比", "%", "indicator", "netprofit_yoy", 1),
    ("core_profit_yoy", "扣非净利同比", "%", "indicator", "dt_netprofit_yoy", 1),
    ("gross_margin", "毛利率", "%", "indicator", "grossprofit_margin", 1),
    ("roe", "加权ROE", "%", "indicator", "roe_waa", 1),
    ("debt_ratio", "资产负债率", "%", "indicator", "debt_to_assets", 1),
    ("core_profit", "扣非归母净利润", "亿元", "deducted", "profit_dedt", 1e8),
    ("capex", "购建长期资产支付的现金", "亿元", "cashflow", "c_pay_acq_const_fiolta", 1e8),
    ("fcf", "经营现金流减购建长期资产现金", "亿元", "cashflow", "fcf", 1e8),
    ("cash", "货币资金", "亿元", "balance", "money_cap", 1e8),
    ("interest_debt", "四项债务合计（短借、一年内到期非流动负债、长借、债券）", "亿元", "balance", "interest_debt", 1e8),
    ("receivables", "应收票据及应收账款", "亿元", "balance", "accounts_receiv_bill", 1e8),
    ("inventory", "存货", "亿元", "balance", "inventories", 1e8),
    ("contract_liab", "合同负债", "亿元", "balance", "contract_liab", 1e8),
    ("goodwill", "商誉", "亿元", "balance", "goodwill", 1e8),
]
MARKET = [
    ("close", "收盘价", "元", 1),
    ("pe_ttm", "市盈率TTM", "倍", 1),
    ("pb", "市净率", "倍", 1),
    ("dv_ttm", "股息率TTM", "%", 1),
    ("total_mv", "总市值", "亿元", 1e4),  # TuShare reports 万元
]


class ScopeError(Exception):
    pass


class ResearchError(Exception):
    pass


def _snapshot_key(value):
    """Compare ordered JSON structure without serializing or copying long page strings."""
    if isinstance(value, dict):
        return (dict, tuple((_snapshot_key(k), _snapshot_key(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(_snapshot_key(v) for v in value))
    return (type(value), repr(value) if isinstance(value, float) else value)


@dataclass
class Material:
    code: str
    name: str
    doc: Document
    pages: dict[int, str]  # MD&A pages of the annual report, 1-based page numbers
    raw_pages: dict[int, str]  # same pages in content-stream order, only for quote matching
    table: list[dict]
    market_date: str
    extra: dict = field(default_factory=dict)
    _cached_key: tuple | None = field(default=None, init=False, repr=False, compare=False)
    _cached_snapshot: dict | None = field(default=None, init=False, repr=False, compare=False)

    def snapshot(self) -> dict:
        data = {
            "code": self.code, "name": self.name,
            "doc": self.doc.info(),
            "pages": self.pages, "raw_pages": self.raw_pages,
            "table": self.table, "market_date": self.market_date,
        }
        if self.extra:
            data["extra"] = self.extra
        key = _snapshot_key(data)
        if key != self._cached_key:
            self._cached_snapshot = {"version": 2 if self.extra else 1,
                                     "sha256": material_hash(data), "data": deepcopy(data)}
            self._cached_key = key
        # Callers may edit a snapshot; neither they nor later material edits may poison the cache.
        return deepcopy(self._cached_snapshot)

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> Self:
        if snapshot.get("version") not in (1, 2):
            raise ResearchError("不支持的材料快照版本")
        data = snapshot["data"]
        if material_hash(data) != snapshot["sha256"]:
            raise ResearchError("材料快照哈希不匹配")
        return cls(
            code=data["code"], name=data["name"], doc=Document(**data["doc"]),
            pages={int(n): text for n, text in data["pages"].items()},
            raw_pages={int(n): text for n, text in data["raw_pages"].items()},
            table=data["table"], market_date=data["market_date"], extra=data.get("extra", {}),
        )

    def source_registry(self) -> dict:
        return evidence.source_registry(self.doc.info(), self.pages, self.raw_pages,
                                        self.extra.get("sources"), self.extra.get("catalog"))

    def prompt_block(self) -> str:
        rows = [
            f"[{r['id']}] {r['label']}（{r['unit']}）：" + ("；".join(f"{p} {v}" for p, v in r["values"].items()) or "缺失")
            for r in self.table
        ]
        block = (
            f"公司：{self.name}（{self.code}）\n\n"
            f"## 财务与行情表（程序从 TuShare 生成；行情为 {self.market_date} 收盘）\n" + "\n".join(rows)
            + "\n\n# 统一原文证据（默认 source=D0；read 为已读，listed 仅目录，不可引证；URL 由来源映射交付，无需正文手抄）\n"
            + json.dumps(evidence.projection(self.source_registry(), catalog=True), ensure_ascii=False)
        )
        if self.extra:
            pending = {k: self.extra.get(k) for k in ("question", "context", "methods_used", "gaps")}
            if self.extra.get("prior"):
                pending["prior"] = self.extra["prior"]
            block += "\n\n# 本次问题与待核查上下文（不是事实来源）\n" + json.dumps(pending, ensure_ascii=False)
        block += "\n\n# 程序计算依据\n" + json.dumps(
            [r for r in self.table if r["id"].startswith("C.")], ensure_ascii=False)
        return block


def replay_material(conn, source_run: int) -> Material:
    row = conn.execute("SELECT code, material FROM runs WHERE id = ?", (source_run,)).fetchone()
    if row is None or row["material"] is None:
        raise ResearchError(f"任务 {source_run} 没有完整材料快照，不能重放；不会重新取数补齐")
    material = Material.from_snapshot(json.loads(row["material"]))
    if material.code[:6] != row["code"][:6] or material.doc.code != material.code:
        raise ResearchError("材料快照与任务公司不一致")
    return material


def _latest_rows(frame: pd.DataFrame, *, consolidated: bool) -> pd.DataFrame:
    frame = frame.copy()
    if consolidated:
        frame = frame[frame["report_type"].astype(str) == "1"]  # consolidated statements
    frame["end_date"] = frame["end_date"].astype(str)
    order = [c for c in ("ann_date", "f_ann_date", "update_flag") if c in frame]
    return frame.sort_values(order, na_position="first").groupby("end_date").tail(1).set_index("end_date")


def _unreconciled_periods(frame: pd.DataFrame) -> dict[str, str]:
    """A later correction in the same financial year makes older versions uncertain, not zero."""
    corrections = {}
    for period, row in frame.iterrows():
        announced, actual = row.get("ann_date"), row.get("f_ann_date")
        if pd.notna(announced) and pd.notna(actual) and str(actual) > str(announced):
            corrections[period[:4]] = max(corrections.get(period[:4], ""), str(actual))
    return {period: corrections[period[:4]] for period, row in frame.iterrows()
            if period[:4] in corrections and
            (str(row["f_ann_date"]) if pd.notna(row.get("f_ann_date")) else str(row.get("ann_date", "")))
            < corrections[period[:4]]}


def _financial_value(row, column: str) -> Decimal | None:
    raw = row.get(column)
    return Decimal(str(raw)) if raw is not None and pd.notna(raw) else None


def _financial_metric(row, key: str, column: str) -> tuple[Decimal | None, list[tuple[str, int]]]:
    columns = [(column, 1)]
    if key == "fcf":
        columns = [("n_cashflow_act", 1), ("c_pay_acq_const_fiolta", -1)]
    elif key == "interest_debt":
        columns = [(c, 1) for c in ("st_borr", "non_cur_liab_due_1y", "lt_borr", "bond_payable")]
    elif key == "receivables" and _financial_value(row, column) is None:
        columns = [("notes_receiv", 1), ("accounts_receiv", 1)]
    values = [_financial_value(row, c) for c, _ in columns]
    value = sum(v * weight for v, (_, weight) in zip(values, columns)) if all(v is not None for v in values) else None
    return value, columns


def financial_table(fin: dict[str, pd.DataFrame], market: dict) -> list[dict]:
    # Indicators have no report_type; the provider keeps that common column null.
    statements = {k: _latest_rows(v, consolidated=k in ("income", "cashflow", "balance")) for k, v in fin.items()}
    ends = sorted(statements["income"].index)
    annual = [e for e in ends if e.endswith("12-31")][-3:]
    latest = ends[-1]
    current = [p for p in ends if p[:4] == latest[:4]]
    prior = [f"{int(p[:4]) - 1}{p[4:]}" for p in current]
    periods = sorted(set(annual + current + [p for p in prior if p in ends]))
    table = []
    for key, label, unit, statement, column, divisor in METRICS:
        frame = statements.get(statement, pd.DataFrame())
        uncertain = _unreconciled_periods(frame) if statement in ("income", "cashflow") else {}
        values, exact_values = {}, {}
        for p in periods:
            raw, _ = _financial_metric(frame.loc[p] if p in frame.index else {}, key, column)
            if raw is not None and p not in uncertain:
                values[p] = f"{raw / Decimal(str(divisor)):.2f}"
                exact_values[p] = str(raw / Decimal(str(divisor)))
        row = {"id": f"T.{key}", "label": label, "unit": unit, "values": values, "exact_values": exact_values}
        if unavailable := {p: f"同年{'利润表' if statement == 'income' else '现金流量表'}于 {uncertain[p]} 有更正，"
                              "本期未见同步版本，暂不作为可比数值；须补读披露核对。"
                           for p in periods if p in uncertain}:
            row["unavailable"] = unavailable
        table.append(row)
    for key, label, unit, divisor in MARKET:
        if pd.notna(market.get(key)):
            value = Decimal(str(market[key])) / Decimal(str(divisor))
            table.append({"id": f"M.{key}", "label": label, "unit": unit,
                          "values": {market["trade_date"]: f"{value:.2f}"},
                          "exact_values": {market["trade_date"]: str(value)}})
    return table


def financial_series(fin: dict[str, pd.DataFrame], code: str, retrieved_at: str) -> dict:
    """Human-readable operating history; the series itself stays outside model evidence."""
    latest = {k: _latest_rows(v, consolidated=k in ("income", "cashflow", "balance")) for k, v in fin.items()}
    quarters = ("03-31", "06-30", "09-30", "12-31")

    def recent_quarters(end: str) -> list[str]:
        number = int(end[:4]) * 4 + quarters.index(end[5:])
        return [f"{n // 4}-{quarters[n % 4]}" for n in range(number - 7, number + 1)]

    def annual_periods(ends) -> list[str]:
        years = [int(p[:4]) for p in ends if p.endswith("12-31")]
        return [f"{year}-12-31" for year in range(max(years) - 4, max(years) + 1)] if years else []

    income_ends = sorted(latest["income"].index)
    end = income_ends[-1]
    balance_ends = sorted(latest.get("balance", pd.DataFrame()).index)
    periods = {"annual": annual_periods(income_ends), "quarterly": recent_quarters(end), "ttm": [end],
               "balance": sorted(set(annual_periods(balance_ends) + (recent_quarters(balance_ends[-1]) if balance_ends else [])))}
    result = {kind: {"periods": dates, "rows": []} for kind, dates in periods.items()}
    result["source"] = {"provider": "TuShare", "code": code, "retrieved_at": retrieved_at}

    for key, label, _, statement, column, divisor in METRICS:
        if divisor != 1e8:
            continue  # Cumulative ratios are not quarterly flow measures.
        frame = latest.get(statement, pd.DataFrame())
        uncertain = _unreconciled_periods(frame) if statement in ("income", "cashflow") else {}
        versions = fin.get(statement, pd.DataFrame())
        if statement in ("income", "cashflow", "balance") and not versions.empty:
            versions = versions[versions["report_type"].astype(str) == "1"]
        cells = {}
        for p, row in frame.iterrows():
            raw, columns = _financial_metric(row, key, column)
            history = versions[versions["end_date"] == p]
            historical = {_financial_metric(v, key, column)[0] for _, v in history.iterrows()}
            historical.discard(None)
            metadata = {name: str(row[name]) if name in row and pd.notna(row[name]) else None
                        for name in ("ann_date", "f_ann_date", "update_flag")}
            later = bool(metadata["f_ann_date"] and metadata["ann_date"]
                         and metadata["f_ann_date"] > metadata["ann_date"])
            inputs = []
            for c, weight in columns:
                past = {_financial_value(v, c) for _, v in history.iterrows()}
                past.discard(None)
                value = _financial_value(row, c)
                inputs.append({"statement": statement, "column": c, "period": p, "weight": weight,
                               "value": str(value) if value is not None else None, **metadata,
                               "retrieved_at": retrieved_at, "corrected": later or len(past) > 1})
            cells[p] = {"value": raw, "inputs": inputs,
                        "corrected": len(historical) > 1 or any(item["corrected"] for item in inputs),
                        "unreconciled_since": uncertain.get(p)}

        def measure(kind: str, p: str) -> dict:
            year, quarter = int(p[:4]), quarters.index(p[5:])
            terms = [(p, 1)]
            if kind == "quarterly" and quarter:
                terms += [(f"{year}-{quarters[quarter - 1]}", -1)]
            elif kind == "ttm" and quarter != 3:
                terms += [(f"{year - 1}-12-31", 1), (f"{year - 1}-{quarters[quarter]}", -1)]
            selected = [cells.get(date, {"value": None, "inputs": [], "corrected": False}) for date, _ in terms]
            unreconciled = {date: cell["unreconciled_since"] for (date, _), cell in zip(terms, selected)
                            if cell.get("unreconciled_since")}
            raw = sum(cell["value"] * weight for cell, (_, weight) in zip(selected, terms)) if not unreconciled and all(cell["value"] is not None for cell in selected) else None
            return {"value": str(raw) if raw is not None else None,
                    "display": f"{raw / Decimal('1e8'):.2f}" if raw is not None else None,
                    "inputs": [item | {"weight": item["weight"] * weight}
                               for cell, (_, weight) in zip(selected, terms) for item in cell["inputs"]],
                    "missing": [date for (date, _), cell in zip(terms, selected) if cell["value"] is None],
                    "corrected": any(cell["corrected"] for cell in selected),
                    **({"unreconciled": unreconciled} if unreconciled else {})}

        for kind in ("balance",) if statement == "balance" else ("annual", "quarterly", "ttm"):
            values = {}
            for p in periods[kind]:
                cell = measure(kind, p)
                if kind != "balance":
                    base_period = f"{int(p[:4]) - 1}{p[4:]}"
                    base = measure(kind, base_period)
                    base_raw = Decimal(base["value"]) if base["value"] is not None else None
                    cell["yoy"] = {"value": f"{(Decimal(cell['value']) / base_raw - 1) * 100:+.2f}" if cell["value"] is not None and base_raw is not None and base_raw > 0 else None,
                                   "period": base_period, "base": base}
                values[p] = cell
            result[kind]["rows"].append({"id": key, "label": label, "values": values})
    return result


# Titles never quote; a wrapped cross-reference like 第四节“经营情况讨论与分析”之… does.
HEADING = re.compile(r"^\s*第[一二三四五六七八九十]+节\s*([^\s.…“”\"《][^.…“”\"《]*)$")


TOC_SECTION = re.compile(r"^第[一二三四五六七八九十]+节$")
PAGE_NUMBER = re.compile(r"(?:^|\s)(\d{1,3})(?:\s*/\s*\d{1,3})?$")
# Designed reports print the number first on left-hand pages: "3       Annual Report 2025".
LEADING_PAGE_NUMBER = re.compile(r"^(\d{1,3})\s{2,}\S")
# Number-first contents entries, possibly two columns per line: "34   管理层讨论与分析      94   债券相关情况".
TOC_ENTRY = re.compile(r"(?<!\S)(\d{1,3})\s+([^\d\s]\S*)")


def _toc_sections(doc: Document) -> list[tuple[str, int]]:
    """(title, printed page) per 第X节 entry of the table of contents, in order."""
    for page in doc.pages[:10]:
        tokens = re.sub(r"[.…·]+", " ", page).split()
        entries: list[list] = []
        for token in tokens:
            if TOC_SECTION.match(token):
                entries.append(["", None])
            elif entries and entries[-1][1] is None and token.isdigit() and len(token) <= 3:
                entries[-1][1] = int(token)
            elif entries:
                entries[-1][0] += token
        if len(entries) >= 5 and all(n is not None for _, n in entries):
            return [(title, n) for title, n in entries]
    for page in doc.pages[:10]:
        if "目录" not in page:
            continue
        # Columns interleave in reading order, so order entries by page number instead.
        entries = sorted({(int(n), title) for n, title in TOC_ENTRY.findall(page)})
        if len(entries) >= 5:
            return [(title, n) for n, title in entries]
    return []


def _page_offset(doc: Document) -> int | None:
    """PDF page minus printed page number, agreed by most numbered headers or footers."""
    offsets = []
    for i, page in enumerate(doc.pages):
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        for line in lines[:1] + lines[-1:]:
            if m := PAGE_NUMBER.search(line) or LEADING_PAGE_NUMBER.match(line):
                offsets.append(i + 1 - int(m[1]))
                break
    if not offsets:
        return None
    counts = sorted((offsets.count(o) for o in set(offsets)), reverse=True)
    if len(counts) > 1 and counts[0] == counts[1]:
        return None  # two numbering schemes agree equally: the section could sit in either
    best = max(set(offsets), key=offsets.count)
    # Designed reports number only their front part; financial statements restart or go unnumbered.
    return best if offsets.count(best) >= max(10, len(offsets) * 2 / 3) else None


def _mdna_span_from_toc(doc: Document) -> tuple[int, int] | None:
    """Section bounds from the table of contents, for designed reports whose body headings are images."""
    sections = _toc_sections(doc)
    offset = _page_offset(doc)
    at = next((i for i, (title, _) in enumerate(sections) if "管理层讨论与分析" in title), None)
    if at is None or offset is None or at + 1 >= len(sections):
        return None
    start, end = sections[at][1] + offset, sections[at + 1][1] + offset
    return (start, end) if 1 <= start <= end <= len(doc.pages) else None


def mdna_pages(doc: Document) -> dict[int, str]:
    """Pages of the 管理层讨论与分析 section, whitespace collapsed."""
    heads = [(i + 1, m[1]) for i, page in enumerate(doc.pages)
             for line in page.splitlines() if (m := HEADING.match(line))]
    starts = [p for p, title in heads if "管理层讨论与分析" in title]
    if starts:
        start = max(starts)  # the table of contents repeats headings earlier
        end = min((p for p, _ in heads if p > start), default=min(start + 40, len(doc.pages)))
    elif span := _mdna_span_from_toc(doc):
        start, end = span
    else:
        raise ResearchError("年报中找不到“管理层讨论与分析”一节")
    return {n: evidence.normalize_page(doc.pages[n - 1]) for n in range(start, end + 1)}


def gather(code: str) -> Material:
    fin = sources.financials(code)
    retrieved_at = db.now()
    income = fin["income"].sort_values("end_date")
    if str(income["comp_type"].iloc[-1]) != "1":
        raise ScopeError("银行、保险、券商等金融类公司暂不支持")
    docs = sources.reports(code)
    annual = next((d for d in docs if d.period.endswith("12-31")), None)
    if annual is None:
        raise ResearchError("近 18 个月没有年报")
    sources.fetch(annual)
    market = sources.valuation(code)
    table = financial_table(fin, market)
    for row in table:
        row["source"] = {"provider": "TuShare", "code": annual.code, "retrieved_at": retrieved_at}
    return Material(
        code=annual.code, name=annual.name.replace(" ", ""), doc=annual, pages=(pages := mdna_pages(annual)),
        raw_pages={n: annual.raw_pages[n - 1] for n in pages},
        table=table, market_date=str(market["trade_date"]),
        extra={"catalog": {"D0" if d.url == annual.url else f"D{i + 1}": d.info()
                           for i, d in enumerate(docs)}, "sources": {},
               "gaps": sorted({f"{p}：{reason}" for row in table for p, reason in row.get("unavailable", {}).items()}),
               "series": financial_series(fin, annual.code, retrieved_at)},
    )
