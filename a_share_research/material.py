"""Research material: TuShare tables, the annual report's MD&A pages and the persisted snapshot."""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Self

import pandas as pd

from a_share_research import db, sources, evidence, stock_codes
from a_share_research.disclosure_checks import IDENTITY_CONFLICT_REASON
from a_share_research.prompts import render_prompt
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
    ("interest_debt", "四项债务合计（短借、一年内到期非流动负债、长借、债券；数据源空白项按无余额计）", "亿元", "balance", "interest_debt", 1e8),
    ("receivables", "应收票据及应收账款", "亿元", "balance", "accounts_receiv_bill", 1e8),
    ("inventory", "存货", "亿元", "balance", "inventories", 1e8),
    ("contract_liab", "合同负债", "亿元", "balance", "contract_liab", 1e8),
    ("goodwill", "商誉", "亿元", "balance", "goodwill", 1e8),
    ("equity_parent", "归属于母公司股东权益", "亿元", "balance", "total_hldr_eqy_exc_min_int", 1e8),
    ("bps", "每股净资产（各期期末股本口径，送转前后不可直接比较）", "元", "indicator", "bps", 1),
]
MARKET = [
    ("close", "收盘价", "元", 1),
    ("pe_ttm", "市盈率TTM", "倍", 1),
    ("pb", "市净率", "倍", 1),
    ("dv_ttm", "股息率TTM", "%", 1),
    ("total_mv", "总市值", "亿元", 1e4),  # TuShare reports 万元
]
# M.pb and M.dv_ttm carry a basis_meta the program recomputes from table rows; material_block.j2 words it.
# TuShare PB equals close / fina_indicator BPS or market cap / parent equity of one period at 4 decimals
# (42 of 42 companies on 2026-11-27; 34 match both, 7 only per share, 300475 only in aggregate).
PB_MATCH = Decimal("0.001")
# Relative gap beyond which market cap / parent equity is not the same ratio as close / BPS (share bases differ).
PB_AGGREGATE_GAP = Decimal("0.01")
# Same-definition recomputations of dv_ttm stayed within 0.8% (38 of 42 companies, 2026-11-27); the
# rest count dividends by report period or another share base, which the basis note names.
DIVIDEND_MATCH = Decimal("0.01")
# disclosure_checks metric -> (label, TuShare column per estimate source)
ESTIMATE_METRICS = {
    "T.net_profit": ("归母净利润", {"forecast": ("net_profit_min", "net_profit_max"), "express": ("n_income",)}),
    "T.revenue": ("营业收入", {"express": ("revenue",)}),
}


class ScopeError(Exception):
    pass


class ResearchError(Exception):
    pass


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

    def snapshot(self) -> dict:
        data = {
            "code": self.code, "name": self.name,
            "doc": self.doc.info(),
            "pages": self.pages, "raw_pages": self.raw_pages,
            "table": self.table, "market_date": self.market_date,
        }
        if self.extra:
            data["extra"] = self.extra
        return {"version": 2 if self.extra else 1, "sha256": material_hash(data), "data": data}

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
        pending = None
        if self.extra:
            pending = {k: self.extra.get(k) for k in ("question", "context", "methods_used", "gaps")}
            for key in ("prior", "events"):
                if self.extra.get(key):
                    pending[key] = self.extra[key]
        return render_prompt(
            "material_block.j2", name=self.name, code=self.code, market_date=self.market_date, table=self.table,
            holdings_snapshot=holdings_projection(self.extra.get("holdings_snapshot")),
            sources=json.dumps(evidence.projection(self.source_registry(), catalog=True), ensure_ascii=False),
            pending=json.dumps(pending, ensure_ascii=False) if pending is not None else None,
            calculations=json.dumps([r for r in self.table if r["id"].startswith("C.")], ensure_ascii=False),
        )


def holdings_projection(snapshot: dict | None) -> dict | None:
    """The researcher sees the position facts, not account identity, ledger ids or account size."""
    if not snapshot:
        return None
    position = snapshot.get("position")
    weight = snapshot.get("weight")
    view = {k: snapshot[k] for k in ("status", "reason", "monitor", "gaps") if snapshot.get(k) is not None}
    if position:
        view["position"] = {k: position[k] for k in ("shares", "cost_price", "buy_date", "holding_days", "theses")}
    if weight:
        view["weight"] = {k: weight[k] for k in ("weight_pct", "close", "close_date", "total_assets_as_of", "reason")
                          if k in weight}
    return {"status": snapshot["status"], "facts": json.dumps(view, ensure_ascii=False)}


def replay_material(conn, source_run: int) -> Material:
    row = conn.execute("SELECT company_key, material FROM runs WHERE id = ?", (source_run,)).fetchone()
    if row is None or row["material"] is None:
        raise ResearchError(f"任务 {source_run} 没有完整材料快照，不能重放；不会重新取数补齐")
    material = Material.from_snapshot(json.loads(row["material"]))
    if stock_codes.normalize_key(material.code) != row["company_key"] or material.doc.code != material.code:
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
    if key == "interest_debt":
        # A balance-sheet item left blank has no balance (e.g. no bonds issued). Requiring all four
        # emptied this row for 16 of 25 companies in production; a period with no item stays missing.
        present = [(v, weight) for v, (_, weight) in zip(values, columns) if v is not None]
        return (sum(v * weight for v, weight in present) if present else None), columns
    value = sum(v * weight for v, (_, weight) in zip(values, columns)) if all(v is not None for v in values) else None
    return value, columns


def pb_basis(market: dict, statements: dict[str, pd.DataFrame], latest: str) -> dict:
    """Which period's book value reproduces the source PB, and whether the two share bases disagree."""
    basis = {"ratio": "pb", "latest_period": latest, "matched_period": None, "matched_by": None,
             "aggregate_differs": False}
    pb = Decimal(str(market["pb"]))
    candidates = {"bps": Decimal(str(market["close"]))}
    if pd.notna(market.get("total_mv")):
        candidates["equity"] = Decimal(str(market["total_mv"])) * 10000  # 万元
    columns = {"bps": ("indicator", "bps"), "equity": ("balance", "total_hldr_eqy_exc_min_int")}

    def ratio(by: str, period: str) -> Decimal | None:
        frame = statements.get(columns[by][0], pd.DataFrame())
        book = _financial_value(frame.loc[period], columns[by][1]) if period in frame.index else None
        return candidates[by] / book if by in candidates and book and book > 0 else None

    periods = sorted({p for name in ("indicator", "balance") for p in statements.get(name, pd.DataFrame()).index
                      if p <= latest}, reverse=True)[:4]
    for period in periods:
        if by := next((b for b in ("bps", "equity") if (r := ratio(b, period)) is not None
                       and abs(r - pb) <= pb * PB_MATCH), None):
            basis.update(matched_period=period, matched_by=by)
            break
    if basis["matched_period"] == latest and None not in (per_share := ratio("bps", latest), aggregate := ratio("equity", latest)):
        basis["aggregate_differs"] = abs(aggregate - per_share) > per_share * PB_AGGREGATE_GAP
    return basis


def dividend_ttm(dividends: pd.DataFrame, trade_date: str) -> dict:
    """Implemented cash dividends per share with ex-dates in the 12 months to `trade_date`.

    Amounts are restated to the current share base: a bonus or transfer issue on or after an ex-date
    divides that dividend. TuShare repeats one implementation under several announcements or report
    periods, so one ex-date counts once; conflicting amounts on one ex-date are reported, not summed.
    """
    end = pd.Timestamp(trade_date)
    start, last = (end - pd.DateOffset(years=1)).date().isoformat(), end.date().isoformat()
    implemented = dividends[(dividends["div_proc"] == "实施") & dividends["ex_date"].notna()] if not dividends.empty else dividends
    events = {}
    for row in implemented.itertuples():
        ex = pd.Timestamp(row.ex_date).date().isoformat()
        if not start < ex <= last:
            continue  # Older conflicts (600989 in 2024) do not touch this window.
        cash = Decimal("0") if pd.isna(row.cash_div_tax) else Decimal(str(row.cash_div_tax))
        shares = Decimal("0") if pd.isna(row.stk_div) else Decimal(str(row.stk_div))
        event = {"ex_date": ex, "period": pd.Timestamp(row.end_date).date().isoformat(), "cash": cash, "shares": shares}
        if ex in events and (events[ex]["cash"], events[ex]["shares"]) != (cash, shares):
            return {"conflict": ex}
        events.setdefault(ex, event)
    window = [e for e in sorted(events.values(), key=lambda e: e["ex_date"]) if e["cash"] > 0]
    total = Decimal("0")
    for event in window:
        factor = Decimal("1")
        for later in events.values():
            if later["ex_date"] >= event["ex_date"]:
                factor *= 1 + later["shares"]
        event["restated"] = event["cash"] / factor
        total += event["restated"]
    return {"value": total, "events": window}


def dividend_row(ttm: dict, trade_date: str) -> dict:
    """Citable M.dps_ttm row; the note lists each counted ex-date so the model can match announcements."""
    parts = [f"除权日 {e['ex_date']} 每股 {e['cash'].normalize():f} 元（{e['period']} 期"
             + (f"，其后送转折算为 {e['restated'].quantize(Decimal('0.0001')).normalize():f} 元" if e["restated"] != e["cash"] else "")
             + "）" for e in ttm["events"]]
    return {"id": "M.dps_ttm", "label": "近 12 个月已实施每股现金分红（税前，按除权日计，折算到当前股本）", "unit": "元",
            "values": {trade_date: f"{ttm['value']:.2f}"}, "exact_values": {trade_date: str(ttm["value"])},
            "note": "；".join(parts) or "近 12 个月没有已实施的现金分红记录"}


def financial_table(fin: dict[str, pd.DataFrame], market: dict, dividends: pd.DataFrame | None = None) -> list[dict]:
    """Statement rows, then market rows when `market` is given; `dividends` adds the dividend anchor."""
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
    table.extend(capital_return_rows(statements, periods, annual))
    ttm = dividend_ttm(dividends, str(market["trade_date"])) if market and dividends is not None else None
    for key, label, unit, divisor in MARKET:
        if pd.notna(market.get(key)):
            value = Decimal(str(market[key])) / Decimal(str(divisor))
            row = {"id": f"M.{key}", "label": label, "unit": unit,
                   "values": {market["trade_date"]: f"{value:.2f}"},
                   "exact_values": {market["trade_date"]: str(value)}}
            if key == "pb":
                row["basis_meta"] = pb_basis(market, statements, latest)
            elif key == "dv_ttm":
                row["basis_meta"] = {"ratio": key, "latest_period": latest, "dividends": dividend_status(ttm, market)}
            table.append(row)
    if ttm and "value" in ttm:
        table.append(dividend_row(ttm, str(market["trade_date"])))
    return table


def capital_return_rows(statements: dict[str, pd.DataFrame], periods: list[str], annual: list[str]) -> list[dict]:
    """T.ebit, T.minority_equity, T.invested_capital and the annual after-tax T.roic.

    Capital is counted from the financing side (equity incl. minority + four debt items − cash), so the
    operating profit excludes interest income as well as interest expense. Tax is the year's effective
    rate applied to EBIT, which keeps the debt tax shield out of the numerator (method card, 11-14).
    """
    income, balance = statements.get("income", pd.DataFrame()), statements.get("balance", pd.DataFrame())
    uncertain = _unreconciled_periods(income)

    def row_of(frame, period):
        return frame.loc[period] if period in frame.index else None

    def ebit(period):
        row = row_of(income, period)
        profit = _financial_value(row, "total_profit") if row is not None and period not in uncertain else None
        if profit is None:
            return None
        # A blank interest line has no amount, as with the debt items (TuShare leaves it null when nil).
        return profit + (_financial_value(row, "fin_exp_int_exp") or 0) - (_financial_value(row, "fin_exp_int_inc") or 0)

    def minority(period):
        row = row_of(balance, period)
        return _financial_value(row, "minority_int") if row is not None else None

    def invested(period):
        row = row_of(balance, period)
        if row is None:
            return None
        equity, cash = _financial_value(row, "total_hldr_eqy_exc_min_int"), _financial_value(row, "money_cap")
        debt, _ = _financial_metric(row, "interest_debt", "interest_debt")
        if None in (equity, cash, debt):
            return None
        return equity + (minority(period) or 0) + debt - cash

    def roic(period):
        operating, base = ebit(period), invested(f"{int(period[:4]) - 1}-12-31")
        row = row_of(income, period)
        profit, tax = (_financial_value(row, "total_profit"), _financial_value(row, "income_tax")) if row is not None else (None, None)
        if None in (operating, base, profit, tax) or profit <= 0 or base <= 0:
            return None
        return operating * (1 - tax / profit) / base * 100

    specs = [
        ("ebit", "息税前利润（利润总额 + 利息费用 − 利息收入，含投资收益与营业外收支；利息空白项按无发生额计）", "亿元", ebit, periods, Decimal("1e8")),
        ("minority_equity", "少数股东权益", "亿元", minority, periods, Decimal("1e8")),
        ("invested_capital", "投入资本（归母权益 + 少数股东权益 + 四项债务 − 货币资金；少数股东权益空白按无计）", "亿元", invested, periods, Decimal("1e8")),
        ("roic", "税后投入资本回报率（年度息税前利润 ×（1 − 所得税 ÷ 利润总额）÷ 上年末投入资本；利润总额或上年末投入资本不为正的年度不计算）", "%", roic, annual, Decimal("1")),
    ]
    rows = []
    for key, label, unit, compute, dates, divisor in specs:
        values, exact_values = {}, {}
        for period in dates:
            if (raw := compute(period)) is not None:
                values[period] = f"{raw / divisor:.2f}"
                exact_values[period] = str(raw / divisor)
        rows.append({"id": f"T.{key}", "label": label, "unit": unit, "values": values, "exact_values": exact_values})
    return rows


def dividend_status(ttm: dict | None, market: dict) -> str:
    """unavailable / conflict / same / differs: how M.dps_ttm ÷ M.close compares with the source yield."""
    if ttm is None:
        return "unavailable"
    if "conflict" in ttm:
        return f"conflict:{ttm['conflict']}"
    recomputed = ttm["value"] / Decimal(str(market["close"])) * 100
    source = Decimal(str(market["dv_ttm"]))
    return "same" if abs(recomputed - source) <= source * DIVIDEND_MATCH else "differs"


def estimate_source(title: str) -> str | None:
    """The one structured source an announcement title names; None when it names both or neither."""
    express = "快报" in title
    forecast = any(word in title for word in ("预告", "预增", "预减", "预亏", "预盈"))
    return None if express == forecast else "express" if express else "forecast"


def estimate_rows(observations: list[dict], *, code: str, period: str, announced: str,
                  revenue_basis: tuple[bool | None, str | None], retrieved_at: str,
                  expected_source: str | None = None) -> tuple[list[dict], str | None]:
    """Citable rows for the one forecast or express published on `announced`, or why there are none.

    Takes disclosure_checks' normalized observations; carries disclosed figures only, never a judgment.
    `expected_source` keeps a same-day record of the other kind from standing in for this announcement.
    `revenue_basis` is disclosure_checks.revenue_basis for this company and period.
    """
    wanted = {"forecast": "业绩预告", "express": "业绩快报"}.get(expected_source, "预告或快报")
    matches = [o for o in observations if o["period"] == period and o["announced"] == announced
               and expected_source in (None, o["source"])]
    if not matches:
        return [], f"结构化数据中没有公告日 {announced}、期间 {period} 的{wanted}记录"
    if len(matches) > 1:
        return [], f"公告日 {announced}、期间 {period} 匹配到 {len(matches)} 条结构化记录，无法确定对应本公告"
    observation = matches[0]
    if observation.get("type_conflict") or any(
            v.get("reason") == IDENTITY_CONFLICT_REASON for v in observation["values"].values()):
        return [], IDENTITY_CONFLICT_REASON
    source = observation["source"]
    kind = {"forecast": "业绩预告", "express": "业绩快报"}[source]
    detail = f"，类型 {observation['type']}" if observation.get("type") else ""
    rows = []
    for metric, interval in observation["values"].items():
        name, columns = ESTIMATE_METRICS[metric]
        bounds = ((interval["low"], "下限"), (interval["high"], "上限")) if source == "forecast" else (
            (interval["low"], ""),)
        for (number, bound), column in zip(bounds, columns[source]):
            if number is None:
                continue
            matched, reference = revenue_basis
            note = render_prompt(
                "estimate_note.j2", tolerance=f"{t.normalize():f}" if (t := interval.get("tolerance")) else None,
                revenue=source == "express" and metric == "T.revenue",
                period=period, matched=matched, reference=reference)
            row = {"id": f"X.{source}.{code}.{period}.{announced}.{column}",
                   "label": f"{kind}·{name}{bound}（{announced} 公告{detail}）", "unit": "亿元",
                   "values": {period: f"{number:.4f}"}, "exact_values": {period: str(number)},
                   "source": {"provider": "TuShare", "api": source, "field": column, "code": code,
                              "params": {"period": period.replace("-", "")}, "announced": announced,
                              "identity": observation["identity"], "retrieved_at": retrieved_at}}
            if note:
                row["note"] = note
            rows.append(row)
    return rows, None if rows else "结构化记录未给出可用数值"


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
MDNA_GAP = ("年报 D0 未能自动定位“管理层讨论与分析”一节，未预载正文；"
            "请用 read_disclosure 检索 D0（如“管理层讨论与分析”“经营情况讨论”“业务回顾”“财务回顾”）后再引证。")


TOC_SECTION = re.compile(r"^第[一二三四五六七八九十]+节$")
PAGE_NUMBER = re.compile(r"(?:^|\s)(\d{1,3})(?:\s*/\s*\d{1,3})?$")
# 中国海油 prints centered footers such as "- 85 -"; require both decorations.
DASHED_PAGE_NUMBER = re.compile(r"^-\s*(\d{1,3})\s*-$")
# Designed reports print the number first on left-hand pages: "3       Annual Report 2025".
LEADING_PAGE_NUMBER = re.compile(r"^(\d{1,3})\s{2,}\S")
# Number-first contents entries, possibly two columns per line: "34   管理层讨论与分析      94   债券相关情况".
TOC_ENTRY = re.compile(r"(?<!\S)(\d{1,3})\s+([^\d\s]\S*)")


DOTTED_TOC_ENTRY = re.compile(r"^\s*([^\d\s.…·][^.…·]*?)\s*[.…·]{3,}\s*(\d{1,3})\s*$", re.M)
RED_CHIP_BUSINESS = re.compile(r"业务(?:概览|回顾)")
RED_CHIP_FINANCE = re.compile(r"财务(?:概览|回顾)")


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
        if "目录" not in re.sub(r"\s+", "", page):
            continue
        # Title-first dotted leaders, one per line: "业务概览 .......... 15".
        entries = [(title, int(n)) for title, n in DOTTED_TOC_ENTRY.findall(page)]
        if len(entries) >= 5 and [n for _, n in entries] == sorted(n for _, n in entries):
            return entries
        # Columns interleave in reading order, so order entries by page number instead.
        entries = sorted({(int(n), title) for n, title in TOC_ENTRY.findall(page)})
        if len(entries) >= 5:
            return [(title, n) for n, title in entries]
    return []


def _page_offset(doc: Document) -> int | None:
    """PDF page minus printed page number, agreed by most numbered headers or footers."""
    # Keep established numbering ahead of dashed footers: a financial appendix may
    # restart dashed numbering and outnumber the report's main-body page scheme.
    for matchers in ((PAGE_NUMBER.search, LEADING_PAGE_NUMBER.match), (DASHED_PAGE_NUMBER.fullmatch,)):
        offsets = []
        for i, page in enumerate(doc.pages):
            lines = [line.strip() for line in page.splitlines() if line.strip()]
            for line in lines[:1] + lines[-1:]:
                if m := next((m for match in matchers if (m := match(line))), None):
                    offsets.append(i + 1 - int(m[1]))
                    break
        if not offsets:
            continue
        counts = sorted((offsets.count(o) for o in set(offsets)), reverse=True)
        if len(counts) > 1 and counts[0] == counts[1]:
            return None  # two numbering schemes agree equally: the section could sit in either
        best = max(set(offsets), key=offsets.count)
        # Designed reports number only their front part; financial statements restart or go unnumbered.
        if offsets.count(best) >= max(10, len(offsets) * 2 / 3):
            return best
    return None


def _mdna_span_from_toc(doc: Document) -> tuple[int, int] | None:
    """Section bounds from the table of contents, for designed reports whose body headings are images."""
    sections = _toc_sections(doc)
    offset = _page_offset(doc)
    at = next((i for i, (title, _) in enumerate(sections) if "管理层讨论与分析" in title), None)
    last = at
    if at is None:
        # Red-chip reports in their overseas format (e.g. 中国移动) split the MD&A into 业务概览 … 财务概览.
        at = next((i for i, (title, _) in enumerate(sections) if RED_CHIP_BUSINESS.fullmatch(title)), None)
        last = next((i for i, (title, _) in enumerate(sections)
                     if at is not None and i >= at and RED_CHIP_FINANCE.fullmatch(title)), None)
    if at is None or last is None or offset is None or last + 1 >= len(sections):
        return None
    start, end = sections[at][1] + offset, sections[last + 1][1] + offset
    return (start, end) if 1 <= start <= end <= len(doc.pages) else None


def mdna_pages(doc: Document) -> dict[int, str]:
    """Pages of the 管理层讨论与分析 section, whitespace collapsed; empty when it cannot be located.

    Preloading saves the researcher many page-by-page reads but is not a gate: a miss becomes MDNA_GAP
    and the run goes on with read_disclosure. Do not add layout rules for each new report format.
    """
    heads = [(i + 1, m[1]) for i, page in enumerate(doc.pages)
             for line in page.splitlines() if (m := HEADING.match(line))]
    starts = [p for p, title in heads if "管理层讨论与分析" in title]
    if starts:
        start = max(starts)  # the table of contents repeats headings earlier
        end = min((p for p, _ in heads if p > start), default=min(start + 40, len(doc.pages)))
    elif span := _mdna_span_from_toc(doc):
        start, end = span
    else:
        return {}
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
    market, valuation_history = sources.valuation_with_history(code)
    gaps = set()
    try:
        dividends = sources.dividends(code)
    except sources.SourceError as exc:
        dividends = None
        gaps.add(f"分红记录未取得：{exc}")
    table = financial_table(fin, market, dividends)
    for row in table:
        row["source"] = {"provider": "TuShare", "code": annual.code, "retrieved_at": retrieved_at}
    gaps |= {f"{p}：{reason}" for row in table for p, reason in row.get("unavailable", {}).items()}
    market_date = str(market["trade_date"])
    try:
        reference = sources.pe_quantiles(valuation_history, code, market_date)
        for p in (30, 50, 70):
            value = reference[f"p{p}"]
            table.append({"id": f"M.pe_ttm_5y_p{p}", "label": f"近 5 年 PE TTM {p}% 分位", "unit": "倍",
                          "values": {market_date: f"{value:.2f}"}, "exact_values": {market_date: str(value)},
                          "source": {**reference["source"], "retrieved_at": retrieved_at}})
    except sources.SourceError as exc:
        gaps.add(str(exc))
    if not (pages := mdna_pages(annual)):
        gaps.add(MDNA_GAP)
    return Material(
        code=annual.code, name=annual.name.replace(" ", ""), doc=annual, pages=pages,
        raw_pages={n: annual.raw_pages[n - 1] for n in pages},
        table=table, market_date=market_date,
        extra={"catalog": {"D0" if d.url == annual.url else f"D{i + 1}": d.info()
                           for i, d in enumerate(docs)}, "sources": {},
               "gaps": sorted(gaps),
               "series": financial_series(fin, annual.code, retrieved_at)},
    )
