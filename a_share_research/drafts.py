"""Draft presentation, atomic repairs and projections for reviewed delivery."""
import json
import re
from copy import deepcopy

from a_share_research import report as report_body, valuation
from a_share_research.checking import SECTIONS
from a_share_research.material import Material
from a_share_research.prompts import render_prompt
from a_share_research.reviewing import digest


def program_valuation(report: dict | None, material: Material) -> str:
    """The valuation the report page shows for this draft, so judgments about price use the program's numbers."""
    summary = valuation.program_summary((report or {}).get("valuation"), material.table, material.market_date)
    if summary is None:
        return ""
    return render_prompt("program_valuation.j2", summary=json.dumps(summary, ensure_ascii=False))


REPAIR_SECTIONS = (*SECTIONS, "answer", "checkpoints", "methods", "prior_status")


def published(report: dict) -> dict:
    """Fields of a passed report that are saved; optional judgment fields only when the contract produced them."""
    return {k: report[k] for k in report_body.PUBLISHED_REQUIRED_FIELDS} | {
        k: report[k] for k in report_body.PUBLISHED_FIELDS if k not in report_body.PUBLISHED_REQUIRED_FIELDS and k in report}


class RepairFormatError(ValueError):
    pass


def merge_repair_patch(report: dict, patch: dict, problems: list[dict], material: Material) -> dict:
    """Apply an atomic patch against original positions without mutating either input."""
    if not isinstance(patch, dict) or set(patch) - {"ops", "verdict", "summary", "gaps", "valuation", "stance", "rebuttals"}:
        raise RepairFormatError("补丁须为对象，只允许 ops、verdict、summary、gaps、valuation、stance、rebuttals；不要输出完整报告")
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
        match = re.fullmatch(r"([a-z_]+)\[(0|[1-9]\d*)\]", where) if isinstance(where, str) else None
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
    if material.extra.get("prior"):
        required.add("prior_status")
    for section in {s for s, _ in replacements} | additions.keys():
        merged[section] = [deepcopy(replacements.get((section, i), item))
                           for i, item in enumerate(report.get(section) or [])
                           if (section, i) not in replacements or replacements[section, i] is not None]
        merged[section].extend(deepcopy(additions.get(section, [])))
        if not merged[section] and section not in required:
            merged.pop(section)
    merged.update({k: deepcopy(patch[k]) for k in ("verdict", "summary", "gaps", "valuation", "stance") if k in patch})
    if patch.get("rebuttals"):
        merged["repair_rebuttals"] = deepcopy(patch["rebuttals"])
    return merged


def indexed_draft(report: dict) -> dict:
    return {key: {f"{key}[{i}]": item for i, item in enumerate(value)}
            if key in REPAIR_SECTIONS and isinstance(value, list) else value
            for key, value in report.items() if key != "repair_rebuttals"}


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
            match = re.fullmatch(r"(risks|business|financial|answer|checkpoints|prior_status)(?:\[(\d+)\])?", location)
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


# Where an unanswered part of the user's question can stay open without withholding the verified rest.
OPEN_QUESTION_WHERE = re.compile(r"answer\[\d+\]|prior_status\[\d+\]|valuation|missing|gaps\[\d+\]")


def open_questions(problems: list[dict], reviewed: bool) -> list[dict] | None:
    """Unresolved review issues that may ship as named gaps, or None when any of them must block.

    Only a completed review of the final draft qualifies, and only `question` issues about the answer,
    a prior item's status, the scenario valuation, or evidence still missing: the reviewer found the work
    incomplete, not wrong.
    A disputed claim, an omitted material risk, or doubt about a fact, verdict or stance still blocks."""
    if not problems or not reviewed:
        return None
    if all(p.get("id") and p.get("type") == "question" and OPEN_QUESTION_WHERE.fullmatch(str(p.get("where")))
           for p in problems):
        return problems
    return None
