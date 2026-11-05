"""A continuation's verdict on each risk and checkpoint of the report it follows: rules and V5 grouping."""
import re

RISK_STATUSES = ("已发生", "仍存在", "已缓解", "已证伪", "无法判断")
CHECKPOINT_STATUSES = ("已触发", "未触发", "未到期", "无法判断")
STATUSES = {"risks": RISK_STATUSES, "checkpoints": CHECKPOINT_STATUSES}
# Only these state an absence of evidence, so only these may stand without a source.
NO_EVIDENCE = frozenset({"无法判断", "未到期"})
UNDECIDED = NO_EVIDENCE
ADVERSE = frozenset({"已发生", "已触发"})
GROUPS = ("存在已发生或已触发", "全部判定且无此类事项", "未判明")

_REF = re.compile(r"(risks|checkpoints)\[(0|[1-9]\d*)\]")


def expected_refs(prior: dict) -> list[str]:
    """Every position of the frozen agenda; a section an older report lacked counts as zero items."""
    return [f"{section}[{i}]" for section in STATUSES for i in range(len(prior.get(section) or []))]


def problems(value, prior: dict) -> list[dict]:
    """Structural checks: one entry per old item, a status from that item's section, a stated reason."""
    wanted = expected_refs(prior)
    if not isinstance(value, list):
        return [{"where": "prior_status", "problem": "接续研究必须提供 prior_status 数组，逐项覆盖："
                 + ("、".join(wanted) or "旧议程为空，请给 []")}]
    found, seen = [], set()
    for i, item in enumerate(value):
        where = f"prior_status[{i}]"
        if not isinstance(item, dict):
            found.append({"where": where, "problem": "条目必须包含 ref、status、text"})
            continue
        ref, status = item.get("ref"), item.get("status")
        match = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if ref not in wanted:
            found.append({"where": where, "problem": f"ref {ref!r} 不在旧议程中；可用："
                          + ("、".join(wanted) or "旧议程为空，prior_status 应为 []")})
        elif ref in seen:
            found.append({"where": where, "problem": f"ref {ref} 重复；每个旧条目只判定一次"})
        else:
            seen.add(ref)
        allowed = STATUSES.get(match[1]) if match else None
        if allowed and status not in allowed:
            found.append({"where": where, "problem": f"{match[1]} 的状态不能是 {status!r}；可用：{'、'.join(allowed)}"})
        if isinstance(item.get("text"), str) and not item["text"].strip():
            found.append({"where": where, "problem": "text 须说明本期状态；无法判断时写明缺什么"})
    if missing := [ref for ref in wanted if ref not in seen]:
        found.append({"where": "prior_status", "problem": "缺少这些旧条目的状态：" + "、".join(missing)})
    return found


def group(items: list[dict]) -> str:
    """Report-level bucket for V5; an unknown is never read as nothing having happened."""
    statuses = {item["status"] for item in items}
    if statuses & ADVERSE:
        return GROUPS[0]
    return GROUPS[1] if items and not statuses & UNDECIDED else GROUPS[2]


def sample(reports: list[dict]) -> dict:
    """Filter before deduplicating, so an unrecorded or same-period rerun never takes a scored slot."""
    picked, unrecorded, reruns, seen = [], [], [], set()
    for report in sorted(reports, key=lambda r: (r["completed"], r["id"])):
        if report["prior_status"] is None:
            unrecorded.append(report)
        elif report["period"] <= report["prior_period"]:
            reruns.append(report)
        elif (key := (report["code"], report["period"])) not in seen:
            seen.add(key)
            picked.append(report | {"group": group(report["prior_status"])})
    return {"picked": picked, "unrecorded": unrecorded, "reruns": reruns}


def _share(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def tally(reports: list[dict]) -> dict:
    """Item counts by section and status, plus each report's share it could not judge."""
    counts = {section: dict.fromkeys(statuses, 0) for section, statuses in STATUSES.items()}
    per_report = []
    for report in reports:
        items = report["prior_status"]
        for item in items:
            counts[_REF.fullmatch(item["ref"])[1]][item["status"]] += 1
        per_report.append({"id": report["id"], "code": report["code"], "items": len(items),
                           "unknown": _share(sum(i["status"] == "无法判断" for i in items), len(items))})
    risks, checkpoints = counts["risks"], counts["checkpoints"]
    return {"counts": counts, "per_report": per_report,
            "occurred": _share(risks["已发生"], sum(n for s, n in risks.items() if s not in UNDECIDED)),
            "triggered": _share(checkpoints["已触发"], checkpoints["已触发"] + checkpoints["未触发"])}
