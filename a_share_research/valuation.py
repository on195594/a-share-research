"""A1: validated scenario assumptions and reproducible Decimal valuation (no providers)."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
import re
from statistics import median

RULE = "valuation-v1"
SCENARIOS = ("保守", "中性", "乐观")


def years(table: list[dict]) -> list[int]:
    annual = [int(p[:4]) for row in table if row["id"].startswith("T.")
              for p in row.get("values", {}) if re.fullmatch(r"\d{4}-12-31", p)]
    return list(range(max(annual) + 1, max(annual) + 4)) if annual else []


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _assumption(value) -> Decimal | None:
    return _number(value) if isinstance(value, (int, float, Decimal)) else None


def problems(value, table: list[dict]) -> list[str]:
    """Structure only; prose citations and figures use checking's ordinary item checks."""
    if not isinstance(value, dict):
        return ["valuation 须为三情景对象，或 {not_priced: {text, cites}, next_evidence: 非空文字}"]
    if "not_priced" in value:
        out = []
        if set(value) != {"not_priced", "next_evidence"}:
            out.append("不足以定价时 valuation 只填 not_priced 与 next_evidence，不混入情景字段")
        if not isinstance(value.get("not_priced"), dict):
            out.append("valuation.not_priced 须为 {text: 原因, cites: [出处]}，出处照常核查")
        if not isinstance(value.get("next_evidence"), str) or not value["next_evidence"].strip():
            out.append("valuation.next_evidence 须填写最有价值的下一项证据（非空文字）")
        return out
    out = []
    if set(value) != {"basis", "years", "scenarios", "payout_pct", "rationale"}:
        out.append("valuation 情景对象只含 basis、years、scenarios、payout_pct、rationale 五项")
    if value.get("basis") not in ("net_profit", "core_profit"):
        out.append("valuation.basis 可用 net_profit（归母净利）或 core_profit（扣非归母净利），单位亿元")
    expected = years(table)
    if not expected:
        out.append("财务表缺少年报期间，无法指定 years；请用 not_priced 并说明缺口")
    elif value.get("years") != expected or not all(type(y) is int for y in value["years"]):
        out.append(f"valuation.years 须为最新年报年之后三年：{expected}")
    scenarios = value.get("scenarios")
    if not isinstance(scenarios, dict) or set(scenarios) != set(SCENARIOS):
        out.append("valuation.scenarios 须且仅含 保守、中性、乐观 三个情景")
        scenarios = scenarios if isinstance(scenarios, dict) else {}
    terminal = []
    for name in SCENARIOS:
        scenario = scenarios.get(name)
        if not isinstance(scenario, dict) or set(scenario) != {"profit", "exit_pe"}:
            out.append(f"valuation.scenarios.{name} 须为 {{profit: [三年利润数字], exit_pe: 退出倍数}}")
            continue
        profit = scenario.get("profit")
        if not isinstance(profit, list) or len(profit) != 3 or any(_assumption(p) is None for p in profit):
            out.append(f"{name}.profit 须为 {expected} 对应的三个有限数字，单位亿元")
        else:
            terminal.append(_assumption(profit[2]))
        pe = _assumption(scenario.get("exit_pe"))
        if pe is None or not 0 < pe <= 200:
            out.append(f"{name}.exit_pe 须为第 Y3 年利润对应的退出市盈率，0 < 倍数 ≤ 200")
    if len(terminal) == 3 and not terminal[0] <= terminal[1] <= terminal[2]:
        out.append("Y3 利润须满足 保守 ≤ 中性 ≤ 乐观；退出倍数不要求单调")
    payout = _assumption(value.get("payout_pct"))
    if payout is None or not 0 <= payout <= 100:
        out.append("valuation.payout_pct 须为 0–100 的派息率数字（%）")
    if not isinstance(value.get("rationale"), list) or not value["rationale"]:
        out.append("valuation.rationale 须至少一条 {text: 支撑假设的事实与推理, cites: [出处]}")
    return out


def _json_numbers(value):
    if isinstance(value, Decimal):
        result = float(value)
        if not (-float('inf') < result < float('inf')):
            raise ValueError("情景测算数值超出 JSON 可表示范围，请核对假设")
        return result
    if isinstance(value, dict):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_numbers(item) for item in value]
    return value


def _cell(table: list[dict], ref: str, period: str) -> Decimal:
    row = next((r for r in table if r["id"] == ref), {})
    raw = row.get("exact_values", {}).get(period, row.get("values", {}).get(period))
    value = _number(raw)
    if value is None:
        raise ValueError(f"缺少有效的 {ref}[{period}]，请补齐报告财务或行情表")
    return value


def compute(valuation, table, market_date, *, r=Decimal("0.10"), r_low=Decimal("0.03"),
            pe_reference=None, consensus=None) -> dict:
    """Frozen inputs; fractions are ratios, profit/cap 亿元, prices 元. JSON-ready output."""
    with localcontext() as context:
        context.prec = 40
        return _json_numbers(_compute(valuation, table, market_date, r, r_low, pe_reference, consensus))


def price_zone(close, support, stretched):
    return (None if support >= stretched else "测算支持区" if close <= support else
            "测算透支区" if close >= stretched else "中性区")


def at(calc: dict, close, day: str) -> dict:
    """Roll frozen terminal totals to a closing date, assuming unchanged share count."""
    empty = dict.fromkeys(('annual_returns', 'support_below', 'stretched_above', 'inverted', 'zone', 'horizon'))
    empty.update(as_of=day, close=None)

    def number(value, field):
        result = _number(value)
        if result is None:
            raise ValueError(f'{field} 缺少有限数值，无法滚动测算')
        return result

    try:
        p = number(close, '收盘价')
        empty['close'] = _json_numbers(p)
        if not isinstance(calc, dict):
            raise ValueError('缺少估值测算，无法滚动测算')
        if calc.get('not_priced'):
            raise ValueError('报告未定价（not_priced），无法滚动测算')
        if 'error' in calc:
            raise ValueError(f"报告测算失败：{calc['error']}")
        with localcontext() as context:
            context.prec = 40
            base_close = number(calc['close'], '报告收盘价')
            cap = number(calc['market_cap'], '报告市值')
            r, r_low = (number(calc[key], key) for key in ('r', 'r_low'))
            if min(p, base_close, cap) <= 0:
                raise ValueError('收盘价与报告市值须为正数')
            if min(r, r_low) <= -1:
                raise ValueError('r 与 r_low 须大于 -1')
            horizon = Decimal((date.fromisoformat(calc['exit_date']) - date.fromisoformat(day)).days) / Decimal('365.25')
            if horizon <= 0:
                raise ValueError(f"收盘日须早于退出日 {calc['exit_date']}")
            totals = {name: sum(number(calc['scenarios'][name][key], f'{name}.{key}')
                                for key in ('exit_cap', 'dividends')) for name in SCENARIOS}
            if min(totals.values()) < 0:
                raise ValueError('退出市值加分红为负，无法计算实数年化回报')
            current_cap = cap * p / base_close
            neutral_exit = number(calc['scenarios']['中性']['exit_cap'], '中性.exit_cap')
            neutral_dividends = number(calc['scenarios']['中性']['dividends'], '中性.dividends')
            support = base_close * totals['保守'] / (cap * (1 + r) ** horizon)
            stretched = base_close * totals['中性'] / (cap * (1 + r_low) ** horizon)
            return _json_numbers({
                'annual_returns': {name: (total / current_cap) ** (1 / horizon) - 1 for name, total in totals.items()},
                # Sensitivity endpoint within the frozen nonnegative dividend assumption, not a realized-return bound.
                'neutral_return_without_dividends': ((neutral_exit / current_cap) ** (1 / horizon) - 1
                                                     if neutral_exit >= 0 and neutral_dividends >= 0 else None),
                'support_below': support, 'stretched_above': stretched, 'inverted': support >= stretched,
                'zone': price_zone(p, support, stretched), 'horizon': horizon, 'as_of': day, 'close': p,
                'assumption': '按冻结累计分红尚未支付测算。',
            })
    except KeyError as exc:
        return empty | {'reason': f'缺少测算字段 {exc.args[0]}，无法滚动测算'}
    except (TypeError, ValueError, DecimalException, OverflowError) as exc:
        return empty | {'reason': f'无法滚动测算：{exc}'}


def _compute(value, table, market_date, r, r_low, pe_reference, consensus):
    errors = problems(value, table)
    if errors:
        raise ValueError("；".join(errors))
    as_of = date.fromisoformat(market_date)
    close = _cell(table, "M.close", market_date)
    if "not_priced" in value:
        return {"rule": RULE, "not_priced": True, "as_of": market_date, "close": close}
    cap = _cell(table, "M.total_mv", market_date)
    if close <= 0 or cap <= 0:
        raise ValueError("M.close 与 M.total_mv 须为正数，无法计算当前价格下的回报")
    r, r_low = _number(r), _number(r_low)
    if r is None or r_low is None or min(r, r_low) <= -1:
        raise ValueError("r 与 r_low 须为大于 -1 的有限回报率")
    target_years = years(table)
    base_year = target_years[0] - 1
    base = _cell(table, f'T.{value["basis"]}', f"{base_year}-12-31")
    exit_date = date(target_years[-1] + 1, 4, 30)
    horizon = Decimal((exit_date - as_of).days) / Decimal("365.25")
    if horizon <= 0:
        raise ValueError(f"行情日须早于退出日 {exit_date.isoformat()}")
    payout = _number(value["payout_pct"]) / 100

    def annual(total):
        if total < 0:
            raise ValueError("退出市值加分红为负，无法计算实数年化回报；请调整正常化假设或用 not_priced")
        return (total / cap) ** (1 / horizon) - 1

    def price(total, rate):
        return close * total / (cap * (1 + rate) ** horizon)

    scenarios = {}
    for name in SCENARIOS:
        assumption = value["scenarios"][name]
        profits = [_number(p) for p in assumption["profit"]]
        exit_cap = profits[2] * _number(assumption["exit_pe"])
        dividends = payout * (profits[0] + profits[1])
        total = exit_cap + dividends
        scenarios[name] = {"exit_cap": exit_cap, "dividends": dividends,
                           "annual_return": annual(total), "price_at_r": price(total, r)}
    neutral = value["scenarios"]["中性"]
    p3, pe = _number(neutral["profit"][2]), _number(neutral["exit_pe"])
    dividends = scenarios["中性"]["dividends"]
    support = scenarios["保守"]["price_at_r"]
    stretched = price(scenarios["中性"]["exit_cap"] + dividends, r_low)
    inverted = support >= stretched
    implied = (cap * (1 + r) ** horizon - dividends) / pe
    # A negative required profit has no real CAGR against a positive base either.
    cagr = (implied / base) ** (Decimal(1) / (target_years[-1] - base_year)) - 1 if base > 0 and implied >= 0 else None
    factors = [Decimal(n) / 10 for n in (8, 9, 10, 11, 12)]
    multiples = [_number(value["scenarios"][name]["exit_pe"]) for name in SCENARIOS]
    comparison = deepcopy(consensus) if consensus is not None else {"status": "none", "years": {}}
    comparison["neutral_vs_median"] = {
        str(year): _number(neutral["profit"][i]) / _number(stats["median"]) - 1
        for i, year in enumerate(target_years)
        if (stats := comparison.get("years", {}).get(str(year))) and _number(stats.get("median")) not in (None, 0)
    }
    assumptions = ["假设股本不变，不计增发、回购或摊薄；利润与市值单位亿元，价格单位元。",
                   "退出日为终点年度次年4月30日；持有期按实际天数除以365.25计算。",
                   "累计分红只计前两年利润乘派息率；终点年分红在退出后，不计入；不考虑分红再投资、税费与交易费用。",
                   "回报门槛属于政策假设；参考区间不是已验证的安全边际，情景不承诺兑现。"]
    if value["basis"] == "core_profit":
        assumptions.append("测算采用扣非归母净利润；卖方 np 按归母净利润处理，两者口径不同，对照差异包含口径影响。")
    return {"rule": RULE, "r": r, "r_low": r_low, "as_of": market_date, "close": close, "market_cap": cap,
            "basis": value["basis"], "base_year": base_year, "base_profit": base, "years": target_years,
            "exit_date": exit_date.isoformat(), "horizon": horizon, "scenarios": scenarios,
            "zones": {"support_below": support, "stretched_above": stretched, "inverted": inverted},
            "zone": price_zone(close, support, stretched),
            "implied": {"profit": implied, "cagr": cagr},
            "sensitivity": {"profit_factors": factors, "exit_pe": multiples,
                            "returns": [[annual(p3 * factor * multiple + dividends) for multiple in multiples] for factor in factors]},
            # Multiples of a non-positive base have no meaning; leave them empty as with cagr.
            "bridge": {"profit_change": p3 / base if base > 0 else None,
                       "pe_change": pe / (cap / base) if base > 0 else None, "dividend_share": dividends / cap},
            "pe_reference": pe_reference, "consensus": comparison, "assumptions": assumptions}


def consensus(rows: list[dict], years: list[int], as_of: str, *, coverage=None) -> dict:
    """Latest report per organization/year within the point-in-time 90-day window."""
    cutoff = date.fromisoformat(as_of)
    latest = {}
    for row in rows:
        quarter = re.fullmatch(r"(\d{4})Q4", str(row.get("quarter", "")))
        if not quarter or int(quarter[1]) not in years or not row.get("org_name"):
            continue
        try:
            reported = date.fromisoformat(str(row.get("report_date", "")))
        except ValueError:
            continue
        if not 0 <= (cutoff - reported).days <= 90:
            continue
        key = (row["org_name"], int(quarter[1]))
        if key not in latest or reported > latest[key][0]:
            latest[key] = (reported, _number(row.get("np")))
    result = {}
    for year in years:
        samples = [(reported, np / 10000) for (_, y), (reported, np) in latest.items() if y == year and np is not None]
        if not samples:
            continue
        # TuShare report_rc np is 万元. Captured 603606.SH / 华泰 / 2028Q4:
        # np=217800 => 21.78亿元; np/eps ≈ 8.25亿 shares agrees with current capital.
        # Thus np is treated as attributable net profit (inference from this real response),
        # not total profit (tp). Do not use tp or mix np with deducted core profit.
        profits = [np for _, np in samples]
        result[str(year)] = {"median": median(profits), "n_orgs": len(samples), "min": min(profits), "max": max(profits),
                             "oldest_days": max((cutoff - reported).days for reported, _ in samples)}
    status = "ok" if result else "none"
    if coverage is not None:
        if coverage["covered_days"] == 0:
            status = "unavailable"
        elif coverage["covered_days"] < coverage["trading_days"]:
            status = "partial"
    return _json_numbers({"status": status, "years": result, "coverage":
                          {key: coverage[key] for key in ("trading_days", "covered_days", "truncated_days")}
                          if coverage is not None else None})
