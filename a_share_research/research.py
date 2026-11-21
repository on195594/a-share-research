"""Gather one company's evidence, run its research agent and atomically publish the reviewed result."""

import argparse
import json
import logging
import os
import re
import time
from copy import deepcopy
from datetime import date

from a_share_research import eval_report
from a_share_research import report as report_body
from a_share_research import db, evidence, market, model_profile, runner_process, sources, stance as stances, stock_codes, valuation
from a_share_research.core.providers.tushare_common import TushareHTTPError
from a_share_research.research_mcp import ResearchSession
from a_share_research.service import research_call_limit
from a_share_research.reviewing import ReviewLedger, digest
from a_share_research.material import Material, ResearchError, ScopeError, gather, replay_material
from a_share_research.drafts import _verified_facts, open_questions, published
from a_share_research.checking import SECTIONS
from a_share_research.research_tools import active_methods_digest, run_method_catalog

log = logging.getLogger(__name__)

EVAL_SET = ["600519", "000858", "600900", "601088", "000333", "002236", "603606", "600276", "002594", "600309", "600036"]


def _research_context(conn, code: str, parent_id: int | None) -> list[dict]:
    code = stock_codes.normalize_key(code)
    parents = conn.execute(
        "SELECT r.id, r.body, r.passed, r.coverage, r.company_key, u.request FROM reports r JOIN runs u ON u.id = r.run_id"
        " WHERE r.company_key = ? AND u.kind = 'research' AND (r.id = ? OR (r.passed = 1 AND u.status = 'done'"
        " AND json_type(u.request, '$.feedback') = 'object')) ORDER BY (r.id = ?) DESC, r.id DESC LIMIT 3",
        (code, parent_id, parent_id),
    ).fetchall()
    context = []
    for parent in parents:
        body = report_body.load(parent["body"])
        content = body if parent["passed"] else body.get("facts", {})
        claims = {k: content.get(k) for k in ("summary", "answer", "methods", "checkpoints", "prior_status", *SECTIONS)}
        # Retired methods must not return through automatic same-company context.
        if parent["id"] != parent_id:
            claims["methods"] = None
        entry = {"report_id": parent["id"], "request": json.loads(parent["request"] or "{}"),
                 "basis": "report" if parent["passed"] else "facts",
                 "claims_to_recheck": claims}
        if parent["id"] == parent_id:
            entry.update(_baseline_window(parent, body))
        if agenda := _corrected_agenda(conn, body):
            entry["prior_agenda"] = agenda
        context.append(entry)
    return context


def _corrected_agenda(conn, body: dict) -> dict | None:
    """The old items a corrected report's prior_status refs point into, unavailable in its own risks and checkpoints."""
    row = conn.execute("SELECT id, body, created_at, coverage, company_key FROM reports WHERE id = ?",
                       ((body.get("prior") or {}).get("report_id"),)).fetchone()
    if row is None:
        return None
    return {"note": "prior_status 的 ref 指这里的旧条目（上一份报告），不是 claims_to_recheck 里的 risks 或 checkpoints", **_agenda(row)}


def _baseline_window(row, body: dict) -> dict:
    # Each search has its own until, keyword and truncation status. A peer's search cannot cover this company.
    return {"period": row["coverage"], "announcement_searches": [
        search for search in body.get("announcement_searches", []) if search.get("code") == row["company_key"]
    ]}


def _agenda(row) -> dict:
    body = report_body.load(row["body"])
    return {"report_id": row["id"], "researched": row["created_at"][:10], **_baseline_window(row, body),
            "market_date": body.get("market_date"),
            "risks": [item["text"] for item in body.get("risks", [])],
            "checkpoints": [item["text"] for item in body.get("checkpoints", [])]}


def _prior_report(conn, code: str, run_id: int) -> dict | None:
    """The company's latest reviewed, non-replay, non-feedback report before this run, as an agenda to recheck.

    Carry its risks, checkpoints and coverage so the new judgment starts from evidence.
    """
    code = stock_codes.normalize_key(code)
    row = conn.execute(
        "SELECT r.id, r.body, r.created_at, r.coverage, r.company_key FROM reports r JOIN runs u ON u.id = r.run_id"
        f" WHERE r.company_key = ? AND r.run_id < ? AND {db.FORMAL_REPORT}"
        " ORDER BY r.id DESC LIMIT 1", (code, run_id),
    ).fetchone()
    return _agenda(row) if row else None


EVENT_QUESTION = "公告扫描发现的 events 对研究判断、风险与检查点有什么影响？"
FINANCIAL_CHECKS = ("T.roe", "T.core_profit", "T.op_cash", "T.capex", "T.fcf")


def financial_checks(table: list[dict], period: str) -> dict:
    """Same-basis cumulative comparison inputs a financial disclosure calls for, with missing reasons."""
    compare = f"{int(period[:4]) - 1}{period[4:]}"
    rows = {r["id"]: r for r in table}
    return {"period": period, "compare": compare, "metrics": [
        {"metric": metric, "label": rows.get(metric, {}).get("label", metric), "periods": {
            p: "available" if p in rows.get(metric, {}).get("values", {})
            else rows.get(metric, {}).get("unavailable", {}).get(p, "财务表无该期数值，须读原文核对或写明无法判断")
            for p in (period, compare)}}
        for metric in FINANCIAL_CHECKS]}


def scan_events(conn, material: Material, frozen: list[dict]) -> list[dict]:
    """Request-frozen events plus the company's still-open ones, listed in the evidence catalog.

    Only public announcement identities travel: no scan judgment, holdings or account data.
    """
    from a_share_research import scans
    merged = {e["id"]: e for e in frozen}
    for event in scans.open_events(conn, material.code):
        merged.setdefault(event["id"], scans.event_context(event))
    if not merged:
        return []
    catalog = material.extra["catalog"]
    result = []
    for event in sorted(merged.values(), key=lambda e: (e["announced"], e["id"])):
        key = next((k for k, v in catalog.items() if v.get("url") == event["url"]), None)
        if key is None:
            key = f"D{max([int(k[1:]) for k in catalog] + [0]) + 1}"
            catalog[key] = sources.Document(
                code=material.code, name=material.name, title=event["title"], period=sources.title_period(event["title"]),
                announced=event["announced"], url=event["url"]).info()
        item = dict(event, source=key)
        if event["kind"] in ("periodic", "forecast") and (period := scans.period_of(event["title"])):
            item["financial_checks"] = financial_checks(material.table, period)
        result.append(item)
    return result


def prior_question(prior: dict) -> str:
    """The question a continuation asks when the user brought none."""
    if "announcement_searches" not in prior:  # Recognize the original question on frozen replays.
        return f"相对上次研究（资料至 {prior['period']}），哪些风险与检查点发生了变化？"
    searches = prior["announcement_searches"]
    cutoff = max((s["until"] for s in searches if s.get("until")), default=None)
    scope = (f"本公司公告检索最晚至 {cutoff}，覆盖范围以各次查询的关键词和实际返回目录为准"
             if cutoff else "本公司公告检索截止日未记录")
    return (f"上次研究的财报期为 {prior['period']}，{scope}。优先核查新增或更正披露，"
            "沿用本次材料已有的原文与表值，按缺口补读历史原文。哪些风险与检查点发生了变化？")


def publish(conn, run_id: int, material: Material, period: str, passed: bool,
            verdict: str | None, body: dict, *, judgment: dict) -> None:
    """Save the report and finish its run together, so a crash never leaves one without the other."""
    body["schema_version"] = report_body.SCHEMA_VERSION
    body["judgment"] = judgment
    report_body.validate(body)
    with db.transaction(conn):
        db.save_report(conn, run_id, material.code, material.name, period, passed, verdict, body)
        if passed:
            db.update_run(conn, run_id, status="done", step=None, error=None, finished_at=db.now())
        else:
            db.update_run(conn, run_id, status="failed", step=None, finished_at=db.now(),
                          error="研究核查未通过，不展示结论")


def calculate_valuation(value: dict, material: Material, *, replay: bool = False) -> dict:
    """Post-review enrichment only; provider/computation failures cannot fail a research."""
    comparison = {"status": "none", "years": {}}
    if "not_priced" not in value:
        if replay:
            comparison = {"status": "unavailable", "reason": "历史快照重放不获取新的卖方预测"}
        else:
            try:
                response = sources.report_rc_window(material.market_date, code=material.code)
                comparison = valuation.consensus(response["rows"], valuation.years(material.table),
                                                 material.market_date, coverage=response)
                if response.get("reason"):
                    comparison["reason"] = response["reason"]
            except Exception as exc:
                log.warning("report_rc %s unavailable (%s)", material.code, type(exc).__name__)
                comparison = {"status": "unavailable", "reason": "卖方预测取数或解析失败"}
    try:
        refs = {key: next((row["values"].get(material.market_date) for row in material.table
                          if row["id"] == f"M.pe_ttm_5y_{key}"), None) for key in ("p30", "p50", "p70")}
        reference = {k: float(v) for k, v in refs.items()} if all(v is not None for v in refs.values()) else None
        return valuation.compute(value, material.table, material.market_date, pe_reference=reference, consensus=comparison)
    except Exception as exc:
        log.warning("valuation %s failed: %s", material.code, exc)
        return {"rule": valuation.RULE, "error": str(exc)}


def stance_calc(stance: dict, calc: dict | None, market_date: str) -> dict:
    """Freeze what the stance will be scored against: the program's wait price and its expiry."""
    out = {"action": stance["action"]}
    if stance["action"] == "等价格":
        out["price"] = ((calc or {}).get("v2") or {}).get("support_below")
        day = date.fromisoformat(market_date)
        month = day.month - 1 + stances.WAIT_MONTHS
        out["valid_until"] = date(day.year + month // 12, month % 12 + 1, min(day.day, 28)).isoformat()
    elif stance["action"] == "等证据":
        out["trigger"] = deepcopy(stance["trigger"])
    if stance["action"] == "现在分批" and "add_trigger" in stance:
        out["add_trigger"] = deepcopy(stance["add_trigger"])
    return out


def _causes(exc: BaseException | None):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


def execute(conn, run_id: int, *, replay_of: int | None = None) -> None:
    run = conn.execute("SELECT code, company_key, request, request_id, started_at, batch_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    code, request = run["code"], json.loads(run["request"] or "{}")
    deadline = time.monotonic() + model_profile.PROFILE.limits.run_seconds
    identity = {}
    session = None
    material = None

    try:
        limit = research_call_limit(conn, run_id)
        identity = model_profile.run_identity(limit) | {"replay_of": replay_of, "material_sha256": None}
        db.update_run(conn, run_id, status="running", step="gather", started_at=run["started_at"] or db.now(),
                      identity=json.dumps(identity))
        with runner_process.time_left(deadline):
            material = replay_material(conn, replay_of) if replay_of is not None else gather(code)
        if stock_codes.normalize_key(material.code) != run["company_key"] or material.doc.code != material.code:
            raise ResearchError("重放材料与目标公司不一致")
        if replay_of is None:
            material.extra["question"] = request.get("question", "")
            material.extra["feedback"] = request.get("feedback")
            material.extra["context"] = _research_context(conn, material.code, request.get("parent_report_id"))
            # Batch deep dives stay independent so cross-company comparison inputs keep their meaning.
            if not (request.get("parent_report_id") or request.get("feedback")
                    or run["batch_id"] is not None):
                if prior := _prior_report(conn, material.code, run_id):
                    material.extra["prior"] = prior
                    if not material.extra["question"]:
                        material.extra["question"] = prior_question(prior)
                if events := scan_events(conn, material, request.get("events") or []):
                    material.extra["events"] = events
                    if not material.extra["question"]:
                        material.extra["question"] = EVENT_QUESTION
            cutoff = run["started_at"] or conn.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            if batch := re.fullmatch(r"batch-(\d+)-.+", run["request_id"] or ""):
                batch_row = conn.execute("SELECT created_at FROM batches WHERE id=?", (int(batch[1]),)).fetchone()
                if batch_row:
                    cutoff = batch_row["created_at"]
            material.extra["method_catalog"] = run_method_catalog(
                conn, before=cutoff, batch=bool(batch) or run["batch_id"] is not None,
                cards_dir=os.environ.get("METHOD_CARDS_DIR"))
        identity["methods_sha256"] = active_methods_digest(conn, catalog=material.extra.get("method_catalog", []))
        db.update_run(conn, run_id, identity=json.dumps(identity))
        db.update_run(conn, run_id, code=material.code, name=material.name)
        source_sha = None
        if replay_of is not None:
            source_sha = conn.execute("SELECT json_extract(material, '$.sha256') FROM runs WHERE id=?",
                                      (replay_of,)).fetchone()[0]
        db.save_material(conn, run_id, material.snapshot())
        ledger = ReviewLedger()
        recorded = conn.execute("SELECT count(*) FROM calls WHERE run_id=?", (run_id,)).fetchone()[0]
        session = ResearchSession(conn, run_id, material, ledger, deadline=deadline,
                                  replay_sha256=source_sha,
                                  effective_model_call_limit=max(0, limit - recorded),
                                  profile_sha256=identity["profile_sha256"])
        runner_process.run_research(session, replay_of=replay_of)
        state = session.result()
        report, problems, reviewed = state["draft"], state["problems"], state["reviewed"]
        material_sha256 = db.save_material(conn, run_id, material.snapshot())
        period = max([material.doc.period] + [p for r in material.table if r["id"].startswith("T.") for p in r["values"]])
        body = {
            "code": material.code, "name": material.name, "period": period,
            "document": material.doc.info(),
            "pages": sorted(material.pages), "table": material.table, "market_date": material.market_date,
            "calls": conn.execute("SELECT count(*) FROM calls WHERE run_id=?", (run_id,)).fetchone()[0],
            "review_audit": ledger.snapshot(),
            "review_binding": {"draft_sha256": digest(report), "material_sha256": material_sha256,
                               "completed": reviewed,
                               "pending_issue_ids": [i["id"] for i in ledger.pending()]},
            "stop_reasons": material.extra.get("stop_reasons", []),
            "question": material.extra.get("question", ""), "parent_report_id": request.get("parent_report_id"),
            "sources": {k: {"document": v["document"], "pages": sorted(map(int, v["pages"]))}
                        for k, v in material.extra.get("sources", {}).items()},
            "gaps": material.extra.get("gaps", []) + (report.get("gaps", []) if isinstance(report.get("gaps"), list) else []),
            "tool_calls": len(material.extra.get("tools", [])),
            "announcement_searches": material.extra.get("announcement_searches", []),
            "methods_used": material.extra.get("methods_used", []),
            "replay_of": replay_of, "material_sha256": material_sha256,
        }
        if "series" in material.extra:
            body["series"] = material.extra["series"]
        if prior := material.extra.get("prior"):
            # Derived from the frozen question so replays, including of older snapshots, keep the label.
            body["prior"] = {"report_id": prior["report_id"], "period": prior["period"],
                             "question_added": material.extra.get("question") == prior_question(prior)}
        if events := material.extra.get("events"):
            body["events"] = events
        if (unanswered := open_questions(problems, reviewed)) is not None:
            # Shipped with the incomplete parts named; the ledger keeps them unresolved for audit.
            body["open_questions"] = [{"id": p["id"], "where": p["where"], "problem": p["problem"]} for p in unanswered]
            body["gaps"] = body["gaps"] + [f"复核认为尚未完成：{p['problem']}" for p in unanswered]
            problems = []
        if problems:
            body["failure"] = {"stage": "review" if reviewed or any(p.get("type") == "review_format" for p in problems) else "check",
                               "category": "review_format" if any(p.get("type") == "review_format" for p in problems)
                               else "source_input" if any(p.get("type") == "source_input" for p in problems)
                               else "generation_format" if any(p.get("type") == "generation_format" for p in problems)
                               else "substantive_review" if reviewed else "citation_calculation"}
            body |= {"facts": _verified_facts(report, problems, reviewed=reviewed),
                     "facts_reviewed": reviewed, "problems": problems}
        else:
            body |= published(report)
            if material.extra.get("feedback"):
                body["methods"] = report["methods"]
            body["answer"] = report.get("answer", [])
            if events:
                from a_share_research import scans
                # The per-event outcome and the sources actually read, judged from the final body.
                body["events"] = [e | {"evidence": scans.event_evidence(body, e)} for e in events]
            if "valuation" in report:
                with runner_process.time_left(deadline):
                    body["valuation_calc"] = calculate_valuation(report["valuation"], material, replay=replay_of is not None)
            if "stance" in report:
                body["stance_calc"] = stance_calc(report["stance"], body.get("valuation_calc"), material.market_date)
        publish(conn, run_id, material, period, not problems, None if problems else report["verdict"], body,
                judgment=model_profile.judgment_identity(identity))
    except Exception as exc:
        stage = session.stage if session is not None else "gather"
        if isinstance(exc, sources.SourceError | evidence.EvidenceError):
            log.warning("run %s source input failed at %s: %s", run_id, stage, market.redact_error(exc))
        elif not isinstance(exc, ScopeError | ResearchError | runner_process.RunnerError):
            log.exception("run %s failed", run_id)
        category = (exc.category if isinstance(exc, runner_process.RunnerError)
                    else "out_of_scope" if isinstance(exc, ScopeError)
                    else "source_input" if isinstance(exc, evidence.EvidenceError | sources.SourceError) or stage == "gather"
                    else "run_limit" if isinstance(exc, ResearchError)
                    else "pipeline_error")
        # Errors whose text this code writes itself are actionable and kept. SourceError can wrap provider
        # text, and model/transport exceptions can contain internal details; those get the generic notice.
        own = isinstance(exc, ScopeError | ResearchError | evidence.EvidenceError | runner_process.RunnerError) and str(exc)
        upstream = next((e for e in _causes(exc) if isinstance(e, TushareHTTPError)), None)
        failure = {"stage": stage, "category": category,
                   "message": str(exc) if own else str(upstream) if upstream
                   else "来源输入未能完成，请核对已保存资料。" if category == "source_input"
                   else "研究阶段未能完成；请核对阶段记录，不自动重试。"}
        if isinstance(exc, runner_process.RunnerError) and exc.kind:
            failure["kind"] = exc.kind
        if session is not None and session.failure:
            failure = session.failure
        identity["failure"] = failure
        if session is not None:
            identity["review_audit"] = session.ledger.snapshot()
        with db.transaction(conn):
            db.update_run(conn, run_id, status="failed", finished_at=db.now(), error=failure["message"],
                          identity=json.dumps(identity, ensure_ascii=False))
            if material is not None:
                db.save_material(conn, run_id, material.snapshot())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Research one or more companies and store the reports.")
    parser.add_argument("codes", nargs="*", help="6-digit codes; default: the eval set")
    parser.add_argument("--question", default="", help="a research question, also available on the web and API")
    parser.add_argument("--replay", type=int, nargs="+", metavar="RUN_ID",
                        help="reuse complete material snapshots from this database; never fetch fresh data")
    args = parser.parse_args()
    if args.replay and (args.codes or args.question):
        parser.error("codes/question and --replay cannot be combined")
    conn = db.connect()
    if args.replay:
        try:
            targets = [(replay_material(conn, run).code, run) for run in args.replay]
        except (ResearchError, KeyError, TypeError, ValueError) as exc:
            parser.error(str(exc))
    else:
        targets = [(code, None) for code in args.codes or EVAL_SET]
    run_ids = []
    for code, replay_of in targets:
        started = time.monotonic()
        run_id = db.create_run(conn, code, status="running")  # never 'queued', so a worker cannot also pick it up
        run_ids.append(run_id)
        if args.question:
            db.update_run(conn, run_id, request=json.dumps({"question": args.question}, ensure_ascii=False))
        execute(conn, run_id, replay_of=replay_of)
        run = conn.execute("SELECT status, error FROM runs WHERE id = ?", (run_id,)).fetchone()
        report = conn.execute("SELECT verdict, body FROM reports WHERE run_id = ?", (run_id,)).fetchone()
        body = report_body.load(report["body"]) if report else {}
        print(f"{code} run={run_id} {run['status']} {time.monotonic() - started:.0f}s "
              f"calls={body.get('calls')} {report['verdict'] if report else ''} "
              f"{run['error'] or body.get('summary', '')}", flush=True)
    eval_report.report_runs(run_ids)


if __name__ == "__main__":
    main()
