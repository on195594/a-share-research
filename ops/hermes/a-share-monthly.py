#!/usr/bin/env python3
"""Hermes no-agent job: monthly candidate batch for a-share-research.

Admission is idempotent per Shanghai month. Status notifications are delivered once per
batch event, including a batch unfinished after 48 hours. Hermes delivers stdout.
"""
import fcntl
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_tools import ToolsError, tool

SHANGHAI = timezone(timedelta(hours=8))
STATE = Path(os.environ.get("A_SHARE_MONTHLY_STATE", Path.home() / ".local/state/a-share-monthly"))
STUCK_AFTER = timedelta(hours=48)
LABELS = {"queued": "排队中", "screening": "初筛中", "researching": "研究中", "selecting": "比较中",
          "proposed": "待确认", "frozen": "已冻结", "tracking": "跟踪中", "failed": "失败"}


def current_month() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m")


def describe(batch_id: int, status: str) -> str:
    return f"批次 #{batch_id}（{LABELS.get(status, status)}）"


def submit(month: str) -> str:
    data = tool("research_batch_monthly_request", {"month": month})
    label = describe(data["batch_id"], data["status"])
    if data["created"]:
        return (f"【月批】已发起 {month} 月度候选{label}；初筛+研究通常需数小时，"
                "完成或失败时另行通知。")
    return f"【月批】{month} 已有{label}，按规则不再新建（失败批次也不自动重开）。"


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
    head = f"【月批】{month} {describe(batch['id'], batch['status'])}"
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
        batch = tool("research_batch_monthly_status", {"month": month, "compact": False})["batch"]
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
    action = "受理" if mode == "submit" else "状态查询"
    try:
        text = submit(month) if mode == "submit" else status(month)
    except ToolsError as exc:
        if mode == "submit" and exc.code == "outcome_unknown":
            text = f"【月批】结果未知，请先查询 {month} 的批次状态"
        else:
            text = f"【月批】{month} {action}未完成。{exc}"
    except (KeyError, OSError, TypeError, ValueError):
        text = f"【月批】{month} {action}未完成，请核对该月回执和本地通知记录。"
    if text:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
