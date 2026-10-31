"""One company's financial-report research: gather → compose → check → review → (repair, at most twice) → save."""

import argparse
import json
import logging
import re
import time
from copy import deepcopy

from a_share_research import db, model, sources, evidence
from a_share_research.service import MAX_CALLS
from a_share_research.reviewing import ReviewLedger, ReviewFormatError, PROTOCOL, digest
from a_share_research.material import Material, ResearchError, ScopeError, gather, replay_material
from a_share_research.checking import SECTIONS, check
from a_share_research.prompts import RULES, output_schema, parse_json
from a_share_research.research_tools import Acquisition, explore

log = logging.getLogger(__name__)

EVAL_SET = ["600519", "000858", "600900", "601088", "000333", "002236", "603606", "600276", "002594", "600309", "600036"]


class ModelStageError(Exception):
    """Public exception deliberately excludes provider payloads and credentials."""


class Runner:
    def __init__(self, conn, run_id: int, *, max_calls: int = MAX_CALLS):
        self.conn, self.run_id, self.calls = conn, run_id, 0
        self.max_calls = min(max_calls, MAX_CALLS)
        self.deadline = time.monotonic() + 1800
        self.stage = "gather"
        self.chain = None  # (call id, full prompt) of the last tool-loop round, so the next stores only its additions
        self.material = None  # set once gathered; its prompt block is stored once however many prompts carry it

    def call(self, step: str, prompt: str, *, messages=None, tools=None):
        self.stage = step
        if self.calls >= self.max_calls:
            raise ResearchError("模型调用次数超过上限")
        timeout = int(min(600, self.deadline - time.monotonic()))
        if timeout <= 0:
            raise ResearchError("研究超过整体时限，已保存取得的材料")
        self.calls += 1
        db.update_run(self.conn, self.run_id, step=step)
        try:
            result = model.complete(prompt, timeout=timeout, max_tokens=16000, messages=messages, tools=tools)
        except Exception as exc:
            db.log_call(self.conn, self.run_id, step, prompt, error=f"{type(exc).__name__}: 模型阶段未完成（未保存供应商异常原文）",
                        identity=getattr(exc, "identity", None), seconds=getattr(exc, "seconds", None),
                        base=self.chain if messages is not None else None, block=self.block())
            raise ModelStageError("模型阶段未完成") from exc
        call_id = db.log_call(self.conn, self.run_id, step, prompt,
                              response=json.dumps(result.message, ensure_ascii=False) if messages is not None else result.text,
                              usage=result.usage, seconds=result.seconds, identity=result.identity,
                              base=self.chain if messages is not None else None, block=self.block())
        if messages is not None:
            self.chain = (call_id, prompt)
        log.info("run %s %s: %.0fs", self.run_id, step, result.seconds)
        return result

    def block(self) -> str | None:
        return self.material.prompt_block() if self.material is not None else None

    def ask(self, step: str, prompt: str) -> str:
        return self.call(step, prompt).text


def compose(runner: Runner, material: Material) -> dict | None:
    prompt = f"你是 A 股财报研究员，为普通投资者写一份研究报告。\n\n{RULES}\n\n{output_schema(material)}\n\n# 资料\n{material.prompt_block()}"
    return parse_json(runner.ask("compose", prompt))


def review(runner: Runner, material: Material, report: dict, ledger: ReviewLedger) -> list[dict]:
    prompt = f"""你是独立复核员。检查下面这份研究报告是否忠实于资料。

逐条检查：
1. 每条论断是否被它所引的出处支持：数字、期间、增减方向、主体是否正确，是否夸大或曲解。
2. 资料中是否有足以改变结论的重大风险或不利变化被遗漏（只依据资料判断，次要项不算）。
3. verdict 与 summary 是否和报告内容一致。
4. 有具体问题时，answer 是否回答了它；纠错是否核对了所指原文。
5. checkpoints 是对下一期的观察条件：只核查其基期事实（数字、期间、主体、单位）是否被出处支持、阈值是否来自出处或计算、条件是否与本报告的判断相关；不要求原文证明未来方向。
原文论断结合所引页的表头、行名和上下文核查。数字、主体、期间和含义已被所引页支持时，不因摘录未重复表头而判失败；截断引文造成曲解、或所引页不支持论断，仍须指出。
通读完整资料及报告，一次列全发现的实质问题，避免分轮逐项补报；不挑措辞、格式或条数。

{PROTOCOL}

# 当前复核绑定与必须逐项决定的旧问题/风险
{json.dumps(ledger.request(report, material.snapshot()["sha256"]), ensure_ascii=False)}

# 待复核的报告
{json.dumps(report, ensure_ascii=False, indent=1)}

# 资料
{material.prompt_block()}"""
    registry, material_sha256 = material.source_registry(), material.snapshot()["sha256"]
    try:
        return ledger.accept(parse_json(runner.ask("review", prompt)), report, registry, material.table, material_sha256)
    except ReviewFormatError as exc:
        # The ledger is unchanged on rejection; the reviewer gets the exact defect once, as with tool errors.
        prompt += f"\n\n# 上一次复核输出被拒\n{exc}\n请修正该处后重新输出完整 JSON 对象，其余判断不必因此改变。"
        return ledger.accept(parse_json(runner.ask("review", prompt)), report, registry, material.table, material_sha256)


def _review_issues(text: str) -> list[dict]:
    verdict = parse_json(text)
    if (verdict is None or not isinstance(verdict.get("issues"), list)
            or not all(isinstance(issue, dict)
                       and isinstance(issue.get("where"), str) and issue["where"].strip()
                       and isinstance(issue.get("problem"), str) and issue["problem"].strip()
                       for issue in verdict["issues"])):
        return [{"where": "review", "problem": "复核输出无法解析"}]
    return verdict["issues"]


REPAIR_SECTIONS = (*SECTIONS, "answer", "checkpoints", "methods")
PATCH_SCHEMA = """本轮只输出补丁 JSON，不输出完整报告。格式示例（所有内容及数字均为占位，须换成实际证据）：
{"ops": [{"op": "replace", "where": "risks[2]", "value": {"text": "修正后的论断", "cites": [{"ref": "实际行标识", "period": "实际期间键"}]}}, {"op": "delete", "where": "business[0]"}, {"op": "add", "where": "risks", "value": {"text": "遗漏的风险", "cites": [{"ref": "实际行标识"}]}}], "verdict": "需要谨慎", "summary": "需要谨慎：营业收入下降10%（数字占位）"}
ops 为数组，无需修改条目时可省略或用 []。replace/delete 的 where 必须是原报告的章节[序号]，序号从 0 开始；所有操作都按本轮原报告定位，删除不会改变其他操作的序号，同一原条目只能操作一次。
add 的 where 是章节名，按补丁顺序追加到末尾；可补建缺失的合法章节。合法章节仅 risks/business/financial/answer/checkpoints/methods；不能整段替换。value 是完整条目 {"text": "论断", "cites": [出处]}。
出处格式：原文 {"source": "实际文档标识", "page": 页码, "quote": "该页连续原文，逐字复制，20–200 字"}；表格/程序计算 {"ref": "实际行标识", "period": "实际期间键，跨期论断可省略"}。
verdict、summary、gaps 可在补丁顶层选择性替换：verdict 为值得继续研究/需要谨慎/暂不值得之一，summary 以 verdict 开头且带至少一个关键数字，gaps 为缺口字符串数组；未提到的字段与条目由程序逐字保留。
可选 rebuttals 数组，每项仅 {"id": "下方未解决问题的实际 ID", "reason": "据证反驳的理由", "evidence": [报告 cites 格式的实际出处]}；无 ID 的机械问题应修正对应条目。
程序将 rebuttals 放入合并稿的 repair_rebuttals，供独立复核员作为待核查数据阅读；它不是解决声明，不接受 status/resolutions/risk_resolutions，也不能关闭问题或豁免旧风险去向复核。
"""


class RepairFormatError(ValueError):
    pass


def merge_repair_patch(report: dict, patch: dict, problems: list[dict], material: Material) -> dict:
    """Apply an atomic patch against original positions without mutating either input."""
    if not isinstance(patch, dict) or set(patch) - {"ops", "verdict", "summary", "gaps", "rebuttals"}:
        raise RepairFormatError("补丁须为对象，只允许 ops、verdict、summary、gaps、rebuttals；不要输出完整报告")
    if not isinstance(patch.get("ops", []), list):
        raise RepairFormatError("补丁 ops 必须是数组；无需改动条目时可省略或用 []")
    replacements, additions = {}, {}
    for i, op in enumerate(patch.get("ops", [])):
        label = f"ops[{i}]："
        if not isinstance(op, dict) or op.get("op") not in ("replace", "delete", "add"):
            raise RepairFormatError(label + "op 必须是 replace/delete/add")
        kind, where = op["op"], op.get("where")
        keys = {"op", "where"} if kind == "delete" else {"op", "where", "value"}
        if not keys <= op.keys():
            raise RepairFormatError(label + f"{kind} 必须包含字段 {sorted(keys)}")
        match = re.fullmatch(r"([a-z]+)\[(0|[1-9]\d*)\]", where) if isinstance(where, str) else None
        section = where if kind == "add" else match[1] if match else None
        if not isinstance(section, str) or section not in REPAIR_SECTIONS:
            raise RepairFormatError(label + f"章节或位置不存在：{where}；replace/delete 用章节[序号]，add 用章节名；章节仅 {', '.join(REPAIR_SECTIONS)}")
        items = report.get(section, [])
        if kind == "add" and items is None:
            items = []
        if not isinstance(items, list):
            raise RepairFormatError(label + f"原报告 {section} 不是条目数组，不能按序号操作")
        if kind != "delete":
            item = op["value"]
            if not isinstance(item, dict):
                raise RepairFormatError(label + "value 必须是条目对象")
        if kind == "add":
            additions.setdefault(section, []).append(op["value"])
            continue
        index = int(match[2])
        if index >= len(items):
            raise RepairFormatError(label + f"{where} 越界；原报告 {section} 共 {len(items)} 条，序号从 0 开始；新增用 add")
        target = (section, index)
        if target in replacements:
            raise RepairFormatError(label + f"{where} 重复操作（包括既删又改）；同一原条目只允许一次 replace 或 delete")
        replacements[target] = None if kind == "delete" else op["value"]
    if "rebuttals" in patch:
        pending_ids = {p["id"] for p in problems if p.get("id")}
        seen = set()
        if not isinstance(patch["rebuttals"], list):
            raise RepairFormatError("rebuttals 必须是数组")
        for i, note in enumerate(patch["rebuttals"]):
            if (not isinstance(note, dict) or set(note) != {"id", "reason", "evidence"}
                    or not isinstance(note["id"], str) or note["id"] not in pending_ids or note["id"] in seen
                    or not isinstance(note["reason"], str) or not note["reason"].strip()
                    or not isinstance(note["evidence"], list) or not note["evidence"]
                    or not all(isinstance(cite, dict) for cite in note["evidence"])):
                raise RepairFormatError(f"rebuttals[{i}] 须含唯一的未解决问题 id、非空 reason、非空出处对象数组 evidence；不能自行指定解决状态")
            seen.add(note["id"])
    merged = deepcopy(report)
    merged.pop("repair_rebuttals", None)
    required = {*SECTIONS}
    if material.extra.get("question"):
        required.add("answer")
    if material.extra.get("feedback"):
        required.add("methods")
        if merged.get("methods") is None:
            merged["methods"] = []
    else:
        required.add("checkpoints")
    for section in {s for s, _ in replacements} | additions.keys():
        merged[section] = [deepcopy(replacements.get((section, i), item))
                           for i, item in enumerate(report.get(section) or [])
                           if (section, i) not in replacements or replacements[section, i] is not None]
        merged[section].extend(deepcopy(additions.get(section, [])))
        if not merged[section] and section not in required:
            merged.pop(section)
    merged.update({k: deepcopy(patch[k]) for k in ("verdict", "summary", "gaps") if k in patch})
    if patch.get("rebuttals"):
        merged["repair_rebuttals"] = deepcopy(patch["rebuttals"])
    return merged


def repair_result(report: dict | None, response: str, problems: list[dict], material: Material) -> dict | None:
    if report is None:
        return parse_json(response)

    patch = parse_json(response)
    if patch is None:
        raise RepairFormatError("补丁输出不是合法 JSON 对象")
    return merge_repair_patch(report, patch, problems, material)


def repair_prompt(material: Material, report: dict | None, problems: list[dict]) -> str:
    if report is None:
        output = "请修正后输出完整的新报告。\n" + output_schema(material)
        original = "（上一次输出不是合法 JSON）"
    else:
        output = PATCH_SCHEMA
        indexed = {key: {f"{key}[{i}]": item for i, item in enumerate(value)}
                   if key in REPAIR_SECTIONS and isinstance(value, list) else value
                   for key, value in report.items() if key != "repair_rebuttals"}
        original = json.dumps(indexed, ensure_ascii=False, indent=1)
    return f"""你是 A 股财报研究员。下面的研究报告被检查出问题，请按本轮输出格式修正。
可以改写、替换或删除有问题的条目；没有问题的条目保持原样。
补充遗漏风险时，保留原报告中已有依据的重大风险；可合并相关条目，不得用新风险替换另一项重大风险。
以下是内部质量检查，不是用户对另一份报告提出的纠错；正文只完成原研究任务。
下方未解决问题 ID 仅用于复核追踪，你不能自行把它们标为解决。可据证反驳错误批评；独立复核员将核对新版去向及理由。

{RULES}

{output}

# 问题
{json.dumps(problems, ensure_ascii=False, indent=1)}

# 原报告
{original}

# 资料
{material.prompt_block()}"""


def repair(runner: Runner, material: Material, report: dict | None, problems: list[dict], *, acquisition=None) -> dict | None:
    def response(original):
        prompt = repair_prompt(material, original, problems)
        if acquisition is None:
            return runner.ask("repair", prompt)  # Frozen replay never acquires evidence.
        prompt += """
# 补证与引用
先区分已有原文未引用、需要补读、仍无法取得的证据。已有页直接给支持主张的 cites；
缺页或跨期/同业口径不全时可用只读披露工具补证及 calculate，不凭表值伪造原文。
补证、补丁及格式失败后的完整改写共用剩余额度；工具失败或无新证据时明确缺口。
重要遗漏与用户要求不能靠一句“未知”自动关闭，仍须独立复核。按本轮输出格式作答。"""
        return json.dumps(acquisition.write(prompt, "repair"), ensure_ascii=False)

    try:
        return repair_result(report, response(report), problems, material)
    except RepairFormatError as exc:
        log.warning("Invalid repair patch; falling back to full rewrite: %s", exc)
        return parse_json(response(None))


# ---------- run ----------

def _verified_facts(report: dict | None, problems: list[dict], *, reviewed: bool = False) -> dict:
    """Retain unflagged claims only after semantic review of this exact report completed."""
    if not report or not reviewed or any(p["where"] == "review" for p in problems):
        return {}
    # A carried issue retains its original target. Reordering/replacing a draft can move that
    # disputed claim; without a new position mapping no subset is safe to endorse.
    if any(p.get("id") and p.get("draft_sha256") != digest(report) for p in problems):
        return {}
    flagged = set()
    for problem in problems:
        targets = set()
        for location in problem["where"].split("/"):
            location = location.strip()
            if location == "missing":
                continue
            match = re.fullmatch(r"(risks|business|financial|answer|checkpoints)(?:\[(\d+)\])?", location)
            if not match:
                return {}
            section, index = match.groups()
            items = report.get(section)
            if not isinstance(items, list) or not items or (index is not None and int(index) >= len(items)):
                return {}
            targets.add(section if index is None else f"{section}[{int(index)}]")
        # A global or unlocatable finding cannot establish which claims were unaffected.
        if not targets:
            return {}
        flagged.update(targets)
    facts = {
        section: [item for i, item in enumerate(report.get(section) or [])
                  if isinstance(item, dict) and section not in flagged and f"{section}[{i}]" not in flagged]
        for section in (*SECTIONS, "answer")
    }
    return facts if any(facts.values()) else {}


def _research_context(conn, code: str, parent_id: int | None) -> list[dict]:
    parents = conn.execute(
        "SELECT r.id, r.body, r.passed, u.request FROM reports r JOIN runs u ON u.id = r.run_id"
        " WHERE r.code = ? AND (r.id = ? OR (r.passed = 1 AND u.status = 'done'"
        " AND json_type(u.request, '$.feedback') = 'object')) ORDER BY (r.id = ?) DESC, r.id DESC LIMIT 3",
        (code, parent_id, parent_id),
    ).fetchall()
    context = []
    for parent in parents:
        body = json.loads(parent["body"])
        content = body if parent["passed"] else body.get("facts", {})
        claims = {k: content.get(k) for k in ("summary", "answer", "methods", "checkpoints", *SECTIONS)}
        # Retired methods must not return through automatic same-company context.
        if parent["id"] != parent_id:
            claims["methods"] = None
        context.append({"report_id": parent["id"], "request": json.loads(parent["request"] or "{}"),
                        "basis": "report" if parent["passed"] else "facts",
                        "claims_to_recheck": claims})
    return context


def _prior_report(conn, code: str, run_id: int) -> dict | None:
    """The company's latest reviewed, non-replay, non-feedback report before this run, as an agenda to recheck.

    Only its risks and checkpoints travel: no verdict or summary, so the new judgment starts from evidence.
    """
    row = conn.execute(
        "SELECT r.id, r.body, r.created_at, r.coverage FROM reports r JOIN runs u ON u.id = r.run_id"
        " WHERE r.code = ? AND r.run_id < ? AND r.passed = 1 AND u.status = 'done'"
        " AND u.replay_of IS NULL AND json_type(u.request, '$.feedback') IS NOT 'object'"
        " ORDER BY r.id DESC LIMIT 1", (code, run_id),
    ).fetchone()
    if row is None:
        return None
    body = json.loads(row["body"])
    return {"report_id": row["id"], "researched": row["created_at"][:10], "period": row["coverage"],
            "market_date": body.get("market_date"),
            "risks": [item["text"] for item in body.get("risks", [])],
            "checkpoints": [item["text"] for item in body.get("checkpoints", [])]}


def prior_question(prior: dict) -> str:
    """The question a continuation asks when the user brought none."""
    return f"相对上次研究（资料至 {prior['period']}），哪些风险与检查点发生了变化？"


def publish(conn, run_id: int, material: Material, period: str, passed: bool,
            verdict: str | None, body: dict) -> None:
    """Save the report and finish its run together, so a crash never leaves one without the other."""
    with db.transaction(conn):
        db.save_report(conn, run_id, material.code, material.name, period, passed, verdict, body)
        if passed:
            db.update_run(conn, run_id, status="done", step=None, finished_at=db.now())
        else:
            db.update_run(conn, run_id, status="failed", step=None, finished_at=db.now(),
                          error="研究核查未通过，不展示结论")


def revise(runner, material, report, ledger, *, acquisition=None):
    """Check and independently review each draft; keep one ledger across evidence additions."""
    problems: list[dict] = []
    # Repairs may add evidence within the original session's remaining limits.
    # Every check and review binds to the current persisted material, never the first draft's hash.
    for attempt in range(3):
        reviewed = False  # A changed draft never inherits an earlier independent endorsement.
        runner.stage = "check"
        problems = check(report, material) if report else [{"where": "report", "problem": "输出不是合法 JSON",
                                                             "type": "generation_format"}]
        if any(p.get("type") == "source_input" for p in problems):
            problems += ledger.pending()
            break  # Source identity conflicts cannot be repaired by rewriting prose.
        if not problems:
            try:
                problems = review(runner, material, report, ledger)
                reviewed = True
            except ReviewFormatError as exc:
                problems = ledger.pending() + [{"where": "review", "problem": str(exc), "type": "review_format"}]
                break  # A malformed reviewer cannot be fixed by asking the writer again.
        else:
            problems += ledger.pending()
        if not problems or attempt == 2:
            break
        ledger.protect_risks(report)
        report = repair(runner, material, report, problems, acquisition=acquisition)
    return report, problems, reviewed


def execute(conn, run_id: int, *, replay_of: int | None = None) -> None:
    run = conn.execute("SELECT code, request, request_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    code, request = run["code"], json.loads(run["request"] or "{}")
    db.update_run(conn, run_id, status="running", step="gather", started_at=db.now())
    runner = Runner(conn, run_id)
    identity: dict = {}

    try:
        identity = model.application_identity() | {"replay_of": replay_of, "material_sha256": None}
        db.update_run(conn, run_id, identity=json.dumps(identity))
        material = replay_material(conn, replay_of) if replay_of is not None else gather(code)
        if material.code[:6] != code[:6]:
            raise ResearchError("重放材料与目标公司不一致")
        if replay_of is None:
            material.extra["question"] = request.get("question", "")
            material.extra["feedback"] = request.get("feedback")
            material.extra["context"] = _research_context(conn, material.code, request.get("parent_report_id"))
            # Batch deep dives stay independent so cross-company comparison inputs keep their meaning.
            if not (request.get("parent_report_id") or request.get("feedback")
                    or (run["request_id"] or "").startswith("batch-")):
                if prior := _prior_report(conn, material.code, run_id):
                    material.extra["prior"] = prior
                    if not material.extra["question"]:
                        material.extra["question"] = prior_question(prior)
        db.update_run(conn, run_id, code=material.code, name=material.name)
        db.save_material(conn, run_id, material.snapshot())
        runner.material = material
        acquisition = None
        if replay_of is None:
            acquisition = Acquisition(runner, material)
            report = explore(runner, material, acquisition=acquisition)
        else:
            report = compose(runner, material)
        ledger = ReviewLedger()
        report, problems, reviewed = revise(runner, material, report, ledger, acquisition=acquisition)
        material_sha256 = db.save_material(conn, run_id, material.snapshot())
        period = max([material.doc.period] + [p for r in material.table if r["id"].startswith("T.") for p in r["values"]])
        body = {
            "code": material.code, "name": material.name, "period": period,
            "document": material.doc.info(),
            "pages": sorted(material.pages), "table": material.table, "market_date": material.market_date,
            "calls": runner.calls,
            "review_audit": ledger.snapshot(),
            "review_binding": {"draft_sha256": digest(report), "material_sha256": material_sha256,
                               "completed": reviewed,
                               "pending_issue_ids": [i["id"] for i in ledger.pending()]},
            "stop_reasons": material.extra.get("stop_reasons", []),
            "question": material.extra.get("question", ""), "parent_report_id": request.get("parent_report_id"),
            "sources": {k: {"document": v["document"], "pages": sorted(map(int, v["pages"]))}
                        for k, v in material.extra.get("sources", {}).items()},
            "gaps": material.extra.get("gaps", []) + (report.get("gaps", []) if report and isinstance(report.get("gaps", []), list) else []),
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
        if problems:
            body["failure"] = {"stage": "review" if reviewed or any(p.get("type") == "review_format" for p in problems) else "check",
                               "category": "review_format" if any(p.get("type") == "review_format" for p in problems)
                               else "source_input" if any(p.get("type") == "source_input" for p in problems)
                               else "generation_format" if any(p.get("type") == "generation_format" for p in problems)
                               else "substantive_review" if reviewed else "citation_calculation"}
            body |= {"facts": _verified_facts(report, problems, reviewed=reviewed),
                     "facts_reviewed": reviewed, "problems": problems}
            publish(conn, run_id, material, period, False, None, body)
        else:
            body |= {k: report[k] for k in ("verdict", "summary", *SECTIONS)}
            if "checkpoints" in report:
                body["checkpoints"] = report["checkpoints"]
            if material.extra.get("feedback"):
                body["methods"] = report["methods"]
            body["answer"] = report.get("answer", [])
            publish(conn, run_id, material, period, True, report["verdict"], body)
    except Exception as exc:
        if not isinstance(exc, ScopeError | ResearchError | ModelStageError | sources.SourceError | evidence.EvidenceError):
            log.exception("run %s failed", run_id)
        category = ("out_of_scope" if isinstance(exc, ScopeError)
                    else "source_input" if isinstance(exc, evidence.EvidenceError | sources.SourceError) or runner.stage == "gather"
                    else "run_limit" if isinstance(exc, ResearchError)
                    else "model_error" if isinstance(exc, ModelStageError) else "pipeline_error")
        # Errors whose text this code writes itself are actionable and kept. SourceError can wrap provider
        # text, and model/transport exceptions can contain internal details; those get the generic notice.
        own = isinstance(exc, ScopeError | ResearchError | evidence.EvidenceError) and str(exc)
        failure = {"stage": runner.stage, "category": category,
                   "message": str(exc) if own else "来源输入未能完成，请核对已保存资料。" if category == "source_input"
                   else "研究阶段未能完成；请核对阶段记录，不自动重试。"}
        db.update_run(conn, run_id, status="failed", finished_at=db.now(), error=failure["message"])
        identity["failure"] = failure
        if "ledger" in locals():
            identity["review_audit"] = ledger.snapshot()
        db.update_run(conn, run_id, identity=json.dumps(identity, ensure_ascii=False))
        if "material" in locals():
            # Diagnostics belong to run metadata, not the evidence hashed by the reviewer.
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
    for code, replay_of in targets:
        started = time.monotonic()
        run_id = db.create_run(conn, code, status="running")  # never 'queued', so a worker cannot also pick it up
        if args.question:
            db.update_run(conn, run_id, request=json.dumps({"question": args.question}, ensure_ascii=False))
        execute(conn, run_id, replay_of=replay_of)
        run = conn.execute("SELECT status, error FROM runs WHERE id = ?", (run_id,)).fetchone()
        report = conn.execute("SELECT verdict, body FROM reports WHERE run_id = ?", (run_id,)).fetchone()
        body = json.loads(report["body"]) if report else {}
        print(f"{code} run={run_id} {run['status']} {time.monotonic() - started:.0f}s "
              f"calls={body.get('calls')} {report['verdict'] if report else ''} "
              f"{run['error'] or body.get('summary', '')}", flush=True)


if __name__ == "__main__":
    main()
