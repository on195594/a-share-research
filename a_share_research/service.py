"""Shared research submission and public evidence views; no model or account actions."""

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import nullcontext
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from requests import RequestException

from a_share_research import report as report_body
from a_share_research import api_keys, batches, companies, db, stance as stances
from a_share_research import stock_codes
from a_share_research.limits import MAX_CALLS, MONTHLY_MAX_CALLS
from a_share_research.evidence import report_source_view, projection
from a_share_research.reviewing import legacy_issue_view, audit_view, issue_view, binding_view

VERSION = "research-v1"


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(pattern=stock_codes.KEY_PATTERN)
    request_id: str = Field(min_length=1, max_length=128)
    question: str = Field(default="", max_length=4000)
    parent_report_id: int | None = Field(default=None, gt=0)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


def requester(conn: sqlite3.Connection, principal: api_keys.Principal) -> str:
    """The existing web/scan identity for a persistent credential."""
    if not api_keys.is_active(conn, principal.key_id, principal.role):
        raise RequestError("研究凭据无效或已撤销", 403)
    token_hash = conn.execute("SELECT token_hash FROM api_keys WHERE id=?", (principal.key_id,)).fetchone()[0]
    # PATs keep the pre-v25 digest identity so earlier requests still replay; OAuth grants hold no PAT.
    return "key:" + token_hash if token_hash else f"grant:{principal.key_id}"


def _request_key(requester: str, request_id: str) -> str:
    if not 1 <= len(request_id) <= 128:
        raise RequestError("请求编号须为 1–128 字")
    # Longer than any client ID, so a legacy lookup can never match a scoped key.
    digest = hashlib.sha512(f"{requester}\0{request_id}".encode()).hexdigest()
    return f"research:{digest}"


def _previous_request(conn, requester: str, request_id: str):
    key = _request_key(requester, request_id)
    # Legacy web research and feedback shared bare IDs with system-generated tasks.
    return conn.execute(
        "SELECT id, kind, company_key, name, request FROM runs WHERE request_id=? "
        "OR (request_id=? AND kind='research' AND request_id NOT GLOB 'batch-*' "
        "AND request_id NOT GLOB 'scan-*' AND request_id NOT GLOB 'web-retry:*') "
        "ORDER BY request_id=? DESC LIMIT 1", (key, request_id, key),
    ).fetchone()


def _form_query(query: str) -> str:
    return re.sub(r"\s+", "", query).upper().removesuffix(".SH").removesuffix(".SZ").removesuffix(".BJ")


def run_link(run_id: int) -> dict:
    return {"run_id": run_id, "path": f"/runs/{run_id}"}


def request_research(conn, requester: str, request_id: str, code: str, *, question: str = "",
                     parent_report_id: int | None = None, form: bool = False, again: bool = False) -> dict:
    """Replay the accepted intent before consulting the directory, including browser name searches."""
    query = _form_query(code) if form else code
    previous = _previous_request(conn, requester, request_id)
    if previous:
        saved = json.loads(previous["request"] or "null") or {}
        queries = (previous["company_key"], saved.get("company_query", previous["name"]))
        if (previous["kind"] != "research" or query not in queries or saved.get("feedback") or saved.get("events")
                or saved.get("question", "") != question.strip() or saved.get("parent_report_id") != parent_report_id):
            raise RequestError("请求编号已用于不同内容，请换一个编号", 409)
        return run_link(previous["id"])

    try:
        if form:
            company, choices = companies.resolve(query)
            if company is None:
                error = "找到多家公司，请选择一家" if choices else "没有找到这家公司，请输入 6 位代码或简称"
                return {"q": code, "error": error, "choices": choices}
        else:
            company = companies.company(code)
    except companies.SourceError as exc:
        raise RequestError(str(exc)) from exc
    except RequestException as exc:
        raise RequestError("暂时无法连接巨潮资讯，请稍后重试", 503) from exc
    name = company["zwjc"].replace(" ", "")
    if companies.FINANCIAL_NAME.search(company["zwjc"]):
        if form:
            return {"q": code, "error": f"{name}属于银行、保险、券商等金融类公司，暂不支持研究。"}
        raise RequestError("金融类公司暂不支持研究")
    if form and not again and not question.strip() and parent_report_id is None:
        from a_share_research import reading
        if recent := reading.recent_report(conn, company["code"]):
            return {"q": company["code"], "recent": recent}
    run_id = submit(conn, company["code"], request_id, name=name, question=question, parent_report_id=parent_report_id,
                    requester=requester, company_query=query if query != company["code"] else None)
    return run_link(run_id)


def request_feedback(conn, requester: str, report_id: int, request_id: str, location: str, text: str,
                     *, code: str = "") -> dict:
    feedback = {"location": location, "text": text.strip()}
    previous = _previous_request(conn, requester, request_id)
    if previous:
        saved = json.loads(previous["request"] or "null") or {}
        if (previous["kind"] != "research" or (code and previous["company_key"] != code)
                or saved.get("parent_report_id") != report_id or saved.get("feedback") != feedback):
            raise RequestError("请求编号已用于不同内容，请换一个编号", 409)
        run_id = previous["id"]
    else:
        run_id = submit(conn, code, request_id, requester=requester, parent_report_id=report_id, feedback=feedback)
    return run_link(run_id)


def submit(conn, code: str, request_id: str, *, name: str | None = None,
           question: str = "", parent_report_id: int | None = None, feedback: dict | None = None,
           events: list[dict] | None = None, requester: str | None = None, company_query: str | None = None) -> int:
    valid_code = stock_codes.normalize_key(code) if stock_codes.CODE_RE.fullmatch(code) else ""
    if (not valid_code and (code or parent_report_id is None)) or not 1 <= len(request_id) <= 128 or len(question) > 4000:
        raise RequestError("需要 6 位公司代码、1–128 字请求编号；问题不超过 4000 字")
    code = valid_code
    question = question.strip()
    if parent_report_id is not None:
        parent = conn.execute(
            "SELECT r.company_key, r.name, r.passed, r.body, u.status, u.kind FROM reports r"
            " JOIN runs u ON u.id = r.run_id WHERE r.id = ?", (parent_report_id,),
        ).fetchone()
        if parent is None:
            raise RequestError("原报告不存在", 404)
        if not ready(parent["status"], parent["passed"]):
            raise RequestError("报告尚未就绪或状态不一致", 409)
        if parent["kind"] != "research":
            raise RequestError("诊断结果不能作为正式研究的追问或纠错基础", 409)
        code = code or parent["company_key"]
        name = name or parent["name"]
        if parent["company_key"] != code:
            raise RequestError("原报告与研究公司不一致")
        if feedback is not None:
            location, text = feedback.get("location", ""), feedback.get("text", "").strip()
            body = report_body.load(parent["body"])
            content = body if "summary" in body else body.get("facts", {})
            match = re.fullmatch(r"(answer|risks|business|financial|methods|checkpoints|prior_status)\[(\d{1,4})\]", location)
            valid = location == "summary" and bool(content.get("summary"))
            if match:
                valid = int(match[2]) < len(content.get(match[1], []))
            if not valid or not 1 <= len(text) <= 4000:
                raise RequestError("请指定报告中的有效段落位置，并提供 1–4000 字纠错说明")
            feedback = {"location": location, "text": text}
            question = f"核查原报告 {location}：{text}。判断反馈是否成立，说明证据、根因与最小改进建议。"
    elif feedback is not None:
        raise RequestError("纠错必须关联原报告")
    request = {"question": question, "parent_report_id": parent_report_id, "feedback": feedback}
    if requester is not None:
        request_id = _request_key(requester, request_id)
    if company_query is not None:
        request["company_query"] = company_query
    if events:
        # Frozen public announcement identities from the scanner; older requests keep their exact payload.
        request["events"] = events
    payload = json.dumps(request, ensure_ascii=False, sort_keys=True)
    # The unique request_id resolves simultaneous submissions without a read/insert race.
    row = conn.execute(
        "INSERT INTO runs (request_id, kind, code, name, status, created_at, request)"
        " VALUES (?, 'research', ?, ?, 'queued', ?, ?) ON CONFLICT(request_id) DO NOTHING RETURNING id",
        (request_id, code, name, db.now(), payload),
    ).fetchone()
    if row:
        return row["id"]
    previous = conn.execute("SELECT id, kind, company_key, request FROM runs WHERE request_id = ?", (request_id,)).fetchone()
    if previous["kind"] != "research" or previous["company_key"] != code or previous["request"] != payload:
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


def retry_feedback(conn, run_id: int, request_id: str, *, requester: str) -> int:
    run = conn.execute('SELECT kind, company_key, status, request, replay_of FROM runs WHERE id=?', (run_id,)).fetchone()
    if run is None:
        raise RequestError('核查任务不存在', 404)
    payload = feedback_retry_payload(run)
    if payload is None:
        raise RequestError('仅支持重试已失败且保留完整原反馈的核查任务', 409)
    feedback = payload['feedback']
    return request_feedback(conn, requester, payload['parent_report_id'], request_id,
                            feedback['location'], feedback['text'], code=run['company_key'])['run_id']


def retry_view(run) -> dict:
    """What the retry button says; a batch stays linked to the failed run, so its retry stands alone."""
    if run['kind'] == 'price':
        label = '重试价格参考'
    else:
        label = '重试原研究问题' if (json.loads(run['request'] or '{}') or {}).get('question') else '重新运行这次研究'
    note = '重试会新建独立任务，结果不回填本批次，该候选在批次中仍显示失败、不可选；调用上限按普通研究计算。'
    return {'label': label, 'note': note if run['batch_id'] is not None else None}


def retry_run(conn, run_id: int) -> int:
    """Retry the saved task once, even across refreshed pages or concurrent clicks."""
    run = conn.execute('SELECT kind, code, name, status, replay_of, request FROM runs WHERE id=?', (run_id,)).fetchone()
    if run is None:
        raise RequestError('任务不存在', 404)
    if run['status'] != 'failed' or run['replay_of'] is not None or run['kind'] not in ('research', 'price'):
        raise RequestError('仅支持重试已失败的研究或价格参考', 409)
    key = f'web-retry:{run_id}'
    with db.transaction(conn):
        if run['kind'] == 'price':
            previous = conn.execute('SELECT id, kind, code FROM runs WHERE request_id=?', (key,)).fetchone()
            if previous and (previous['kind'] != 'price' or previous['code'] != run['code']):
                raise RequestError('重试编号已用于不同任务', 409)
            return submit_price(conn, run['code'], key)
        payload = json.loads(run['request'] or '{}') or {}
        retried = submit(conn, run['code'], key, name=run['name'], question=payload.get('question') or '',
                         parent_report_id=payload.get('parent_report_id'), feedback=payload.get('feedback'),
                         events=payload.get('events'))
        # Scan events follow the retry, so their pending state tracks the task that can still finish.
        conn.execute('UPDATE scan_events SET run_id=? WHERE run_id=?', (retried, run_id))
        return retried


def _table_periods(body: dict) -> list[str]:
    rows = body.get("table")
    return sorted({p for row in (rows if isinstance(rows, list) else [])
                   if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].startswith("T.")
                   and isinstance(row.get("values"), dict) for p in row["values"] if isinstance(p, str)})


def report_view(conn: sqlite3.Connection, report_id: int, *, compact: bool = False) -> dict:
    # One SELECT gives a consistent task/report snapshot while a worker is saving a result.
    row = conn.execute(
        "SELECT r.id, r.run_id, r.code, r.name, r.created_at, r.period, r.passed, r.body,"
        " u.kind AS purpose, u.status, u.error, u.identity, u.material, u.request,"
        " (SELECT json_group_array(identity) FROM calls WHERE run_id = r.run_id) AS call_identities FROM reports r"
        " JOIN runs u ON u.id = r.run_id WHERE r.id = ?", (report_id,),
    ).fetchone()
    if row is None:
        raise RequestError("报告不存在", 404)
    if not ready(row["status"], row["passed"]):
        raise RequestError("报告尚未就绪或状态不一致", 409)
    body = report_body.load(row["body"])
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
            "report": body if row["passed"] else {key: body.get(key, default) for key, default in
                                                   zip(report_body.FAILED_FIELDS, ({}, None, []))} |
                                                   ({'producer': body['producer']} if 'producer' in body else {}),
            "purpose": row["purpose"], "evidence": evidence, "identity": json.loads(row["identity"] or "null"),
            "model_calls": [{key: identity.get(key) for key in
                             ("requested", "actual", "pi_version", "node_version", "parameters", "prompt_sha256", "pi_infer_sha256")}
                            if (identity := json.loads(value or "null")) else None
                            for value in json.loads(row["call_identities"])],
            "coverage": {"periods": _table_periods(body),
                         "missing": body.get("gaps", extra.get("gaps", [])),
                         "announcement_searches": extra.get("announcement_searches", []),
                         "limits": "覆盖已读取的披露和财务数据；公告检索范围单独列出，历史结果不自动刷新。"}}
    result["diagnostics"] = diagnostics(row["status"], body, gaps=extra.get("gaps"),
                                        announcement_searches=extra.get("announcement_searches"))
    if not compact:
        return result  # Keep the original full evidence wire contract.
    result["view"] = "compact"
    result["report"] = ({key: body[key] for key in
                         report_body.COMPACT_FIELDS if key in body} if row["passed"] else result["report"])
    result["evidence"] = {"market_date": evidence["market_date"],
                          "material_sha256": evidence["material_sha256"],
                          "source_error": evidence["source_error"],
                          "sources": {key: {"document": source["document"],
                                              "pages_available": source["read_pages"], "state": source["state"]}
                                      for key, source in evidence["sources"].items()}}
    result.pop("identity")
    result.pop("model_calls")
    result["budget"] = run_budget(conn, row["run_id"])
    result["continuation"] = continuation(conn, row, body, extra)
    return result


def card_checks(conn: sqlite3.Connection, report_id: int) -> list[dict]:
    """Public effective disclosure checks; storage identities stay internal."""
    from a_share_research import disclosure_checks, scans

    fields = ('ref', 'kind', 'metric', 'period', 'source', 'announced',
              'observed', 'outcome', 'direction', 'reason')
    return [{key: check[key] for key in fields}
            for check in disclosure_checks.latest(scans.stored_checks(conn, report_id))]


def company_card(conn: sqlite3.Connection, code: str, *, scan_id: int | None = None) -> dict:
    """A2 read-only projection of the latest reviewed, non-feedback company research."""
    if not stock_codes.KEY_RE.fullmatch(code):
        raise RequestError("公司代码必须为六位数字")
    row = conn.execute(
        "SELECT r.id, r.name, r.created_at, COALESCE(u.finished_at,r.created_at) AS completed_at, r.period, r.body FROM reports r JOIN runs u ON u.id = r.run_id "
        f"WHERE r.company_key = ? AND {db.FORMAL_REPORT} "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1", (code,),
    ).fetchone()
    if row is None:
        raise RequestError("暂无通过复核的非纠错研究报告", 404)
    body = report_body.load(row["body"])
    calc = body.get("valuation_calc")
    summary = None
    if isinstance(calc, dict):
        summary = {k: calc[k] for k in ("rule", "zone", "zones", "implied", "consensus", "as_of", "close", "not_priced", "error")
                   if k in calc}
        if "scenarios" in calc:
            summary["scenarios"] = {name: {"annual_return": scenario["annual_return"]}
                                    for name, scenario in calc["scenarios"].items()}
        if isinstance(calc.get("v2"), dict):
            summary["v2"] = {k: calc["v2"][k] for k in ("rule", "probabilities", "annual_return", "zone", "support_below")}
        summary["boundary"] = stances.on_boundary(calc)
        summary["flip_distance"] = stances.flip_distance(calc)
    from a_share_research import scans, valuation
    pending = scans.open_events(conn, code)
    failure = scans.announcement_failure(conn, code, scan_id=scan_id)
    if failure:
        pending.append(failure)
    current = scans.latest_close(conn, code)
    current['valuation'] = (valuation.at(calc, current['last_close'], current['last_close_date'])
                            if current['last_close'] is not None else None)
    if current['valuation'] is not None:
        current['valuation']['boundary'] = stances.on_boundary(current['valuation'])
        rolled = current['valuation']
        # at() omits frozen inputs; restore them using its unchanged-share-count price basis.
        rolled['flip_distance'] = stances.flip_distance({
            **calc, **rolled, 'market_cap': calc['market_cap'] * rolled['close'] / calc['close'],
        }) if rolled.get('v2') is not None else None
    current['current_zone'] = current['valuation']['zone'] if current['valuation'] is not None else None
    if pending:
        current['note'] = '公告扫描未完成，区间暂停解读' if failure else '待复核，区间暂停解读'
    return {"version": VERSION, "code": code, "name": row['name'], "report_id": row["id"], "period": row["period"],
            "created_at": row['created_at'], "completed_at": row['completed_at'], "pending_events": pending, "current": current,
            "status": "待复核" if pending else "有效",
            "checks": card_checks(conn, row["id"]),
            "check_failure": scans.mechanical_failure(conn, code, row["id"], scan_id=scan_id),
            **{key: body.get(key) for key in report_body.CARD_FIELDS},
            # Four card fields are reshaped rather than copied from the stored body.
            "risks": [item["text"] for item in body.get("risks", [])[:3]],
            "checkpoints": [{k: item[k] for k in ("text", "forecast") if k in item}
                            for item in body.get("checkpoints", [])],
            "valuation_calc": summary,
            "open_questions": body.get("open_questions", [])}


def company_cards(conn: sqlite3.Connection, *, holdings: bool = False) -> dict:
    """Read subscription and frozen L5 cards, optionally including verified holdings for policy monitoring."""
    from a_share_research import scans

    scope = {item['code']: item['origin'] for item in scans.subscriptions(conn)}
    result = {'version': VERSION}
    if holdings:
        from a_share_research.account import mirror
        from a_share_research.policy import account_view, registry

        with nullcontext() if conn.in_transaction else db.transaction(conn, immediate=False):
            try:
                current = account_view.verified_current(conn, mirror.target_directory(conn))
            except (registry.PolicyError, sqlite3.Error, OSError) as exc:
                logging.getLogger(__name__).warning('Could not verify holdings for policy research cards: %s', exc)
                result['holdings_available'] = False
            else:
                result['holdings_available'] = True
                for code in current['codes']:
                    # Keep the monitor contract's external/l5 origins and preserve existing L5 candidates.
                    scope.setdefault(code, 'external')
    cards, missing = [], []
    for code, origin in scope.items():
        card = scans.card_or_none(conn, code)
        if card is None:
            missing.append({'code': code, 'origin': origin})
        else:
            cards.append(card | {'origin': origin})
    return result | {'cards': cards, 'missing': missing}


def is_monthly_research(conn, run_id: int | None) -> bool:
    # Queued/gathering runs have six digits; gathered runs include the exchange suffix.
    return conn.execute(
        "SELECT 1 FROM runs r JOIN batches b ON r.batch_id = b.id "
        "WHERE r.id = ? AND b.request_id LIKE 'monthly:%'", (run_id,)).fetchone() is not None


def research_call_limit(conn, run_id: int | None = None) -> int:
    return MONTHLY_MAX_CALLS if is_monthly_research(conn, run_id) else MAX_CALLS


def run_budget(conn, run_id: int | None = None) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM calls WHERE run_id = ?", (run_id,)).fetchone()[0] if run_id is not None else None
    return {"model_call_limit": research_call_limit(conn, run_id), "model_calls_recorded": count, "run_id": run_id,
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
            "review_binding": binding_view(body.get("review_binding")),
            "stop_reasons": body.get("stop_reasons", []), "gaps": missing, "gaps_basis": gaps_basis, "next_step": next_step,
            "announcement_searches": body.get("announcement_searches", announcement_searches or []),
            "limits": "缺口与预算提示仅反映本次运行状态。"}


def continuation(conn, row, body: dict, extra: dict) -> dict:
    from a_share_research import reading

    compared = reading.comparison(conn, row)
    parent = compared.get("old", {})
    previous = report_body.load(parent["body"]) if parent else {}
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
            "previous_risks": previous.get("risks", []) if baseline_status == "ready" else [],
            "current_checkpoints": body.get("checkpoints", []) if row["passed"] else [],
            "table_changes": changes,
            "generated_at": row["created_at"], "financial_period": max([row["period"], *_table_periods(body)]),
            "market_date": body.get("market_date"),
            "disclosures": [{key: doc.get(key) for key in ("title", "period", "announced", "url")}
                            for doc in documents if doc],
            "announcement_searches": extra.get("announcement_searches", []),
            "freshness_status": "historical_not_live_checked",
            "limits": "仅对比已存表格变化；观察条件须重新取证。"}


def run_view(conn, *, run_id: int | None = None, request_id: str | None = None) -> dict:
    if request_id is not None and not 1 <= len(request_id) <= 128:
        raise RequestError("请求编号须为 1–128 字")
    field, value = ("request_id", request_id) if request_id is not None else ("id", run_id)
    row = conn.execute("SELECT u.id, u.kind, u.code, u.name, u.status, u.step, u.error, u.created_at, u.finished_at,"
                       " u.material, u.identity, u.request, r.id AS report_id, r.body AS report_body FROM runs u LEFT JOIN reports r ON r.run_id = u.id"
                       f" WHERE u.{field} = ? AND u.kind IN ('research', 'diagnostic')", (value,)).fetchone()
    if row is None:
        raise RequestError("研究任务不存在；未找到不代表原提交确定失败，请勿换编号重复提交", 404)
    body = report_body.load(row["report_body"]) if row["report_body"] else {}
    material = (json.loads(row["material"]) or {}) if row["material"] else {}
    extra = material.get("data", {}).get("extra", {})
    metadata = json.loads(row["identity"] or "{}") or {}
    for key in ("failure", "review_audit", "stop_reasons"):
        if key not in body and key in metadata | extra:
            body[key] = (extra | metadata)[key]
    public = {key: row[key] for key in row.keys() if key not in {"report_body", "material", "identity", "request"}}
    return {"version": VERSION, **public,
            "budget": run_budget(conn, row["id"]),
            "diagnostics": diagnostics(row["status"], body, gaps=extra.get("gaps"),
                                       announcement_searches=extra.get("announcement_searches"),
                                       report_available=row["report_id"] is not None)}


def run_summary(conn, run_id: int, *, include_gaps: bool = False) -> dict:
    from a_share_research import reading
    row = reading.api_run(conn, run_id, include_gaps=include_gaps)
    if row is None:
        raise RequestError("任务不存在", 404)
    result = {"version": VERSION, **dict(row)}
    if include_gaps:
        gaps = json.loads(result["gaps"] or "[]")
        result["gaps"] = ([row["error"]] if row["error"] else []) + gaps
    return result


def research_status(conn, *, requester: str | None = None, request_id: str | None = None,
                    run_id: int | None = None, compact: bool = True) -> dict:
    if (run_id is None) == (request_id is None):
        raise RequestError("run_id 与 request_id 须提供且仅提供一个")
    if request_id is not None:
        previous = _previous_request(conn, requester, request_id)
        if previous is None:
            raise RequestError("研究任务不存在；未找到不代表原提交确定失败，请勿换编号重复提交", 404)
        run_id = previous["id"]
    if not compact:
        return run_view(conn, run_id=run_id)
    summary = run_summary(conn, run_id, include_gaps=True)
    if summary["kind"] not in ("research", "diagnostic"):
        raise RequestError("研究任务不存在", 404)
    return summary


def create_batch(conn, request_id: str, *, created_at: str | None = None) -> int:
    """Queue a batch; one live batch per calendar month (failed ones do not count)."""
    created_at = created_at or db.now()
    month = datetime.fromisoformat(created_at).astimezone(db.SHANGHAI).strftime("%Y-%m")
    row = conn.execute(
        "INSERT INTO batches (request_id, status, created_at, rule) SELECT ?, 'queued', ?, ?"
        " WHERE NOT EXISTS (SELECT 1 FROM batches WHERE strftime('%Y-%m', created_at, '+8 hours') = ? AND status != 'failed')"
        " ON CONFLICT(request_id) DO NOTHING RETURNING id",
        (request_id, created_at, json.dumps(batches.NEW_RULE, ensure_ascii=False), month),
    ).fetchone()
    if row:
        return row["id"]
    existing = conn.execute("SELECT id FROM batches WHERE request_id = ?", (request_id,)).fetchone()
    if existing:
        return existing["id"]
    raise RequestError("本月已有批次；每月一个独立批次", 409)


def research_search(conn, q: str, *, code: str | None = None, section: str | None = None,
                    history: bool = False, page: int = 1, library: bool = False) -> dict:
    """Search saved research. The library view also recognizes company names and codes."""
    from urllib.parse import urlencode
    from a_share_research import reading

    if code is not None and not stock_codes.KEY_RE.fullmatch(code):
        raise RequestError("公司代码必须为六位数字")
    matched_companies = []
    if library and code is None and section is None:
        found = reading.library_results(conn, q.strip(), history=history, page=page)
        if found["error"]:
            raise RequestError(found["error"])
        for row in found["companies"]:
            matched_companies.append({key: row[key] for key in ("code", "name", "created_at", "coverage", "verdict", "passed")} |
                                     {"report_id": row["id"], "path": f"/stock/{row['code']}"})
        items, more = found["hits"], found["more"]
    else:
        items, more = reading.search_items(conn, q, code=code, section=section, history=history, page=page)
    result = {"version": VERSION, "page": page, "more": more}
    if not library:
        return result | {"items": items}

    hits = []
    for hit in items:
        text = hit["text"]
        item = {key: hit[key] for key in ("report_id", "code", "name", "created_at", "period", "location")}
        item.update(excerpt=text[:200] + ("…" if len(text) > 200 else ""),
                    path=reading.item_link(hit["report_id"], hit["location"]))
        latest = hit.get("latest")
        if latest and latest["id"] != hit["report_id"]:
            item.update(latest_report_id=latest["id"], latest_passed=bool(latest["passed"]))
        hits.append(item)
    params = {"q": q, "page": page}
    if history:
        params["history"] = "true"
    return result | {"companies": matched_companies, "items": hits, "queried_at": db.now(),
                     "status": "found" if matched_companies or hits else "empty",
                     "gaps": [] if matched_companies or hits else ["本页没有匹配的已有研究"],
                     "path": "/research-library?" + urlencode(params)}


def monthly_batch(conn, month: str, *, submit: bool = False) -> dict:
    """Automatic admission only: any existing month's batch, including failure, is final."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise RequestError("月份须为 YYYY-MM", 422)
    fields = ("id, status, error, created_at, frozen_at, as_of, l5, sl3, q20, s10, "
              "json_extract(NULLIF(selection, ''), '$.ranking.error') AS ranking_error")
    query = f"SELECT {fields} FROM batches WHERE strftime('%Y-%m', created_at, '+8 hours') = ? ORDER BY id LIMIT 1"
    if not submit:
        row, created = conn.execute(query, (month,)).fetchone(), False
    else:
        # Serialize with other writers, including the existing manual INSERT. No check/insert race.
        with db.transaction(conn):
            stamp = db.now()  # Decide the month after acquiring the write lock, including midnight waits.
            if month != datetime.fromisoformat(stamp).astimezone(db.SHANGHAI).strftime("%Y-%m"):
                raise RequestError("自动发起仅限当前上海自然月，不补跑历史月份", 409)
            row = conn.execute(query, (month,)).fetchone()
            created = row is None
            if created:
                ident = create_batch(conn, f"monthly:{month}", created_at=stamp)
                row = conn.execute(f"SELECT {fields} FROM batches WHERE id = ?", (ident,)).fetchone()
    if row is None:
        return {"version": VERSION, "month": month, "batch": None, "created": False}
    summary = {key: row[key] for key in ("id", "status", "error", "created_at", "frozen_at", "as_of", "ranking_error")}
    for key in ("l5", "sl3"):
        summary[key] = json.loads(row[key]) if row[key] else None
    # Gather failures keep a six-digit run code; the frozen run links do not change.
    summary["failed_research"] = [dict(r) for r in conn.execute(
        "SELECT code, error FROM runs WHERE status='failed' AND id IN ("
        "SELECT json_extract(value, '$.run_id') FROM json_each(?) "
        "UNION SELECT json_extract(value, '$.run_id') FROM json_each(?)) ORDER BY id",
        (row["q20"] or "[]", row["s10"] or "[]"))]
    return {"version": VERSION, "month": month, "created": created, "batch": summary}


def freeze_batch(conn, batch_id: int, codes: list[str], note: str, by: str = "owner", *,
                 small_codes: list[str] | None = None) -> None:
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
    if (allowed and not codes) or len(codes) > rule["picks"] or any(c not in allowed for c in codes):
        raise RequestError(f"请从研究通过的候选中选择 1–{rule['picks']} 家")
    counts: dict[str, int] = {}
    for code in codes:
        counts[allowed[code]["industry"]] = counts.get(allowed[code]["industry"], 0) + 1
    if max(counts.values(), default=0) > rule["picks_per_industry"]:
        raise RequestError(f"同一行业最多 {rule['picks_per_industry']} 家")
    if len(note) > 2000:
        raise RequestError("说明不超过 2000 字")
    draft = [p["code"] for p in ((batch["selection"] or {}).get("draft") or {}).get("picks", [])]
    l5 = {"codes": codes, "note": note.strip(), "changed_from_draft": codes != draft, "draft": draft,
          "frozen_by": by}
    small_fields = {}
    if rule["version"] in {"screen-v1.2", "screen-v1.3"}:
        small_draft = [p["code"] for p in ((batch.get("small_selection") or {}).get("draft") or {}).get("picks", [])]
        chosen = list(dict.fromkeys(small_draft if small_codes is None else small_codes))
        allowed_small = {r["code"]: r for r in (batch.get("s10") or [])
                         if r.get("verdict") and r["verdict"] != "暂不值得"}
        small_rule = batches.small_rule(rule)
        if len(chosen) > small_rule["picks"] or any(c not in allowed_small for c in chosen):
            raise RequestError(f"请从小市值研究通过的候选中选择至多 {small_rule['picks']} 家，可留空")
        industries = [allowed_small[c]["industry"] for c in chosen]
        if any(industries.count(i) > small_rule["picks_per_industry"] for i in industries):
            raise RequestError(f"小市值组同一行业最多 {small_rule['picks_per_industry']} 家")
        from a_share_research.sources import batch_coverage
        small_fields = {
            "sl3": {"codes": chosen, "note": "", "changed_from_draft": chosen != small_draft,
                    "draft": small_draft, "frozen_by": by},
            "coverage": batch_coverage(batch),
        }
    # RETURNING, not rowcount: keeps the update and atomic id retrieval within one statement.
    extras = "".join(f", {key} = ?" for key in small_fields)
    updated = conn.execute("UPDATE batches SET l5 = ?, frozen_at = ?, status = 'frozen', updated_at = ?" + extras +
                           " WHERE id = ? AND status = 'proposed' RETURNING id",
                           (json.dumps(l5, ensure_ascii=False), db.now(), db.now(),
                            *(json.dumps(v, ensure_ascii=False) for v in small_fields.values()), batch_id)).fetchone()
    if updated is None:
        raise RequestError("批次已被冻结", 409)


def submit_price(conn, code: str, request_id: str) -> int:
    try:
        key = stock_codes.normalize_key(code)
    except ValueError as exc:
        raise RequestError("需要6位数字或带合法交易所后缀的股票代码", 400) from exc
    report = conn.execute(
        "SELECT code, name FROM reports WHERE company_key = ? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    if report is None:
        raise RequestError("请先研究这家公司。", 404)
    run_code = report["code"] if "." in report["code"] else stock_codes.infer_ts_code(code)
    return db.create_run(conn, run_code, request_id or None, kind="price", name=report["name"])
