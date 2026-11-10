#!/usr/bin/env python3
"""Hermes no-agent job: monthly candidate batch for a-share-research.

submit: POST /api/batches/monthly for the current Shanghai month (server rejects other months and
        never creates a second batch for a month, including after failure). Prints one confirmation.
status: GET /api/batches/monthly/{month}; prints once when the batch reaches a terminal or owner-action
        state, or once when it is still unfinished 48 hours after creation. Silent otherwise.

Reuses the advisor credentials (~/.config/a-share-advisor/runtime.env) and Host header handling from
a-share-advisor.py. A per-month receipt under ~/.local/state/a-share-monthly/ only suppresses duplicate
messages; admission idempotency belongs to the server. Delivery is Hermes' job; this script only writes stdout.
"""
import fcntl
import os
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module

advisor = import_module("a-share-advisor")  # shares settings()/call(); file name has a hyphen

SHANGHAI = timezone(timedelta(hours=8))
STATE = Path(os.environ.get("A_SHARE_MONTHLY_STATE", Path.home() / ".local/state/a-share-monthly"))
STUCK_AFTER = timedelta(hours=48)
LABELS = {"queued": "排队中", "screening": "初筛中", "researching": "研究中", "selecting": "比较中",
          "proposed": "待确认", "frozen": "已冻结", "tracking": "跟踪中", "failed": "失败"}


def current_month() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m")


def describe(batch: dict) -> str:
    return f"批次 #{batch['id']}（{LABELS.get(batch['status'], batch['status'])}）"


def submit(month: str) -> str:
    data = advisor.call("POST", "/api/batches/monthly", {"month": month})
    batch = data["batch"]
    if data["created"]:
        return (f"【月批】已发起 {month} 月度候选{describe(batch)}；初筛+研究通常需数小时，"
                "完成或失败时另行通知。")
    return f"【月批】{month} 已有{describe(batch)}，按规则不再新建（失败批次也不自动重开）。"


def event_for(batch: dict, now: datetime) -> str | None:
    status = batch["status"]
    if status == "failed":
        return "failed"
    if status == "proposed":
        return "needs_owner"
    if status in ("frozen", "tracking"):
        gaps = batch.get("failed_research") or batch.get("ranking_error") or batch.get("error")
        return "completed_with_gaps" if gaps else "completed"
    if now - datetime.fromisoformat(batch["created_at"]) > STUCK_AFTER:
        return "stuck"
    return None  # queued/in progress: nothing to report yet


def message(month: str, event: str, batch: dict) -> str:
    head = f"【月批】{month} {describe(batch)}"
    if event == "failed":
        lines = [f"{head}失败：{batch.get('error') or '未记录原因'}。自动月批不自动重跑，需你决定是否在网页手动重开。"]
    elif event == "needs_owner":
        lines = [f"{head}自动冻结未完成，需要你在批次页确认入选。"]
    elif event == "stuck":
        lines = [f"{head}创建超过 48 小时仍未完成，请查看 Research 服务。"]
    else:
        lines = [f"{head}已完成，冻结于 {(batch.get('frozen_at') or '')[:16]}，基准日 {batch.get('as_of') or '—'}。"]
        if event == "completed_with_gaps":
            failed = batch.get("failed_research") or []
            if failed:
                lines.append(f"研究失败 {len(failed)} 家：" + "；".join(f"{r['code']} {r.get('error') or ''}" for r in failed[:10]))
            if batch.get("ranking_error"):
                lines.append(f"比较排序异常：{batch['ranking_error']}")
            if batch.get("error"):
                lines.append(f"批次备注：{batch['error']}")
    lines.append("详情见批次页；入选与账户动作仍由你决定。")
    return "\n".join(lines)


def status(month: str) -> str:
    STATE.mkdir(parents=True, exist_ok=True)
    receipt_path = STATE / f"{month}.receipt"
    with receipt_path.open("a+", encoding="utf-8") as receipt:
        os.chmod(receipt_path, 0o600)
        fcntl.flock(receipt, fcntl.LOCK_EX)
        receipt.seek(0)
        sent = set(receipt.read().split())
        batch = advisor.call("GET", f"/api/batches/monthly/{month}")["batch"]
        if not batch:
            return ""  # before submission (or submission failed and was already reported by submit)
        event = event_for(batch, datetime.now(SHANGHAI))
        key = f"{batch['id']}:{event}"
        if event is None or key in sent:
            return ""
        receipt.write(key + "\n")
        receipt.flush()
        os.fsync(receipt.fileno())
        return message(month, event, batch)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    month = sys.argv[2] if len(sys.argv) > 2 else current_month()
    try:
        text = submit(month) if mode == "submit" else status(month)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        text = f"【月批】{mode} {month} 请求被拒绝：HTTP {exc.code} {detail}"
    except (urllib.error.URLError, KeyError, OSError, ValueError) as exc:
        text = f"【月批】{mode} {month} 任务未能完成：{type(exc).__name__}: {exc}"
    if text:
        print(text)


if __name__ == "__main__":
    main()
