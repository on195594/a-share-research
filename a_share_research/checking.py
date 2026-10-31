"""Mechanical report checks: structure, citations and figures against the material."""

import re
from decimal import Decimal

from a_share_research import evidence
from a_share_research.evidence import numbers as _numbers, supported as _supported
from a_share_research.material import Material

VERDICTS = ("值得继续研究", "需要谨慎", "暂不值得")
SECTIONS = {"risks": "主要风险与反证", "business": "业务", "financial": "财务"}
REF_IN_TEXT = re.compile(r"(?<![A-Za-z])(?:[TM]\.[a-z_]+|C\.\d+|P\d{6}\.[TM]\.[a-z_]+)")


def _without_codes(text: str, material: Material) -> str:
    """Stock codes are identifiers, not figures: drop them rather than accept them as supported values."""
    codes = {material.code[:6]} | {r["id"][1:7] for r in material.table if re.match(r"^P\d{6}\.", r["id"])}
    codes = sorted(c for c in codes if c.isdigit())
    return re.sub(r"(?<!\d)(?:" + "|".join(codes) + r")(?!\d)", " ", text) if codes else text


REPORT_ID = re.compile(r"(?:报告|report)\s*#?\s*\d+", re.I)
QUESTION_THRESHOLD = re.compile(
    r"(?:阈值|门槛|低于|高于|不低于|不高于|小于|大于|不小于|不大于|超过|跌破|突破|[<>≤≥]=?)\s*"
    r"(?P<value>[-−+]?\d[\d,]*(?:\.\d+)?)(?![\d.,])\s*"
    r"(?P<unit>(?:万亿|亿|万)?元|个百分点|[%％]|倍|天|(?!\s*[\w%％]))")


def _threshold_key(match: re.Match) -> tuple[Decimal, str]:
    # Preserve sign and unit; no rounding tolerance or unit conversion grants an exemption.
    return (Decimal(match["value"].replace(",", "").replace("−", "-")),
            (match["unit"] or "").replace("％", "%"))


def _checked_figures(text: str, material: Material) -> list[tuple[float, int]]:
    """Only the explicit threshold occurrence is a question term; equal factual amounts still need sources."""
    terms = {_threshold_key(m) for m in QUESTION_THRESHOLD.finditer(str(material.extra.get("question") or ""))}
    text = QUESTION_THRESHOLD.sub(lambda m: " " if _threshold_key(m) in terms else m[0], text)
    return _numbers(_without_codes(REPORT_ID.sub(" ", text), material))


def _candidates(cites: list[dict], material: Material) -> list[float]:
    rows = {r["id"]: r for r in material.table}
    values = []
    for cite in cites:
        if "ref" in cite and cite["ref"] in rows:
            row = rows[cite["ref"]]
            for fld in ("values", "exact_values"):
                cited = row.get(fld, {})
                values += [float(v) for p, v in cited.items()
                           if not cite.get("period") or cite["period"] == p or row["id"].startswith("C.")]
            calc = row.get("calculation") or {}
            for group in ("inputs", "normalized_inputs"):
                values += [float(i["value"]) for i in calc.get(group, []) if "value" in i]
        elif "quote" in cite:
            values += [v for v, _ in _numbers(cite["quote"])]
    return values


def quote_found(cite: dict, material: Material) -> bool:
    return bool(evidence.quote_match(cite, material.source_registry()))


def check(report: dict, material: Material) -> list[dict]:
    """Structural, citation and number checks; returns problems as {where, problem}."""
    problems = []
    try:
        registry = material.source_registry()
    except evidence.EvidenceError as exc:
        return [{"where": "sources", "problem": str(exc), "type": "source_input"}]
    if not isinstance(report.get("gaps", []), list) or not all(isinstance(g, str) for g in report.get("gaps", [])):
        problems.append({"where": "gaps", "problem": "gaps 必须是字符串数组"})
    if report.get("verdict") not in VERDICTS:
        problems.append({"where": "verdict", "problem": f"verdict 必须是 {VERDICTS} 之一"})
    summary = str(report.get("summary", ""))
    if not summary.startswith(tuple(VERDICTS)):
        problems.append({"where": "summary", "problem": "summary 必须以结论词开头"})
    if REF_IN_TEXT.search(summary):
        problems.append({"where": "summary", "problem": "summary 中不要写出处编号"})
    all_cites = []
    sections = [*SECTIONS]
    if material.extra.get("question") or report.get("answer"):
        sections.append("answer")
    if material.extra.get("feedback") or "methods" in report:
        sections.append("methods")
    if not material.extra.get("feedback") or "checkpoints" in report:
        sections.append("checkpoints")
    for section in sections:
        items = report.get(section)
        if not isinstance(items, list) or (not items and section != "methods"):
            problems.append({"where": section, "problem": "该段缺失或为空"})
            continue
        for i, item in enumerate(items):
            where = f"{section}[{i}]"
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                problems.append({"where": where, "problem": "条目必须包含 text 和 cites"})
                continue
            cites = item.get("cites") or []
            if not isinstance(cites, list) or not all(isinstance(c, dict) for c in cites):
                problems.append({"where": where, "problem": "cites 必须是出处对象数组"})
                continue
            if REF_IN_TEXT.search(str(item.get("text", ""))):
                problems.append({"where": where, "problem": "text 中不要写出处编号"})
            if not cites:
                problems.append({"where": where, "problem": "没有出处"})
            errors = evidence.citation_errors(cites, registry, material.table)
            problems += [{"where": where, "problem": error, "type": "citation_calculation"} for error in errors]
            if errors:
                continue
            candidates = _candidates(cites, material)
            for value, decimals in _checked_figures(str(item.get("text", "")), material):
                if not _supported(value, decimals, candidates):
                    problems.append({"where": where, "problem": f"数字 {value:g} 在所引出处中找不到"})
            all_cites += cites
    # A full rewrite is raw model output, so the key may be echoed back in any shape.
    for i, note in enumerate(report.get("repair_rebuttals") or []):
        errors = evidence.citation_errors(note.get("evidence") if isinstance(note, dict) else None,
                                          registry, material.table)
        problems += [{"where": f"repair_rebuttals[{i}]", "problem": error, "type": "citation_calculation"}
                     for error in errors]
    candidates = _candidates(all_cites, material) + [float(v) for r in material.table
                                                   for fld in ("values", "exact_values")
                                                   for v in r.get(fld, {}).values()]
    for value, decimals in _checked_figures(summary, material):
        if not _supported(value, decimals, candidates):
            problems.append({"where": "summary", "problem": f"数字 {value:g} 在报告出处与表中找不到"})
    return problems
