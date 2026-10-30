"""Shared research submission and public evidence views; no model or account actions."""

import json
import re
import sqlite3
from datetime import datetime

from a_share_research import batches, db
from a_share_research.evidence import report_source_view, projection
from a_share_research.reviewing import legacy_issue_view, audit_view, issue_view

VERSION = "research-v1"
MAX_CALLS = 21  # Loop guard, not a cost limit; shared with the worker.


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def submit(conn, code: str, request_id: str, *, name: str | None = None,
           question: str = "", parent_report_id: int | None = None, feedback: dict | None = None) -> int:
    valid_code = code[:6] if re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", code) else ""
    if (not valid_code and (code or parent_report_id is None)) or not 1 <= len(request_id) <= 128 or len(question) > 4000:
        raise RequestError("需要 6 位公司代码、1–128 字请求编号；问题不超过 4000 字")
    code = valid_code
    question = question.strip()
    if parent_report_id is not None:
        parent = conn.execute(
            "SELECT r.code, r.name, r.passed, r.body, u.status FROM reports r"
            " JOIN runs u ON u.id = r.run_id WHERE r.id = ?", (parent_report_id,),
        ).fetchone()
        if parent is None:
            raise RequestError("原报告不存在", 404)
        if not ready(parent["status"], parent["passed"]):
            raise RequestError("报告尚未就绪或状态不一致", 409)
        code = code or parent["code"][:6]
        name = name or parent["name"]
        if parent["code"][:6] != code:
            raise RequestError("原报告与研究公司不一致")
        if feedback is not None:
            location, text = feedback.get("location", ""), feedback.get("text", "").strip()
            body = json.loads(parent["body"])
            content = body if "summary" in body else body.get("facts", {})
            match = re.fullmatch(r"(answer|risks|business|financial|methods|checkpoints)\[(\d{1,4})\]", location)
            valid = location == "summary" and bool(content.get("summary"))
            if match:
                valid = int(match[2]) < len(content.get(match[1], []))
            if not valid or not 1 <= len(text) <= 4000:
                raise RequestError("请指定报告中的有效段落位置，并提供 1–4000 字纠错说明")
            feedback = {"location": location, "text": text}
            question = f"核查原报告 {location}：{text}。判断反馈是否成立，说明证据、根因与最小改进建议。"
    elif feedback is not None:
        raise RequestError("纠错必须关联原报告")
    payload = json.dumps({"question": question, "parent_report_id": parent_report_id, "feedback": feedback},
                         ensure_ascii=False, sort_keys=True)
    # The unique request_id resolves simultaneous submissions without a read/insert race.
    row = conn.execute(
        "INSERT INTO runs (request_id, kind, code, name, status, created_at, request)"
        " VALUES (?, 'research', ?, ?, 'queued', ?, ?) ON CONFLICT(request_id) DO NOTHING RETURNING id",
        (request_id, code, name, db.now(), payload),
    ).fetchone()
    if row:
        return row["id"]
    previous = conn.execute("SELECT id, kind, code, request FROM runs WHERE request_id = ?", (request_id,)).fetchone()
    if previous["kind"] != "research" or previous["code"][:6] != code or previous["request"] != payload:
        raise RequestError("请求编号已用于不同内容，请换一个编号", 409)
    return previous["id"]


def ready(status: str, passed: int) -> bool:
    """A report is final only when its run agrees: reviewed and done, or failed."""
    return (status, passed) in (("done", 1), ("failed", 0))


def feedback_retry_payload(run) -> dict | None:
    """Only an explicitly saved, failed feedback task can retry its original question."""
    if run['kind'] != 'research' or run['status'] != 'failed':
        return None
    if run['replay_of'] is not None:
        return None
    request = json.loads(run['request'] or '{}') or {}
    parent, feedback = request.get('parent_report_id'), request.get('feedback')
    if type(parent) is not int or not isinstance(feedback, dict):
        return None
    if not all(isinstance(feedback.get(key), str) and feedback[key].strip() for key in ('location', 'text')):
        return None
    return {'parent_report_id': parent, 'feedback': feedback}


def retry_feedback(conn, run_id: int, request_id: str) -> int:
    run = conn.execute('SELECT kind, code, status, request, replay_of FROM runs WHERE id=?', (run_id,)).fetchone()
    if run is None:
        raise RequestError('核查任务不存在', 404)
    payload = feedback_retry_payload(run)
    if payload is None:
        raise RequestError('仅支持重试已失败且保留完整原反馈的核查任务', 409)
    # submit revalidates parent readiness, company and claim position. The browser
    # supplies neither a replacement parent nor edited feedback on this route.
    return submit(conn, run['code'], request_id, **payload)


def _table_periods(body: dict) -> list[str]:
    rows = body.get("table")
    return sorted({p for row in (rows if isinstance(rows, list) else [])
                   if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].startswith("T.")
                   and isinstance(row.get("values"), dict) for p in row["values"] if isinstance(p, str)})


def report_view(conn: sqlite3.Connection, report_id: int, *, compact: bool = False) -> dict:
    # One SELECT gives a consistent task/report snapshot while a worker is saving a result.
    row = conn.execute(
        "SELECT r.*, u.status, u.error, u.identity, u.material, u.request,"
        " (SELECT json_group_array(identity) FROM calls WHERE run_id = r.run_id) AS call_identities FROM reports r"
        " JOIN runs u ON u.id = r.run_id WHERE r.id = ?", (report_id,),
    ).fetchone()
    if row is None:
        raise RequestError("报告不存在", 404)
    if not ready(row["status"], row["passed"]):
        raise RequestError("报告尚未就绪或状态不一致", 409)
    body = json.loads(row["body"])
    if "review_audit" in body:
        body["review_audit"] = audit_view(body["review_audit"])
    material = json.loads(row["material"] or "null")
    data = material["data"] if material else {}
    extra = data.get("extra", {})
    evidence = {"sources": extra.get("sources", {}), "table": body.get("table", []),
                "market_date": body.get("market_date"), "material_sha256": body.get("material_sha256"),
                "methods_used": extra.get("methods_used", [])}
    source_view = report_source_view(body, data)
    evidence["sources"] = projection(source_view["sources"], catalog=True, raw=True)
    evidence["source_error"] = source_view["error"]
    result = {"version": VERSION, "id": row["id"], "run_id": row["run_id"], "code": row["code"],
            "name": row["name"], "created_at": row["created_at"], "status": row["status"],
            "passed": bool(row["passed"]), "error": row["error"],
            "question": (json.loads(row["request"] or "{}") or {}).get("question", ""),
            "report": body if row["passed"] else {"facts": body.get("facts", {}),
                                                   "facts_reviewed": body.get("facts_reviewed"),
                                                   "problems": body.get("problems", [])},
            "evidence": evidence, "identity": json.loads(row["identity"] or "null"),
            "model_calls": [{key: identity.get(key) for key in
                             ("requested", "actual", "pi_version", "node_version", "parameters", "prompt_sha256", "pi_infer_sha256")}
                            if (identity := json.loads(value or "null")) else None
                            for value in json.loads(row["call_identities"])],
            "coverage": {"periods": _table_periods(body),
                         "missing": body.get("gaps", extra.get("gaps", [])),
                         "announcement_searches": extra.get("announcement_searches", []),
                         "limits": "覆盖已读取的披露和财务数据；公告检索范围单独列出，不代表全部公告、新闻或账户事实。历史结果不自动刷新。"}}
    result["diagnostics"] = diagnostics(row["status"], body, gaps=extra.get("gaps"),
                                        announcement_searches=extra.get("announcement_searches"))
    if not compact:
        return result  # Keep the original full evidence wire contract.
    result["view"] = "compact"
    result["report"] = ({key: body[key] for key in
                         ("verdict", "summary", "answer", "risks", "business", "financial", "methods", "checkpoints")
                         if key in body} if row["passed"] else result["report"])
    result["evidence"] = {"market_date": evidence["market_date"],
                          "material_sha256": evidence["material_sha256"],
                          "source_error": evidence["source_error"],
                          "sources": {key: {"document": source["document"],
                                              "pages_available": source["read_pages"], "state": source["state"]}
                                      for key, source in evidence["sources"].items()}}
    result.pop("identity")
    result.pop("model_calls")
    result["budget"] = run_budget(conn, row["run_id"])
    result["diagnostics"] = diagnostics(row["status"], body, gaps=extra.get("gaps"),
                                        announcement_searches=extra.get("announcement_searches"))
    result["continuation"] = continuation(conn, row, body, extra)
    return result


def run_budget(conn, run_id: int | None = None) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM calls WHERE run_id = ?", (run_id,)).fetchone()[0] if run_id is not None else None
    return {"model_call_limit": MAX_CALLS, "model_calls_recorded": count, "run_id": run_id,
            "scope": "run" if run_id is not None else "configuration",
            "limit_kind": "loop_guard_not_money_cap", "cost": None, "cost_status": "unavailable",
            "automatic_paid_restart": False,
            "note": "调用上限不是金额预算；无法据调用次数推算账单。失败后不自动创建新任务。"}


def diagnostics(status: str, body: dict, *, gaps: list | None = None,
                announcement_searches: list | None = None, report_available: bool = True) -> dict:
    problems = body.get("problems", [i for i in (body.get("review_audit") or {}).get("issues", [])
                                     if i.get("status") == "unresolved"])
    problems = [issue_view(i) for i in problems if isinstance(i, dict)]
    missing = body.get("gaps", gaps or [])
    gaps_basis = "report" if "gaps" in body else "material_snapshot" if gaps is not None else "unavailable"
    next_step = "历史报告不自动刷新；追问先核查是否有新披露。"
    if status in {"queued", "running"}:
        next_step = "任务处理中；按原任务编号间隔查询，不重新提交。"
    elif status == "failed":
        next_step = ("先查看 error、具体核查问题与已取证材料，缩小到一个关键缺口；继续研究须另行确认，不自动重跑。"
                     if report_available else "尚未形成报告；先查看 error 与 diagnostics.gaps，缩小到一个关键缺口；继续研究须另行确认，不自动重跑。")
    return {"status": "passed" if status == "done" else "review_failed" if status == "failed" and problems else status,
            "problems": problems, "issue_states": legacy_issue_view(problems),
            "failure": body.get("failure"), "review_audit": audit_view(body.get("review_audit")),
            "stop_reasons": body.get("stop_reasons", []), "gaps": missing, "gaps_basis": gaps_basis, "next_step": next_step,
            "announcement_searches": body.get("announcement_searches", announcement_searches or []),
            "limits": "缺口与预算提示不是失败根因判定；不据此推断未披露或清除投资门禁。"}


def continuation(conn, row, body: dict, extra: dict) -> dict:
    from a_share_research import reading

    compared = reading.comparison(conn, row)
    parent = compared.get("old", {})
    previous = json.loads(parent["body"]) if parent else {}
    baseline_status = compared["baseline_status"]
    changes = {key: compared[key] for key in ("diff", "excluded", "missing", "reason") if key in compared}
    if not row["passed"] or baseline_status != "ready":
        changes.pop("diff", None)
        changes.setdefault("reason", "至少一份报告未通过核查，不形成已核查报告差值。")
    documents = [source["document"] for source in extra.get("sources", {}).values()]
    documents += [body.get("document") or {}]
    return {"parent_report_id": compared["parent_report_id"],
            "baseline_status": baseline_status,
            "baseline": {"generated_at": parent.get("created_at"),
                         "financial_period": max([parent.get("period") or "", *_table_periods(previous)]) or None,
                         "market_date": previous.get("market_date"),
                         "document": previous.get("document"),
                         "announcement_searches": previous.get("announcement_searches", []),
                         "status": parent.get("status"), "passed": bool(parent["passed"]) if parent else None},
            "previous_checkpoints": previous.get("checkpoints", []) if baseline_status == "ready" else [],
            "previous_checkpoints_basis": "agenda_to_recheck_not_current_facts",
            "current_checkpoints": body.get("checkpoints", []) if row["passed"] else [],
            "table_changes": changes,
            "generated_at": row["created_at"], "financial_period": max([row["period"], *_table_periods(body)]),
            "market_date": body.get("market_date"),
            "disclosures": [{key: doc.get(key) for key in ("title", "period", "announced", "url")}
                            for doc in documents if doc],
            "announcement_searches": extra.get("announcement_searches", []),
            "freshness_status": "historical_not_live_checked",
            "limits": "表格变化不是投资理由变化；观察条件须重新取证，不自动写入 L3 或账户。"}


def run_view(conn, *, run_id: int | None = None, request_id: str | None = None) -> dict:
    if request_id is not None and not 1 <= len(request_id) <= 128:
        raise RequestError("请求编号须为 1–128 字")
    field, value = ("request_id", request_id) if request_id is not None else ("id", run_id)
    row = conn.execute("SELECT u.id, u.code, u.name, u.status, u.step, u.error, u.created_at, u.finished_at,"
                       " u.material, u.identity, r.id AS report_id, r.body AS report_body FROM runs u LEFT JOIN reports r ON r.run_id = u.id"
                       f" WHERE u.{field} = ? AND u.kind = 'research'", (value,)).fetchone()
    if row is None:
        raise RequestError("研究任务不存在；未找到不代表原提交确定失败，请勿换编号重复提交", 404)
    body = json.loads(row["report_body"]) if row["report_body"] else {}
    material = (json.loads(row["material"]) or {}) if row["material"] else {}
    extra = material.get("data", {}).get("extra", {})
    metadata = json.loads(row["identity"] or "{}") or {}
    for key in ("failure", "review_audit", "stop_reasons"):
        if key not in body and key in metadata | extra:
            body[key] = (extra | metadata)[key]
    public = {key: row[key] for key in row.keys() if key not in {"report_body", "material", "identity"}}
    return {"version": VERSION, **public,
            "budget": run_budget(conn, row["id"]),
            "diagnostics": diagnostics(row["status"], body, gaps=extra.get("gaps"),
                                       announcement_searches=extra.get("announcement_searches"),
                                       report_available=row["report_id"] is not None)}


def create_batch(conn, request_id: str) -> int:
    """Queue a batch; one live batch per calendar month (failed ones do not count)."""
    month = datetime.now(db.SHANGHAI).strftime("%Y-%m")
    row = conn.execute(
        "INSERT INTO batches (request_id, status, created_at, rule) SELECT ?, 'queued', ?, ?"
        " WHERE NOT EXISTS (SELECT 1 FROM batches WHERE substr(created_at, 1, 7) = ? AND status != 'failed')"
        " ON CONFLICT(request_id) DO NOTHING RETURNING id",
        (request_id, db.now(), json.dumps(batches.RULE, ensure_ascii=False), month),
    ).fetchone()
    if row:
        return row["id"]
    existing = conn.execute("SELECT id FROM batches WHERE request_id = ?", (request_id,)).fetchone()
    if existing:
        return existing["id"]
    raise RequestError("本月已有批次；每月一个独立批次", 409)


def freeze_batch(conn, batch_id: int, codes: list[str], note: str, by: str = "owner") -> None:
    """Final picks; validated against the batch and frozen once. `by` is a FROZEN_BY key."""
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise RequestError("批次不存在", 404)
    batch = batches.load(row)
    if batch["status"] != "proposed":
        raise RequestError("批次不在待确认状态", 409)
    rule = batch["rule"]
    allowed = {r["code"]: r for r in batch["q20"] if r.get("verdict") and r["verdict"] != "暂不值得"}
    codes = list(dict.fromkeys(codes))
    if not codes or len(codes) > rule["picks"] or any(c not in allowed for c in codes):
        raise RequestError(f"请从研究通过的候选中选择 1–{rule['picks']} 家")
    counts: dict[str, int] = {}
    for code in codes:
        counts[allowed[code]["industry"]] = counts.get(allowed[code]["industry"], 0) + 1
    if max(counts.values()) > rule["picks_per_industry"]:
        raise RequestError(f"同一行业最多 {rule['picks_per_industry']} 家")
    if len(note) > 2000:
        raise RequestError("说明不超过 2000 字")
    draft = [p["code"] for p in ((batch["selection"] or {}).get("draft") or {}).get("picks", [])]
    l5 = {"codes": codes, "note": note.strip(), "changed_from_draft": codes != draft, "draft": draft,
          "frozen_by": by}
    # RETURNING, not rowcount: the web connection is shared and other writes change its counters.
    updated = conn.execute("UPDATE batches SET l5 = ?, frozen_at = ?, status = 'frozen', updated_at = ?"
                           " WHERE id = ? AND status = 'proposed' RETURNING id",
                           (json.dumps(l5, ensure_ascii=False), db.now(), db.now(), batch_id)).fetchone()
    if updated is None:
        raise RequestError("批次已被冻结", 409)


def submit_price(conn, code: str, request_id: str) -> int:
    report = conn.execute("SELECT name FROM reports WHERE code = ? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    if report is None:
        raise RequestError("请先研究这家公司。", 404)
    return db.create_run(conn, code, request_id or None, kind="price", name=report["name"])
