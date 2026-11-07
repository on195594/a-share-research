"""Mechanical report checks: structure, citations and figures against the material."""

import re
from decimal import Decimal

from a_share_research import agenda, evidence, forecasts, stance as stances, stock_codes, valuation
from a_share_research.evidence import numbers as _numbers, supported as _supported
from a_share_research.material import Material

VERDICTS = ("值得继续研究", "需要谨慎", "暂不值得")
SECTIONS = {"risks": "主要风险与反证", "business": "业务", "financial": "财务"}
REF_IN_TEXT = re.compile(r"(?<![A-Za-z])(?:[TM]\.[a-z_]+|C\.\d+|P\d{6}\.[TM]\.[a-z_]+)")


def _without_codes(text: str, material: Material) -> str:
    """Stock codes are identifiers, not figures: drop them rather than accept them as supported values."""
    codes = {stock_codes.normalize_key(material.code)} | {r["id"][1:7] for r in material.table if re.match(r"^P\d{6}\.", r["id"])}
    codes = sorted(c for c in codes if c.isdigit())
    return re.sub(r"(?<!\d)(?:" + "|".join(codes) + r")(?!\d)", " ", text) if codes else text


WINDOW_LIST = re.compile(r"(?<![\d.])(\d+(?:\s*[、,，/和及]\s*\d+)*)\s*个?交易日")
INDEX_NAME = re.compile(r"(?:中证|沪深|上证|深证|国证|创业板|科创)\d+")
REPORT_ID = re.compile(r"(?:报告|report)\s*#?\s*\d+", re.I)
# Page references ("第11—12页") and product/project codes ("HDM1002", "GLP-1") are identifiers, not figures.
# Currency prefixes stay figures so "RMB100亿" still needs a source.
PAGE_REF = re.compile(r"第\s*\d+(?:\s*[、,，和及至~—–-]+\s*\d+)*\s*页")
# A code has at least two letters, is not a currency or ratio label, and is not followed by a unit.
CODE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9.])(?!(?:RMB|CNY|USD|US|HKD|HK|EUR|JPY|GBP|ROE|ROA|ROIC|PE|PB|PS|EPS|BPS|DPS|TTM)-?\d)"
    r"[A-Za-z]{2,}-?\d[A-Za-z0-9-]*(?![.\d]|\s*(?:%|％|倍|(?:万亿|亿|万)?元|个百分点|美元|港元))", re.I)
_VALUE = r"(?P<value>[-−+]?\d[\d,]*(?:\.\d+)?)(?![\d.,])\s*"
_UNIT = r"(?P<unit>(?:万亿|亿|万)?元|个百分点|[%％]|倍|天|(?!\s*[\w%％]))"
QUESTION_THRESHOLD = re.compile(
    r"(?:阈值|门槛|低于|高于|不低于|不高于|小于|大于|不小于|不大于|超过|跌破|突破|[<>≤≥]=?)\s*" + _VALUE + _UNIT)
# The same threshold written with a trailing comparative: "保持在150亿元以上".
TRAILING_THRESHOLD = re.compile(_VALUE + r"(?P<unit>(?:万亿|亿|万)?元|个百分点|[%％]|倍|天)?\s*(?:以上|以下|之上|之下)")


def _threshold_key(match: re.Match) -> tuple[Decimal, str]:
    # Preserve sign and unit; no rounding tolerance or unit conversion grants an exemption.
    return (Decimal(match["value"].replace(",", "").replace("−", "-")),
            (match["unit"] or "").replace("％", "%"))


def _checked_figures(text: str, material: Material, cites=()) -> list[tuple[float, int]]:
    """Only the explicit threshold occurrence is a question term; equal factual amounts still need sources."""
    # Session counts are measurement windows, not financial values. Derive them from
    # the cited calculation's actual stored daily series; never whitelist the numeral.
    rows = {r["id"]: r for r in material.table}
    windows = set()
    pending = [c.get("ref") for c in cites if isinstance(c.get("ref"), str)]
    visited = set()
    while pending:
        ref = pending.pop()
        if ref in visited:
            continue
        visited.add(ref)
        row = rows.get(ref, {})
        source = row.get("source", {})
        if source.get("api") == "industry_constituents" and source.get("window"):
            sessions = source.get("sessions", [])
            if (len(sessions) == source["window"] + 1 and sessions[0] == source["start"]
                    and sessions[-1] == source["end"]):
                windows.add(source["window"])
        calc = row.get("calculation", {})
        pending.extend(i["ref"] for i in calc.get("inputs", []) if "ref" in i)
        inputs = calc.get("inputs", [])
        if calc.get("operation") != "change_pct" or len(inputs) != 2:
            continue
        left, right = inputs
        row = rows.get(left.get("ref"), {})
        if left.get("ref") != right.get("ref") or row.get("source", {}).get("api") != "fut_daily":
            continue
        days = sorted(row.get("values", {}))
        if left.get("period") in days and right.get("period") in days:
            windows.add(days.index(left["period"]) - days.index(right["period"]))
    # A list such as "近20、60、120个交易日" names several windows at once; drop only listed numerals
    # that are windows of the cited rows, so a stray figure in the list still needs a source.
    def drop_windows(match: re.Match) -> str:
        rest = [n for n in re.findall(r"\d+", match[1]) if int(n) not in windows]
        return " " + "、".join(rest) + "个交易日" if rest else " "

    text = WINDOW_LIST.sub(drop_windows, text)
    # Index names ("中证800") are identifiers when a cited row in the chain is measured against that index.
    names = {name for ref in visited for name in INDEX_NAME.findall(str(rows.get(ref, {}).get("label", "")))}
    if names:
        text = re.sub("(?:" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")(?![\d.])", " ", text)
    question = str(material.extra.get("question") or "")
    terms = {_threshold_key(m) for pattern in (QUESTION_THRESHOLD, TRAILING_THRESHOLD) for m in pattern.finditer(question)}
    for pattern in (QUESTION_THRESHOLD, TRAILING_THRESHOLD):
        text = pattern.sub(lambda m: " " if _threshold_key(m) in terms else m[0], text)
    text = CODE_TOKEN.sub(" ", PAGE_REF.sub(" ", REPORT_ID.sub(" ", text)))
    return _numbers(_without_codes(text, material))


def _assumed(row: dict) -> bool:
    return bool((row.get("calculation") or {}).get("assumptions"))


def _candidates(cites: list[dict], material: Material, *, facts_only=False) -> list[float]:
    """Values the cites can support; `facts_only` drops results and operands that rest on explicit assumptions."""
    rows = {r["id"]: r for r in material.table}
    registry = None
    values = []
    for cite in cites:
        if "ref" in cite and cite["ref"] in rows:
            row = rows[cite["ref"]]
            calc = row.get("calculation") or {}
            if not (facts_only and _assumed(row)):
                for fld in ("values", "exact_values"):
                    cited = row.get(fld, {})
                    values += [float(v) for p, v in cited.items()
                               if not cite.get("period") or cite["period"] == p or row["id"].startswith("C.")]
            inputs = calc.get("inputs", [])
            normalized = calc.get("normalized_inputs") or [{} for _ in inputs]
            for raw, norm in zip(inputs, normalized):
                if facts_only and ("assumption" in raw or raw.get("assumptions")):
                    continue
                values += [float(i["value"]) for i in (raw, norm) if "value" in i]
            if not facts_only:
                values += [float(i["value"]) for i in calc.get("assumptions", []) if "value" in i]
        elif "quote" in cite:
            values += [v for v, _ in _numbers(cite["quote"])]
            registry = registry or material.source_registry()
            values += [float(v) for v in evidence.rejoined_numbers(cite, registry)]
    return values


def _stale_valuation(cites: list[dict], material: Material, value) -> list[str]:
    """Program valuation rows (directly or through a calculation) must price this draft's scenarios."""
    rows = {r["id"]: r for r in material.table}
    current = valuation.pricing_inputs(value)
    stale, seen = [], set()
    pending = [c.get("ref") for c in cites if isinstance(c.get("ref"), str)]
    while pending:
        ref = pending.pop()
        if ref in seen or ref not in rows:
            continue
        seen.add(ref)
        calc = rows[ref].get("calculation") or {}
        if calc.get("operation") == valuation.PROGRAM_OPERATION and calc.get("valuation") != current:
            stale.append(ref)
        pending.extend(i["ref"] for i in calc.get("inputs", []) if "ref" in i)
    return sorted(stale)


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
    if "checkpoints" in sections and not material.extra.get("feedback"):
        if problem := forecasts.count_problem(report.get("checkpoints")):
            problems.append({"where": "checkpoints", "problem": problem})
    item_sections = [(section, report.get(section)) for section in sections]
    prior_status = report.get("prior_status")
    if prior := material.extra.get("prior"):
        problems += agenda.problems(prior_status, prior)
        # An empty agenda legitimately yields []; structure was just checked above.
        if isinstance(prior_status, list) and prior_status:
            item_sections.append(("prior_status", prior_status))
    elif "prior_status" in report:
        problems.append({"where": "prior_status", "problem": "本次没有旧议程（无 prior），不应输出 prior_status"})
    value = report.get("valuation")
    if not material.extra.get("feedback") or value is not None:
        problems += [{"where": "valuation", "problem": p} for p in valuation.problems(value, material.table)]
    if isinstance(value, dict):
        if "not_priced" in value:
            item_sections.append(("valuation.not_priced", [value["not_priced"]]))
        else:
            item_sections.append(("valuation.rationale", value.get("rationale")))
    stance = report.get("stance")
    if not material.extra.get("feedback") or stance is not None:
        problems += [{"where": "stance", "problem": p}
                     for p in stances.problems(stance, material.table, value, material.market_date)]
    if isinstance(stance, dict):
        item_sections.append(("stance.text", [stance]))
    for section, items in item_sections:
        whole = section.split(".")[0] if "." in section else None  # one-object fields: valuation, stance
        if not isinstance(items, list) or (not items and section != "methods"):
            problems.append({"where": whole or section, "problem": f"{section} 该段缺失或为空"})
            continue
        for i, item in enumerate(items):
            where = whole or f"{section}[{i}]"
            if (not isinstance(item, dict) or not isinstance(item.get("text"), str)
                    or whole and not item["text"].strip()):
                problems.append({"where": where, "problem": "条目必须包含 text 和 cites"})
                continue
            cites = item.get("cites") or []
            if not isinstance(cites, list) or not all(isinstance(c, dict) for c in cites):
                problems.append({"where": where, "problem": "cites 必须是出处对象数组"})
                continue
            if REF_IN_TEXT.search(str(item.get("text", ""))):
                problems.append({"where": where, "problem": "text 中不要写出处编号"})
            status = item.get("status")
            if not cites and not (section == "prior_status" and isinstance(status, str) and status in agenda.NO_EVIDENCE):
                problems.append({"where": where, "problem": "没有出处"})
            if section == "checkpoints" and "forecast" in item:
                problems += [{"where": where, "problem": p} for p in forecasts.problems(item["forecast"], material.table)]
            errors = evidence.citation_errors(cites, registry, material.table)
            problems += [{"where": where, "problem": error, "type": "citation_calculation"} for error in errors]
            if errors:
                continue
            if stale := _stale_valuation(cites, material, report.get("valuation")):
                problems.append({"where": where, "type": "citation_calculation", "problem":
                                 f"{'、'.join(stale)} 按另一组估值情景测算，与本稿 valuation 的 basis/years/scenarios/payout_pct 不一致；"
                                 "请按最终 valuation 重新调用 valuation 工具并引用新行"})
                continue
            text = str(item.get("text", ""))
            candidates = _candidates(cites, material)
            for value, decimals in _checked_figures(text, material, cites):
                if not _supported(value, decimals, candidates):
                    problems.append({"where": where, "problem": f"数字 {value:g} 在所引出处中找不到"})
            problems += [{"where": where, "problem": p}
                         for p in _unmarked_assumptions(text, material, cites, candidates, _candidates(cites, material, facts_only=True))]
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
    for value, decimals in _checked_figures(summary, material, all_cites):
        if not _supported(value, decimals, candidates):
            problems.append({"where": "summary", "problem": f"数字 {value:g} 在报告出处与表中找不到"})
    facts = _candidates(all_cites, material, facts_only=True) + [float(v) for r in material.table if not _assumed(r)
                                                                for fld in ("values", "exact_values")
                                                                for v in r.get(fld, {}).values()]
    problems += [{"where": "summary", "problem": p}
                 for p in _unmarked_assumptions(summary, material, all_cites, candidates, facts)]
    return problems


ASSUMPTION_MARK = re.compile(r"假设|假定|测算|情景|压力|若|如果|倘若")


def _unmarked_assumptions(text: str, material: Material, cites: list[dict],
                          candidates: list[float], facts: list[float]) -> list[str]:
    """A figure only an explicit-assumption calculation supports must be stated as conditional in its sentence."""
    out = []
    for sentence in re.split(r"(?<=[。；;！？!?\n])", text):
        if ASSUMPTION_MARK.search(sentence):
            continue
        for value, decimals in _checked_figures(sentence, material, cites):
            if _supported(value, decimals, candidates) and not _supported(value, decimals, facts):
                out.append(f"数字 {value:g} 只来自显式假设的测算；请在同一句写明假设或测算条件（如“假设……测算为”），"
                           "不要写成已披露事实")
    return out
