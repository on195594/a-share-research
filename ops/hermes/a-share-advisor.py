#!/usr/bin/env python3
"""Hermes no-agent job for a-share-research batch A (A4).

scan:   sync open holdings (a-stock-cache, read-only) into Research subscriptions, run one disclosure
        scan, print the notify items. Prints nothing when there is nothing to report.
        Then append new Research opportunities (Skills monitor-snapshot `research_opportunity`
        prompts for researched, not-held candidates); each is pushed once until it lapses.
weekly: print the weekly digest.

Credentials come from ~/.config/a-share-advisor/runtime.env (A_SHARE_RESEARCH_URL, A_SHARE_RESEARCH_TOKEN);
A_SHARE_RESEARCH_HOST (set by the .sh wrapper) is the public host name the service requires,
never from this file. Delivery is Hermes' job; this script only writes to stdout.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(os.environ.get("A_SHARE_ADVISOR_ENV", Path.home() / ".config/a-share-advisor/runtime.env"))
REASONS = {"major_risk": "发现风险公告", "pending_review": "有新信息待核实", "review_failed": "自动复核未完成",
           "judgment_changed": "研究更新", "current_zone_changed": "估值位置变化"}
ZONES = {"测算支持区": "模型认为估值较有吸引力", "中性区": "模型认为估值居中",
         "测算透支区": "模型认为价格偏贵"}
VALUATION_NOTE = "注：估值参考不是技术支撑位或买卖指令，也不预测股价一定涨跌。"
ACTIONS = {"covered": "已纳入研究", "queued_review": "已安排研究复核", "deferred": "等待研究复核",
           "no_card": "暂无研究记录", "skipped": "跳过"}
OPPORTUNITY_STATE = Path(os.environ.get("A_SHARE_ADVISOR_STATE", Path.home() / ".local/state/a-share-advisor")) / "opportunities.json"
OPPORTUNITY_NOTE = ("注：机会来自研究立场与按情景概率加权的期望回报，情景尚未记分；"
                    "建仓前在 Claude 用 a-stock-research 按风险预算定仓（position-size），确认后才下单。")
POLL_SECONDS, POLL_LIMIT = 20, 90  # a scan of ~10 codes takes minutes; give up after 30 minutes


def settings() -> tuple[str, str, str | None]:
    values = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    values.update({k: v for k, v in os.environ.items() if k.startswith("A_SHARE_RESEARCH_")})
    # Optional public Host header: the service only accepts its own host name, even on 127.0.0.1.
    return values["A_SHARE_RESEARCH_URL"].rstrip("/"), values["A_SHARE_RESEARCH_TOKEN"], values.get("A_SHARE_RESEARCH_HOST")


def call(method: str, path: str, body: dict | None = None) -> dict:
    base, token, host = settings()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    request = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def holdings() -> list[str]:
    out = subprocess.run(["a-stock-cache", "holdings", "--json", "--active-only"],
                         check=True, capture_output=True, text=True).stdout
    return sorted({row["code"][:6] for row in json.loads(out)})


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


def notice_text(item: dict, held_codes: set[str], public_base: str = "") -> str:
    reason, card = item["reason"], item.get("card")
    pending = bool(card and card.get("status") != "有效") or reason == "pending_review"
    zone = ((card or {}).get("current") or {}).get("current_zone")
    if reason == "major_risk":
        conclusion = "发现风险公告，需要优先核实影响；不等于风险已确认或必须立即卖出。"
    elif reason == "review_failed":
        conclusion = "自动复核没有完成，暂不能确认新信息的影响；失败不代表风险消失。"
    elif pending:
        conclusion = "新信息尚未核实，旧估值区间暂不能作为决策依据。"
    else:
        conclusion = ZONES.get(zone or "", "研究已更新，当前估值位置尚不明确") + "；这不是买卖指令。"
    # Subscription/L5 membership is not evidence of an account position.
    held = item["code"] in held_codes
    if held:
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


def render_scan(result: dict, held_codes: set[str], public_base: str = "") -> str:
    lines = []
    if result["status"] == "failed":
        lines.append(f"【研究扫描】扫描 #{result['scan_id']} 失败：{result.get('error')}")
    failures = result["summary"].get("failures", [])
    if failures:
        lines.append("【研究扫描】部分代码未扫描成功（不等于无事件）：" + "；".join(
            f"{f['code']} {f.get('stage')}：{f.get('reason')}" for f in failures))
    notices = result.get("notify", [])
    lines.extend(notice_text(item, held_codes, public_base) for item in notices)
    queued = result["summary"].get("queued_runs", [])
    if lines and queued:
        lines.append(f"已自动提交 {len(queued)} 家接续复核，完成后的变化会在下次扫描推送。")
    if notices:
        lines.append(VALUATION_NOTE)
    return "\n\n".join(lines)


def scan() -> str:
    call("PUT", "/api/subscriptions", {"codes": holdings()})
    scan_id = call("POST", "/api/scans")["scan_id"]
    for _ in range(POLL_LIMIT):
        result = call("GET", f"/api/scans/{scan_id}")
        if result["status"] in ("done", "failed"):
            break
        time.sleep(POLL_SECONDS)
    else:
        return f"【研究扫描】扫描 #{scan_id} 30 分钟未完成，请查看 Research 服务。"
    # Refresh at delivery time: a position may have been closed while the scan ran.
    held_codes = set(holdings()) if result.get("notify") else set()
    _, _, host = settings()
    public_base = f"https://{host}" if host else ""
    text = render_scan(result | {"scan_id": scan_id}, held_codes, public_base)
    extra = opportunities(public_base)
    return "\n\n".join(part for part in (text, extra) if part)


def opportunity_key(prompt: dict) -> str:
    e = prompt["evidence"]
    # A new report, stance or zone makes it a new opportunity; a price wiggle inside one does not.
    return f"{prompt['code']}|{e.get('report_id')}|{e.get('stance_action')}|{e.get('v2_zone')}"


def render_opportunities(prompts: list[dict], seen: set[str], public_base: str = "") -> str:
    fresh = [p for p in prompts if opportunity_key(p) not in seen]
    if not fresh:
        return ""
    lines = []
    for prompt in fresh:
        e = prompt["evidence"]
        line = f"【建仓机会】{prompt['detail']}"
        if e.get("report_id") is not None:
            line += f"\n依据：研究报告#{e['report_id']}（{e.get('close_date')}收盘{e.get('close')}元）"
        if public_base:
            line += f"\n查看研究：{public_base.rstrip('/')}/stock/{prompt['code']}"
        lines.append(line)
    return "\n\n".join([*lines, OPPORTUNITY_NOTE])


def opportunities(public_base: str = "") -> str:
    done = subprocess.run(["a-stock-cache", "monitor-snapshot", "--json"], capture_output=True, text=True, timeout=300)
    try:
        prompts = [p for p in json.loads(done.stdout).get("review_prompts", []) if p["kind"] == "research_opportunity"]
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
        return f"【建仓机会】检查未完成：monitor-snapshot 退出码 {done.returncode}，输出不可解析。"
    seen = set(json.loads(OPPORTUNITY_STATE.read_text())) if OPPORTUNITY_STATE.exists() else set()
    text = render_opportunities(prompts, seen, public_base)
    # Keep only current keys so an opportunity that lapses and returns is pushed again.
    OPPORTUNITY_STATE.parent.mkdir(parents=True, exist_ok=True)
    OPPORTUNITY_STATE.write_text(json.dumps(sorted(opportunity_key(p) for p in prompts)))
    return text


def render_weekly(data: dict) -> str:
    states = {"有效": "可参考", "待复核": "有新信息待核实"}
    lines = [f"【研究周报】截至 {data['as_of'][:16]}",
             "研究状态：" + "、".join(f"{states.get(k, k)} {v}家" for k, v in data["status_distribution"].items()),
             "估值分布：" + "、".join(f"{ZONES.get(k, k)} {v}家" for k, v in data["zone_distribution"].items())]
    for item in data["subscriptions"]:
        lines.append(f"- {(item.get('card') or {}).get('name') or ''}({item['code']})：{card_line(item.get('card'))}")
    if data["events"]:
        lines.append(f"本周发现披露 {len(data['events'])} 条：" + "；".join(
            f"{e['code']} {e['title']}（{ACTIONS.get(e['action'], e['action'])}）" for e in data["events"][:10]))
    if data["backlog"]:
        lines.append(f"待复核积压 {len(data['backlog'])} 条")
    if data["failures"] or data["review_failures"]:
        lines.append(f"扫描失败 {len(data['failures'])} 次，复核失败 {len(data['review_failures'])} 次")
    lines.append(f"后续观察项 {data['checkpoint_count']} 条（含预测 {data['forecast_count']} 条）")
    lines.append(VALUATION_NOTE)
    return "\n".join(lines)


def weekly() -> str:
    return render_weekly(call("GET", "/api/scans/weekly"))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode not in ("scan", "weekly"):
        # Any other argument (e.g. --help) must not trigger a real scan.
        sys.exit("usage: a-share-advisor.py [scan|weekly]")
    try:
        text = weekly() if mode == "weekly" else scan()
    except (urllib.error.URLError, subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, OSError) as exc:
        text = f"【研究扫描】任务未能完成：{type(exc).__name__}: {exc}"
    if text:
        print(text)


if __name__ == "__main__":
    main()
