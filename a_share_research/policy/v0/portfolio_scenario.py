"""Static, user-sized sell scenarios. No quotes, database, candidate sizing or orders."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

from a_share_research.policy.v0 import commands_holdings, monitor_contract, risk_budget


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot", required=True, help="已有 monitor-v1 JSON 文件；不自动刷新")
    parser.add_argument("--sell", action="append", required=True, metavar="CODE:SHARES",
                        help="用户明确指定的卖出股数；可重复，不自动生成数量")
    parser.add_argument("--fees", type=float, required=True, help="用户提供的总费用估计，含税；零也须显式指定")
    parser.add_argument("--cash", type=float, help="与快照同一时点的已确认现金；不提供则不推算现金总额")


def compare(snapshot: dict, sales: dict[str, int], *, fees: float, cash: float | None = None) -> dict:
    monitor_contract.validate_monitor_snapshot(snapshot)
    if not sales or any(not re.fullmatch(r"[0-9]{6}", code) or not risk_budget.valid_shares(n)
                        for code, n in sales.items()):
        raise ValueError("须明确指定代码和正整数卖出股数")
    if not risk_budget.finite_number(fees) or fees < 0:
        raise ValueError("费用须为显式、有限、非负数")
    if cash is not None and (not risk_budget.finite_number(cash) or cash < 0):
        raise ValueError("同一快照时点的现金须为有限非负数")
    account = snapshot["account"]
    total = account.get("portfolio_value")
    if (not risk_budget.positive_number(total) or account.get("denominator_status") != "explicit"
            or account.get("denominator_requires_review") is not False
            or not snapshot["quote_coverage"]["complete"] or snapshot["valuation_status"] != "exact"):
        raise ValueError("缺少已确认分母或完整估值，不能形成组合情景")
    if fees >= total:
        raise ValueError("费用必须小于组合总资产")
    policy = account["risk_policy"]  # Already checked by the monitor contract.
    holdings = snapshot["holdings"]
    codes = [h["code"] for h in holdings]
    if len(codes) != len(set(codes)) or not set(sales) <= set(codes):
        raise ValueError("持仓代码重复或指定股票不在快照中")
    before, after, trades = [], [], []
    gross = 0.0
    for holding in holdings:
        code, shares, quote = holding["code"], holding.get("shares"), holding["quote"]
        price, stop = quote.get("price"), holding.get("stop_loss_20")
        if (not risk_budget.valid_shares(shares) or quote.get("status") != "fresh"
                or not quote.get("as_of") or not quote.get("source")
                or not risk_budget.positive_number(price) or not risk_budget.positive_number(stop)):
            raise ValueError(f"{code} 缺少快照当时的完整报价、股数或止损线")
        sold = sales.get(code, 0)
        if sold > shares:
            raise ValueError(f"{code} 指定卖出数量超过持仓；不代表已验证可卖数量")
        reference = holding.get("reference_cost")
        original = commands_holdings.calculate_position_risk(shares, price, stop, total, reference)
        remaining = commands_holdings.calculate_position_risk(shares - sold, price, stop, total - fees, reference)
        if shares == sold:
            remaining = {key: 0.0 for key in original}
        if any(original[key] is None for key in ("market_value", "stop_risk", "stop_risk_pct")):
            raise ValueError(f"{code} 风险计算溢出或缺失")
        before.append({"code": code, "shares": shares, **original})
        after.append({"code": code, "shares": shares - sold, **remaining})
        if sold:
            proceeds = sold * price
            gross += proceeds
            trades.append({"code": code, "shares": sold, "assumed_price": price,
                           "quote_as_of": quote["as_of"], "gross_proceeds": proceeds})
    if fees > gross:
        raise ValueError("费用大于卖出所得；请检查输入")

    def totals(items: list[dict], value: float) -> dict:
        market_value = sum(item["market_value"] for item in items)
        return {"portfolio_value": value, "stock_market_value": market_value,
                "stock_weight_pct": market_value / value * 100,
                **risk_budget.assess(items, value, policy,
                                     account_scope=account.get("account_scope"),
                                     denominator_source="static_snapshot")}

    baseline = totals(before, total)
    for key in ("stock_market_value", "total_stop_risk", "total_stop_risk_pct"):
        if account.get(key) is not None and not math.isclose(account[key], baseline[key], rel_tol=1e-9, abs_tol=1e-8):
            raise ValueError(f"快照汇总 {key} 与明细冲突；先核对，不能用情景覆盖")
    if cash is not None and cash + sum(item["market_value"] for item in before) > total + 0.01:
        raise ValueError("现金与股票市值超过组合分母；检查同一时点和账户范围")
    result = {"version": "portfolio-scenario-v1", "kind": "static_sell_comparison",
              "snapshot_as_of": snapshot["as_of"], "account_scope": account.get("account_scope"),
              "source_denominator": {key: account.get(key) for key in
                                     ("denominator_components", "denominator_status", "portfolio_value_as_of",
                                      "denominator_scope_status", "denominator_freshness_status",
                                      "denominator_requires_review")},
              "risk_policy": policy, "before": baseline, "after": totals(after, total - fees),
              "positions_before": before, "positions_after": after, "user_specified_sales": trades,
              "estimated_fees": fees, "cash_delta": gross - fees,
              "cash_before": cash, "cash_after": cash + gross - fees if cash is not None else None,
              "action_status": "not_formed", "execution_status": "not_checked", "writes": False,
              "source_action_status": snapshot["action_status"],
              "source_review_status": snapshot["review_status"], "source_data_status": snapshot["data_status"],
              "source_data_gaps": snapshot["data_gaps"],
              "source_escalations": snapshot["escalations"],
              "limits": ["仅按历史快照价格静态比较，不代表当前报价或投资建议；未生成交易候选。",
                         "费用为用户估计；成交价、滑点、T+1可卖数量、交易单位和成交可行性均未核验。",
                         "卖出所得转为现金，不重复计入总资产；总资产仅扣估计费用。",
                         "风险预算改善不清除原门禁、L3、估值或复核缺口；不自动更新账户。"]}
    # Reject overflow instead of serializing Infinity in a machine contract.
    json.dumps(result, allow_nan=False)
    return result


def cmd_portfolio_scenario(argv: list[str]) -> int:
    """Compare only user-specified sales against an existing snapshot; no state writes."""
    parser = argparse.ArgumentParser(prog="a-stock-cache portfolio-scenario")
    configure_parser(parser)
    args = parser.parse_args(argv)
    try:
        sales = {}
        for item in args.sell:
            match = re.fullmatch(r"([0-9]{6}):([1-9][0-9]*)", item)
            if not match or match[1] in sales:
                raise ValueError("--sell 须为 CODE:SHARES，同一股票只指定一次")
            sales[match[1]] = int(match[2])
        snapshot = monitor_contract.loads_monitor_snapshot(Path(args.snapshot).read_text(encoding="utf-8"))
        result = compare(snapshot, sales, fees=args.fees, cash=args.cash)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(f"情景不可计算：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0
