"""Pure legacy ledger/index comparison; acquisition belongs to the caller."""
import math

from a_share_research.policy.v0.position_ledger import ledger_cash_flows, public_market_equivalent

BENCHMARKS = (("H00300.CSI", "沪深300全收益"), ("H00922.CSI", "中证红利全收益"))


def prepare(events: list, benchmarks: dict) -> dict:
    if not events or set(benchmarks) != {code for code, _ in BENCHMARKS} or any(not v for v in benchmarks.values()):
        raise ValueError('账本或指数资料不完整')
    first = events[0][2]
    valuation_day = min(max(closes) for closes in benchmarks.values())
    if valuation_day < first:
        raise ValueError('估值日期早于首笔交易')
    flows = ledger_cash_flows([row[1:] for row in events if row[2] <= valuation_day])
    open_shares = {}
    for code, event_type, day, shares, *_ in events:
        if day <= valuation_day and event_type in ('buy', 'sell', 'adjustment'):
            delta = (shares or 0) * (-1 if event_type == 'sell' else 1)
            open_shares[code] = open_shares.get(code, 0) + delta
    return dict(first=first, valuation_day=valuation_day, flows=[list(row) for row in flows], open_shares=open_shares)


def calculate(events: list, benchmarks: dict, prices: dict) -> dict:
    prepared = prepare(events, benchmarks)
    first, valuation_day, flows, open_shares = (prepared[k] for k in ('first', 'valuation_day', 'flows', 'open_shares'))
    required = {code for code, shares in open_shares.items() if shares}
    if set(prices) != required or any(type(p) not in (int, float) or not math.isfinite(p) or p <= 0 for p in prices.values()):
        raise ValueError('在仓股票缺少有效价格')
    market_value = sum(shares * prices[code] for code, shares in open_shares.items() if shares)
    gross_buys = -sum(flow for _, flow in flows if flow < 0)
    pnl = sum(flow for _, flow in flows) + market_value
    if gross_buys <= 0 or not all(math.isfinite(v) for v in (market_value, gross_buys, pnl)):
        raise ValueError('账本没有有效买入现金流或计算溢出')
    lines = [f"账本 vs 指数（{first} 至 {valuation_day} 收盘）| 累计买入 {gross_buys:,.0f} 元 | 在仓市值 {market_value:,.0f} 元",
             f"  账本净盈亏：{pnl:+,.0f} 元（{pnl / gross_buys:+.2%}）"]
    comparisons = []
    for code, label in BENCHMARKS:
        closes = {day: px for day, px in benchmarks[code].items() if day <= valuation_day}
        bench = public_market_equivalent(flows, closes)
        excess = pnl - bench
        if not math.isfinite(bench) or not math.isfinite(excess):
            raise ValueError('指数比较计算溢出')
        comparisons.append(dict(code=code, label=label, pnl=bench, excess=excess))
        lines.append(f"  同样现金流买{label}：{bench:+,.0f} 元（{bench / gross_buys:+.2%}） | 账本超额 {excess:+,.0f} 元")
    contains_inferred = any(row[8] for row in events)
    if contains_inferred:
        lines.append('  ⚠️ 含推断补录的早期事件：结果为近似值，须与券商流水核对')
    lines.append('  样本期短、交易少时只说明期间相对表现，不证明方法有效')
    return prepared | dict(stdout='\n'.join(lines) + '\n', market_value=market_value, gross_buys=gross_buys,
                           pnl=pnl, comparisons=comparisons, contains_inferred=contains_inferred)
