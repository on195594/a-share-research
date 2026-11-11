"""Pure legacy stop/governance checks; acquisition and persistence belong to callers."""
import json

from a_share_research.policy.v0 import domain, risk_gates


def calculate(holdings, defers, quotes, fundamentals, market_snapshot, now) -> dict:
    lines = []
    def emit(value=''):
        lines.append(value)
    if not holdings:
        return dict(status="no_holdings", exit_code=0, stdout="暂无持仓\n", stderr="", checks=[])

    today_str = now.strftime("%Y-%m-%d")

    emit(f"\n{'─' * 72}")
    emit(f"  持仓止损检查  现价查询时间: {now.strftime('%Y-%m-%d %H:%M')}")
    emit(f"{'─' * 72}")

    existing_defers = {}
    for holding_id, evidence, review_due in defers:
        try:
            parsed = json.loads(evidence) if evidence else {}
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, dict):
            existing_defers[holding_id] = {
                key: value
                for key, value in {
                    "defer_started_at": parsed.get("defer_started_at"),
                    "review_due": parsed.get("review_due") or review_due,
                    "original_price": parsed.get("original_price"),
                    "original_stop_loss_20": parsed.get("original_stop_loss_20"),
                }.items()
                if value is not None
            }

    checks = []
    alerts = []
    governance_alerts = []
    for holding_id, code, name, _cost, sl15, sl20 in holdings:
        label, status, is_alert = domain.evaluate_holding_status(
            code, name, sl15, sl20, quotes.get(code), today_str, now
        )
        fundamental = fundamentals.get(code)
        gate_values = (
            [
                fundamental.get(name)
                for name in ("regulatory_gate", "roe_structural_gate", "cash_flow_gate")
            ]
            if fundamental
            else []
        )
        failed_gates = [
            gate
            for gate in gate_values
            if isinstance(gate, dict)
            and gate.get("status") not in {"clear", "not_applicable"}
        ]
        if failed_gates:
            reasons = ", ".join(
                str(gate.get("reason_code", "incomplete")) for gate in failed_gates
            )
            governance_alerts.append((label, reasons))
            status += f" | 🔴 风险门冻结加仓({reasons})"
        quote = quotes.get(code)
        existing_defer = existing_defers.get(holding_id)
        p3 = risk_gates.liquidity_shock_gate(
            {
                "stop_loss_triggered": bool(
                    existing_defer or is_alert and quote and quote.price <= sl20
                ),
                "stop_loss_20": sl20,
                "quote": {
                    "price": quote.price,
                    "source": quote.source,
                    "suspended": quote.suspended,
                    "limit_down_locked": quote.limit_down_locked,
                    "trading_status": quote.trading_status,
                }
                if quote
                else {},
                "market_snapshot": market_snapshot,
                "existing_defer": existing_defer,
                "now": now,
            }
        )
        if p3["status"] in {"deferred", "untradeable", "incomplete"}:
            status = status.replace("建议立即止损", "暂不可执行")
            status += f" | P3:{p3['status']}({p3['reason_code']})"
            is_alert = True
        if is_alert:
            alerts.append((label, status))
        checks.append(dict(holding_id=holding_id, code=code, label=label, status=status, is_alert=is_alert,
                           p3=p3, governance_gates=failed_gates))
        emit(f"  {label:<16} {status}")

    emit()
    if alerts:
        emit(f"  共 {len(alerts)} 项预警，请及时处理：")
        for label, status in alerts:
            emit(f"    {label}: {status}")
    if governance_alerts:
        emit(f"  共 {len(governance_alerts)} 项风险门复核，已冻结新增风险敞口：")
        for label, reasons in governance_alerts:
            emit(f"    {label}: {reasons}（不自动卖出/清仓）")
    if not alerts and not governance_alerts:
        emit("  无预警，所有持仓价格在止损线之上")
    emit()
    return dict(status="ready", exit_code=0, stdout="\n".join(lines) + "\n", stderr="", checks=checks)
