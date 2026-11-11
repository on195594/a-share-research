"""Risk-budget sizing for a proposed new or added position. Read-only: no quotes, database or orders.

Size comes from the account's own stop-risk budgets instead of an unstated weight cap:
shares x (price - second-tier stop) must fit both the single-stock budget and what remains of the
portfolio budget in an existing monitor-v1 snapshot. The stop is the framework's second-tier stop
from the proposed buy price, the same rule `buy-holding` records after a fill.
See docs/specs/2026-11-04-proactive-decision-spec.md.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys

from a_share_research.policy.v0 import boards, commands_holdings, domain, monitor_contract, risk_budget

TRANCHES = (("1/3", 1 / 3), ("2/3", 2 / 3), ("1", 1))


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot", required=True, help="已有 monitor-v1 JSON 文件；不自动刷新")
    parser.add_argument("--code", required=True, help="拟买入的六位代码")
    parser.add_argument("--price", type=float, required=True, help="拟买入价（用户或快照给出的价格）")
    parser.add_argument("--framework", required=True, help="框架，如 A/B/C/D/E/F；决定第二档止损比例")
    parser.add_argument("--cash", type=float, help="同一时点可动用资金（含可先卖出的基准 ETF）；不提供则不按资金约束")


def _valid_quantity(code: str, shares: int) -> int:
    """Largest board-valid buy quantity not above `shares`, or 0."""
    board, lot = commands_holdings._a_share_board(code)
    if board in {"STAR", "BSE"}:
        return shares if shares >= lot else 0
    return shares // lot * lot


def size(snapshot: dict, code: str, price: float, framework: str, *, cash: float | None = None) -> dict:
    monitor_contract.validate_monitor_snapshot(snapshot)
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("代码须为六位数字")
    if not risk_budget.positive_number(price):
        raise ValueError("拟买入价须为正数")
    if cash is not None and (not risk_budget.finite_number(cash) or cash < 0):
        raise ValueError("可动用资金须为有限非负数")
    account = snapshot["account"]
    total = account.get("portfolio_value")
    if (not risk_budget.positive_number(total) or account.get("denominator_status") != "explicit"
            or account.get("denominator_requires_review") is not False):
        raise ValueError("缺少已确认的组合总资产分母，不能按风险预算定仓")
    if account.get("total_stop_risk") is None:
        raise ValueError("快照组合止损风险不完整，无法确定剩余组合预算")
    label = domain.FRAMEWORK_ALIASES.get(framework)
    if label is None:
        raise ValueError("框架须为 A/B/C/D/E/F 之一")
    stop = round(price * domain.get_stop_loss_pct(label)[1], 3)
    per_share = price - stop
    if per_share <= 0:
        raise ValueError("第二档止损价不低于拟买入价，无法定仓")
    policy = account["risk_policy"]
    if not boards.advisable(code, policy.get("untradable_boards")):
        raise ValueError(f"账户无{boards.LABELS[boards.board(code)]}交易权限（风险政策 untradable_boards），不定仓；研究不受影响")
    holding = next((h for h in snapshot["holdings"] if h["code"] == code), None)
    existing = (holding or {}).get("stop_risk") or 0
    single = total * policy["max_position_risk_pct"] / 100 - existing
    portfolio = total * policy["max_portfolio_risk_pct"] / 100 - account["total_stop_risk"]
    budget = min(single, portfolio)
    limits = {"single_stock_remaining": single, "portfolio_remaining": portfolio}
    raw = math.floor(budget / per_share) if budget > 0 else 0
    if cash is not None:
        raw = min(raw, math.floor(cash / price))
        limits["cash"] = cash
    shares = _valid_quantity(code, raw)
    binding = ("cash" if cash is not None and math.floor(cash / price) < math.floor(max(budget, 0) / per_share)
               else "single_stock_remaining" if single <= portfolio else "portfolio_remaining")
    # Cumulative target per stage; 0 means that stage is below one valid lot. Stages are never
    # shifted forward, so a "first tranche only" cap can always read tranches[0].
    tranches = [{"stage": stage, "cumulative_shares": _valid_quantity(code, math.floor(shares * fraction))}
                for stage, fraction in TRANCHES]
    research = snapshot.get("research") or {}
    row = next((r for group in ("holdings", "candidates") for r in research.get(group, [])
                if r["code"] == code), {})
    frozen = (row.get("card") or {}).get("stance_calc") or {}
    checkpoint = monitor_contract.stance_add_trigger(frozen)
    note = "仅按风险预算给出规模上限；是否买入由研究立场与门禁决定，未核验涨跌停、滑点与可成交数量"
    if checkpoint is not None:
        note += (f"；加仓检查点仅供参考，不阻止首批；动作上限为首批 tranches[0]：{tranches[0]['cumulative_shares']} 股，"
                 "不得反复以首批名义追加；目标期间披露达标且更新的有效研究确认后才加后续批次，未达标复核是否减仓")
    return {
        "version": "position-size-v1", "code": code, "price": price, "framework": label,
        "stop_loss_20": stop, "risk_per_share": per_share,
        "policy": {key: policy[key] for key in ("policy_id", "max_position_risk_pct", "max_portfolio_risk_pct")},
        "portfolio_value": total, "existing_position_stop_risk": existing,
        "limits": limits, "binding_limit": binding if shares else "budget_below_one_lot",
        "max_shares": shares, "amount": shares * price, "weight_pct": shares * price / total * 100,
        "stop_risk_pct": shares * per_share / total * 100, "tranches": tranches,
        "snapshot_as_of": snapshot.get("as_of"),
        "action_status": "not_formed", "execution_status": "not_checked",
        "note": note,
    }


def cmd_position_size(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="a-stock-cache position-size")
    configure_parser(parser)
    args = parser.parse_args(argv)
    try:
        snapshot = monitor_contract.loads_monitor_snapshot(Path(args.snapshot).read_text(encoding="utf-8"))
        result = size(snapshot, args.code, args.price, args.framework.upper(), cash=args.cash)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(f"定仓不可计算：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0
