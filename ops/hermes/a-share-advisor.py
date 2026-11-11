#!/usr/bin/env python3
"""Hermes no-agent job for a-share-research batch A (A4).

scan:   sync verified open holdings (account.current) into Research subscriptions, run one disclosure
        scan, print the notify items. Prints nothing when there is nothing to report.
        Then append new Research opportunities (policy.monitor `research_opportunity`
        prompts for researched, not-held candidates); each is pushed once until it lapses.
weekly: print the weekly digest.

Credentials come from ~/.config/a-share-advisor/runtime.env (A_SHARE_RESEARCH_URL, A_SHARE_RESEARCH_TOKEN);
A_SHARE_RESEARCH_HOST (set by the .sh wrapper) is the public host name the service requires,
never from this file. Delivery is Hermes' job; this script only writes to stdout.
"""
import json
import fcntl
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

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
                    "建仓前通过 a-share 读取生效政策，按风险预算核对仓位，再由所有者决定是否下单。")
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


class AdvisorError(ValueError):
    """Safe task failure; never turn an unavailable account into an empty portfolio."""


def tool(name: str, arguments: dict) -> dict:
    config = os.environ.get('ASHARE_HERMES_CONFIG', str(Path.home() / '.config/ashare/hermes-assistant.json'))
    try:
        result = subprocess.run(['ashare', '--config', config, 'call', name, '--json', json.dumps(arguments)],
                                capture_output=True, text=True, timeout=40, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise AdvisorError(f'{name} 客户端不可用或超时；未自动重试') from None
    if result.returncode:
        raise AdvisorError(f'{name} 未完成；请检查工具服务、凭据、账户来源与生效政策')
    try:
        response = json.loads(result.stdout)
        data = response['result']['data']
        if response.get('ok') is not True or not isinstance(data, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise AdvisorError(f'{name} 返回格式不兼容') from None
    return data


def holdings() -> list[str]:
    data = tool('account.current', {})
    codes = data.get('codes')
    if (not isinstance(codes, list) or any(not isinstance(c, str) or re.fullmatch('[0-9]{6}', c) is None for c in codes)
            or not isinstance(data.get('source'), dict) or data['source'].get('mode') not in ('mirror', 'ledger')):
        raise AdvisorError('账户代码或来源格式不兼容；未更新订阅')
    return sorted(set(codes))


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


def save_opportunity_state(state: dict) -> None:
    """Atomically retain the original request before a potentially ambiguous HTTP call."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=OPPORTUNITY_STATE.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, OPPORTUNITY_STATE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_opportunity_state() -> dict:
    value = json.loads(OPPORTUNITY_STATE.read_text()) if OPPORTUNITY_STATE.exists() else []
    # Preserve the existing notification keys when installing the replacement.
    if isinstance(value, list):
        value = dict(schema_version=1, seen=value, pending=None)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'seen', 'pending'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['seen'], list) or any(not isinstance(k, str) for k in value['seen'])):
        raise AdvisorError('机会去重记录不兼容；未覆盖原记录')
    pending = value['pending']
    if pending is not None and (not isinstance(pending, dict)
            or set(pending) != {'request_id', 'expected_activation_id'}
            or not isinstance(pending['request_id'], str) or not pending['request_id']
            or type(pending['expected_activation_id']) is not int or pending['expected_activation_id'] < 1):
        raise AdvisorError('待查政策请求记录不兼容；未覆盖原记录')
    return value


def _opportunities(public_base: str) -> str:
    state = read_opportunity_state()
    if state['pending'] is None:
        current = tool('policy.current', {})
        activation = current.get('activation') or {}
        if current.get('installed') is not True or type(activation.get('id')) is not int or activation['id'] < 1:
            raise AdvisorError('尚无与实装一致的生效政策，未采集建仓机会')
        state['pending'] = dict(request_id='hermes-advisor-' + uuid.uuid4().hex,
                                expected_activation_id=activation['id'])
        save_opportunity_state(state)
    saved = tool('policy.monitor', state['pending'])
    try:
        if (saved['mode'] != 'service_monitor_capture' or saved['request_id'] != state['pending']['request_id']
                or saved['activation_id'] != state['pending']['expected_activation_id']):
            raise ValueError
        captured = datetime.fromisoformat(saved['capture_context']['capture_finished_at'])
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        if captured.tzinfo is None or captured > now:
            raise ValueError
        if captured.astimezone(now.tzinfo).date() != now.date():
            save_opportunity_state(dict(schema_version=1, seen=state['seen'], pending=None))
            return '【建仓机会】已查回前日评估回执，未作为今日机会推送；下次检查重新采集。'
        prompts = saved['result']['review_prompts']
        if not isinstance(prompts, list):
            raise ValueError
        prompts = [p for p in prompts if p['kind'] == 'research_opportunity']
        text = render_opportunities(prompts, set(state['seen']), public_base)
        keys = sorted(opportunity_key(p) for p in prompts)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise AdvisorError('政策回执不兼容；保留原请求供核对，不清空去重记录') from None
    # Keep only current keys so a lapsed opportunity can be notified again.
    save_opportunity_state(dict(schema_version=1, seen=keys, pending=None))
    return text


def opportunities(public_base: str = "") -> str:
    OPPORTUNITY_STATE.parent.mkdir(parents=True, exist_ok=True)
    with OPPORTUNITY_STATE.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return '【建仓机会】已有检查在运行，本次未重复采集。'
        try:
            return _opportunities(public_base)
        except (AdvisorError, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, AdvisorError) else '去重状态读写失败；保留原记录，请核对后重试'
            return f'【建仓机会】检查未完成：{message}'


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
    except (AdvisorError, urllib.error.URLError, subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, OSError) as exc:
        text = f"【研究扫描】任务未能完成：{type(exc).__name__}: {exc}"
    if text:
        print(text)


if __name__ == "__main__":
    main()
