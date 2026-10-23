"""Shared research submission and public evidence views; no model or account actions."""

import json
import re
import sqlite3

from a_share_research import db

VERSION = "research-v1"


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def submit(conn, code: str, request_id: str, *, name: str | None = None,
           question: str = "", parent_report_id: int | None = None, feedback: dict | None = None) -> int:
    code = code[:6] if re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", code) else ""
    if not code or not 1 <= len(request_id) <= 128 or len(question) > 4000:
        raise RequestError("需要 6 位公司代码、1–128 字请求编号；问题不超过 4000 字")
    question = question.strip()
    if parent_report_id is not None:
        parent = conn.execute("SELECT code, body FROM reports WHERE id = ?", (parent_report_id,)).fetchone()
        if parent is None:
            raise RequestError("原报告不存在", 404)
        if parent["code"][:6] != code:
            raise RequestError("原报告与研究公司不一致")
        if feedback is not None:
            location, text = feedback.get("location", ""), feedback.get("text", "").strip()
            body = json.loads(parent["body"])
            content = body if "summary" in body else body.get("facts", {})
            match = re.fullmatch(r"(answer|risks|business|financial)\[(\d{1,4})\]", location)
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


def report_view(conn: sqlite3.Connection, report_id: int) -> dict:
    # One SELECT gives a consistent task/report snapshot while a worker is saving a result.
    row = conn.execute(
        "SELECT r.*, u.status, u.error, u.identity, u.material, u.request,"
        " (SELECT json_group_array(identity) FROM calls WHERE run_id = r.run_id) AS call_identities FROM reports r"
        " JOIN runs u ON u.id = r.run_id WHERE r.id = ?", (report_id,),
    ).fetchone()
    if row is None:
        raise RequestError("报告不存在", 404)
    if not ((row["status"] == "done" and row["passed"] == 1)
            or (row["status"] == "failed" and row["passed"] == 0)):
        raise RequestError("报告尚未就绪或状态不一致", 409)
    body = json.loads(row["body"])
    material = json.loads(row["material"] or "null")
    data = material["data"] if material else {}
    extra = data.get("extra", {})
    evidence = {"sources": extra.get("sources", {}), "table": body.get("table", []),
                "market_date": body.get("market_date"), "material_sha256": body.get("material_sha256")}
    evidence["sources"] = {"D0": {"document": data.get("doc", body.get("document")),
                                     "pages": data.get("pages", {})}, **evidence["sources"]}
    return {"version": VERSION, "id": row["id"], "run_id": row["run_id"], "code": row["code"],
            "name": row["name"], "created_at": row["created_at"], "status": row["status"],
            "passed": bool(row["passed"]), "error": row["error"],
            "question": json.loads(row["request"] or "{}").get("question", ""),
            "report": body if row["passed"] else {"facts": body.get("facts", {}), "problems": body.get("problems", [])},
            "evidence": evidence, "identity": json.loads(row["identity"] or "null"),
            "model_calls": [{key: identity.get(key) for key in
                             ("requested", "actual", "pi_version", "node_version", "parameters", "prompt_sha256", "pi_infer_sha256")}
                            if (identity := json.loads(value or "null")) else None
                            for value in json.loads(row["call_identities"])],
            "coverage": {"periods": sorted({p for r in evidence["table"] if not r["id"].startswith("C.")
                                             for p in r["values"]}),
                         "missing": extra.get("gaps", []),
                         "limits": "覆盖所列定期披露和财务数据；不含全部临时公告、新闻或账户事实。历史结果不自动刷新。"}}
