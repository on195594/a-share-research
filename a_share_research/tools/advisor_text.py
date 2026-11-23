"""Render scheduled advisor receipts for delivery by Hermes."""
from collections import Counter
from datetime import datetime

from a_share_research.db import SHANGHAI


REASONS = {"major_risk": "发现风险公告", "pending_review": "有新信息待核实", "review_failed": "自动复核未完成",
           "judgment_changed": "研究更新", "current_zone_changed": "估值位置变化",
           "checkpoint_deviated": "披露偏离预测区间", "add_trigger_missed": "加仓条件未达到"}
ZONES = {"测算支持区": "模型认为估值较有吸引力", "中性区": "模型认为估值居中",
         "测算透支区": "模型认为价格偏贵"}
VALUATION_NOTE = "注：估值参考不是技术支撑位或买卖指令，也不预测股价一定涨跌。"
ACTIONS = {"covered": "已有报告", "queued_review": "已安排研究复核", "deferred": "等待研究复核",
           "no_card": "暂无研究记录", "skipped": "跳过"}
OPPORTUNITY_NOTE = ("注：机会来自研究立场与按情景概率加权的期望回报，情景尚未记分；"
                    "建仓前通过 a-share 读取生效政策，按风险预算核对仓位，再由所有者决定是否下单。")


def pct(value) -> str:
    return "—" if value is None else f"{value * 100:+.1f}%"


def card_line(card: dict | None, *, summary: bool = True) -> str:
    if not card:
        return "暂无可用研究，不能据此判断估值"
    current = card.get("current") or {}
    pending = card.get("status") != "有效"
    parts = []
    if summary:
        parts.append("有新信息待核实，旧估值区间暂停使用" if pending else
                     f"研究结论：{card.get('verdict') or '未提供'}")
    if current.get("last_close") is not None:
        parts.append(f"{current.get('last_close_date')}收盘{current['last_close']:.2f}元")
    if not pending:
        if summary:
            parts.append(ZONES.get(current.get("current_zone") or "", "暂不能判断当前估值位置"))
        # Pending cards must never resurrect the report-date thresholds.
        rolling = current.get("valuation") or {}
        calc = card.get("valuation_calc") or {}
        z = rolling if rolling.get("support_below") is not None else calc.get("zones") or {}
        if z.get("support_below") is not None and z.get("stretched_above") is not None:
            parts.append(f"估值参考：较有吸引力≤{z['support_below']:.2f}元，偏贵≥{z['stretched_above']:.2f}元")
    if summary and current.get("note"):
        parts.append(current["note"])
    failure = card.get("check_failure")
    if failure:
        parts.append(f"披露机械核对未完成：{failure['reason']}"
                     f"（报告 #{failure['report_id']}，扫描 #{failure['scan_id']}）")
    return "；".join(parts)


def change_lines(item: dict) -> list[str]:
    change = item.get("change")
    if item["reason"] == "judgment_changed" and not change:
        return ["变化明细未提供，暂不能判断具体改变了什么；请核对原报告。"]
    if item["reason"] == "judgment_changed" and change:
        before, after = change["before"], change["after"]
        lines = []
        if before.get("verdict") == after.get("verdict"):
            lines.append(f"研究结论未变：仍为“{after.get('verdict') or '未提供'}”。")
        else:
            lines.append(f"研究结论：{before.get('verdict') or '上次未提供'} → {after.get('verdict') or '本次未提供'}。")
        if before.get("zone") != after.get("zone"):
            old = ZONES.get(before.get("zone") or "", before.get("zone") or "上次未提供")
            new = ZONES.get(after.get("zone") or "", after.get("zone") or "本次未提供")
            lines.append(f"估值判断：{old} → {new}。")
            if before.get("zone") is None:
                lines.append("这是补充估值判断，不代表公司基本面突然变差。")
        return lines
    if item["reason"] == "current_zone_changed":
        old = ZONES.get(item.get("from_zone") or "", item.get("from_zone") or "未知")
        new = ZONES.get(item.get("to_zone") or "", item.get("to_zone") or "未知")
        return [f"估值位置：{old} → {new}。"]
    if item["reason"] == "pending_review":
        return ["发生了什么：发现新信息，之前的研究需要重新核对。"]
    return [f"发生了什么：{item.get('detail') or REASONS.get(item['reason'], item['reason'])}"]


def notice_text(item: dict, held_codes: set[str] | None, public_base: str = "") -> str:
    reason, card = item["reason"], item.get("card")
    pending = bool(card and card.get("status") != "有效") or reason == "pending_review"
    zone = ((card or {}).get("current") or {}).get("current_zone")
    if reason == "major_risk":
        conclusion = "发现风险公告，需要优先核实影响；不等于风险已确认或必须立即卖出。"
    elif reason == "review_failed":
        conclusion = "自动复核没有完成，暂不能确认新信息的影响；失败不代表风险消失。"
    elif reason == "checkpoint_deviated":
        conclusion = "披露值偏离原预测区间，需要复核判断及偏离原因。"
    elif reason == "add_trigger_missed":
        conclusion = "披露值未达到原加仓条件，需要重新核对持仓判断。"
    elif pending:
        conclusion = "新信息尚未核实，旧估值区间暂不能作为决策依据。"
    else:
        conclusion = ZONES.get(zone or "", "研究已更新，当前估值位置尚不明确") + "；这不是买卖指令。"
    # Subscription/L5 membership is not evidence of an account position.
    held = None if held_codes is None else item["code"] in held_codes
    if held is None:
        action = "持仓状态未知：先核对账户与上述信息，不据此确认空仓、加仓或执行买卖。"
    elif held and reason == "add_trigger_missed":
        action = "你仍持仓：加仓条件未达到，复核是否减仓。"
    elif held:
        action = "你仍持仓：优先复核上述风险及既有退出条件。" if reason == "major_risk" else (
            "你仍持仓：先核对新信息，不据此加仓。" if pending or reason == "review_failed" else
            "你仍持仓：先看这次变化，不因本提醒立即买卖。")
    else:
        action = "当前未持仓：先核实风险，不据此新开仓。" if reason == "major_risk" else (
            "当前未持仓：无需处理持仓；信息未核实前不据此买入。" if pending or reason == "review_failed" else
            "当前未持仓：无需处理持仓，也不因本提醒直接买入。")
    lines = [f"【{REASONS.get(reason, reason)}】{item.get('name') or ''}（{item['code']}）",
             f"一句话：{conclusion}", *change_lines(item)]
    prices = card_line(card, summary=False)
    if prices:
        lines.append(prices)
    lines.append(f"对你：{action}")
    if card and card.get("report_id") is not None:
        lines.append(f"依据：研究报告#{card['report_id']}")
    if public_base and item.get("path"):
        lines.append(f"查看研究：{public_base.rstrip('/')}{item['path']}")
    return "\n".join(lines)


def render_scan(result: dict, held_codes: set[str] | None, public_base: str = "") -> str:
    scan_id = result.get("scan_id", result.get("id"))
    lines = []
    if result["status"] == "failed":
        lines.append(f"【研究扫描】扫描 #{scan_id} 失败：{result.get('error')}")
    failures = result["summary"].get("failures", [])
    if failures:
        lines.append("【研究扫描】部分代码未扫描成功（不等于无事件）：" + "；".join(
            f"{f['code'] or '账户范围'} {f.get('stage')}：{f.get('reason')}" for f in failures))
    checks = result["summary"].get("check_failures", [])
    if checks:
        lines.append("【研究扫描】披露核对未完成（机械检查；不等于披露符合预期；下次扫描自动重试）：" + "；".join(
            f"{f['code']} {f.get('stage', 'disclosure_checks')}：{f.get('reason')}"
            f"（报告 #{f.get('report_id') or 'pending'}，扫描 #{scan_id}）" for f in checks))
    gaps = result["summary"].get("coverage_gaps", [])
    if gaps:
        lines.append("【研究扫描】距上次成功扫描超过 30 日，以下区间的公告未覆盖，不能视为连续覆盖：" + "；".join(
            f"{g['code']} {g['uncovered_from']} 至 {g['uncovered_to']}" for g in gaps))
    notices = result.get("notify", [])
    lines.extend(notice_text(item, held_codes, public_base) for item in notices)
    queued = result["summary"].get("queued_runs", [])
    if lines and queued:
        lines.append(f"已自动提交 {len(queued)} 家接续复核，完成后的变化会在下次扫描推送。")
    if notices:
        lines.append(VALUATION_NOTE)
    return "\n\n".join(lines)


def render_opportunities(prompts: list[dict], public_base: str = "") -> str:
    if not prompts:
        return ""
    lines = []
    for prompt in prompts:
        e = prompt["evidence"]
        line = f"【建仓机会】{prompt['detail']}"
        if e.get("report_id") is not None:
            line += f"\n依据：研究报告#{e['report_id']}（{e.get('close_date')}收盘{e.get('close')}元）"
        if public_base:
            line += f"\n查看研究：{public_base.rstrip('/')}/stock/{prompt['code']}"
        lines.append(line)
    return "\n\n".join([*lines, OPPORTUNITY_NOTE])


def render_weekly(data: dict, opportunity_costs: list[dict] | None = None) -> str:
    states = {"有效": "可参考", "待复核": "有新信息待核实"}
    lines = [f"【研究周报】截至 {data['as_of'][:16]}",
             "研究状态：" + "、".join(f"{states.get(k, k)} {v}家" for k, v in sorted(data["status_distribution"].items())),
             "估值分布：" + "、".join(f"{ZONES.get(k, k)} {v}家" for k, v in sorted(data["zone_distribution"].items()))]
    for item in data["subscriptions"]:
        lines.append(f"- {(item.get('card') or {}).get('name') or ''}({item['code']})：{card_line(item.get('card'))}")
    # Older services have no scan_scope; the line is simply absent then.
    extra = {s["code"] for s in data.get("scan_scope", [])} - {i["code"] for i in data["subscriptions"]}
    if extra:
        lines.append(f"披露扫描另覆盖 {len(extra)} 家（周报未逐家列出）")
    if data["events"]:
        lines.append(f"本周发现披露 {len(data['events'])} 条：" + "；".join(
            f"{e['code']} {e['title']}（{ACTIONS.get(e['action'], e['action'])}）" for e in data["events"][:10]))
    if data["backlog"]:
        lines.append(f"待复核积压 {len(data['backlog'])} 条")
    last = data.get("last_scan")
    lines.append(f"最后成功扫描：{last['started_at'][:16] if last else '无记录'}")
    gaps = data.get("coverage_gaps", [])
    if gaps:
        lines.append("未覆盖区间（超过 30 日回看，不回补，不能视为连续覆盖）：" + "；".join(
            f"{g['code']} {g['uncovered_from']} 至 {g['uncovered_to']}" for g in gaps))
    open_events = data.get("open_events", [])
    if open_events:
        lines.append(f"待核对公告 {len(open_events)} 条（已纳入研究不等于风险排除）：")
        for e in open_events[:10]:
            review = e.get("review") or {}
            exit_hint = (f"重试任务 #{e['run_id']}" if review.get("state") == "research_failed"
                         else "在公司页发起接续研究或人工复核")
            lines.append(f"- {e['code']} {e['announced']} {e['title']}：{review.get('label', '待核对')}；{exit_hint}")
    if data["failures"] or data["review_failures"]:
        lines.append(f"扫描失败 {len(data['failures'])} 次，复核失败 {len(data['review_failures'])} 次")
        stages = Counter(f.get("stage", "scan") for f in data["failures"])
        if stages.get("disclosure_checks"):
            codes = "、".join(dict.fromkeys(f["code"] for f in data["failures"] if f.get("stage") == "disclosure_checks"))
            lines.append(f"其中披露核对失败 {stages['disclosure_checks']} 次（{codes}），下次扫描自动重试")
            lines.extend(f"机械检查：{f['code']} · 报告 #{f.get('report_id') or 'pending'} · 扫描 #{f['scan_id']}：{f['reason']}"
                         for f in data["failures"] if f.get("stage") == "disclosure_checks")
    lines.append(f"后续观察项 {data['checkpoint_count']} 条（含预测 {data['forecast_count']} 条）")
    if opportunity_costs:
        lines.append(f"换仓机会成本复核 {len(opportunity_costs)} 项：")
        for prompt in opportunity_costs:
            lines.append(f"- {prompt['detail']}")
    lines.append(VALUATION_NOTE)
    return "\n".join(lines)


def scan_text(result, public_base, today):
    codes = result['held_codes']
    scan = result['scan']
    text = render_scan(scan, set(codes) if codes is not None else None, public_base)
    if text:
        requested = datetime.fromisoformat(scan['requested_at']).astimezone(SHANGHAI)
        if requested.date() != today:
            text = f'【研究扫描补回】原扫描时间 {requested.isoformat()}；以下为历史回执，不代表今日观察。\n\n' + text
    opportunities = render_opportunities(result['opportunities'], public_base)
    holding_note = f"【持仓核对】{result['holding_note']}" if result['holding_note'] else ''
    return '\n\n'.join(part for part in (text, holding_note, opportunities) if part)
