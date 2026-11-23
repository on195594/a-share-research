"""Monthly batch admission and notification text for the scheduled delivery ledger."""
from datetime import datetime, timedelta

from a_share_research import db, service


LABELS = {"queued": "排队中", "screening": "初筛中", "researching": "研究中", "selecting": "比较中",
          "proposed": "待确认", "frozen": "已冻结", "tracking": "跟踪中", "failed": "失败"}


def _describe(batch):
    status = batch['status']
    return f"批次 #{batch['id']}（{LABELS.get(status, status)}）"


def _event_for(batch, now):
    status = batch['status']
    if status == 'failed':
        return 'failed'
    if status == 'proposed':
        return 'needs_owner'
    if status in ('frozen', 'tracking'):
        gaps = batch['failed_research'] or batch['ranking_error'] or batch['error']
        return 'completed_with_gaps' if gaps else 'completed'
    if now - datetime.fromisoformat(batch['created_at']) > timedelta(hours=48):
        return 'stuck'
    return None


def _message(month, event, batch):
    head = f"【月批】{month} {_describe(batch)}"
    if event == "failed":
        lines = [f"{head}失败：{batch['error'] or '未记录原因'}。自动月批不自动重跑，需你决定是否在网页手动重开。"]
    elif event == "needs_owner":
        lines = [f"{head}自动冻结未完成，需要你在批次页确认入选。"]
    elif event == "stuck":
        lines = [f"{head}创建超过 48 小时仍未完成，请查看 Research 服务。"]
    else:
        lines = [f"{head}已完成，冻结于 {(batch['frozen_at'] or '')[:16]}，基准日 {batch['as_of'] or '—'}。"]
        if batch['failed_research']:
            failed = batch['failed_research']
            lines.append(f"研究失败 {len(failed)} 家：" + "；".join(f"{r['code']} {r['error'] or ''}" for r in failed[:10]))
        if batch['ranking_error']:
            lines.append(f"比较排序异常：{batch['ranking_error']}")
        if batch['error']:
            lines.append(f"批次备注：{batch['error']}")
    lines.append("详情见批次页；入选与账户动作仍由你决定。")
    return "\n".join(lines) + "\n"


def run(conn, job, slot, previous):
    month = slot[:7]
    submit = job == 'monthly_submit'
    receipt = service.monthly_batch(conn, month, submit=submit)
    batch = receipt['batch']
    if submit:
        label = _describe(batch)
        if receipt['created']:
            return (f"【月批】已发起 {month} 月度候选{label}；初筛+研究通常需数小时，完成或失败时另行通知。\n", {})
        return f"【月批】{month} 已有{label}，按规则不再新建（失败批次也不自动重开）。\n", {}
    if batch is None:
        return '', previous
    event = _event_for(batch, datetime.fromisoformat(db.now()))
    if event is None:
        return '', previous
    key = f"{batch['id']}:{event}"
    seen = previous.get('seen', [])
    if key in seen:
        return '', previous
    return _message(month, event, batch), {'seen': [*seen, key]}
