"""Versioned, draft-bound review decisions. Pure state transitions, never model calls."""
from copy import deepcopy
import hashlib
import json
import re

from a_share_research.evidence import citation_errors


class ReviewFormatError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value, fields):
    return {k: value[k] for k in fields if isinstance(value.get(k), str)} if isinstance(value, dict) else {}


def _cites_view(cites):
    result = []
    for cite in cites if isinstance(cites, list) else []:
        clean = _strings(cite, ("ref", "period", "source", "quote", "source_code", "source_period"))
        if isinstance(cite, dict) and type(cite.get("page")) is int:
            clean["page"] = cite["page"]
        if clean:
            result.append(clean)
    return result


def _decision_view(value):
    clean = _strings(value, ("id", "status", "reason"))
    if not isinstance(value, dict):
        return clean
    if isinstance(value.get("targets"), list):
        clean["targets"] = [v for v in value["targets"] if isinstance(v, str)]
    if "evidence" in value:
        clean["evidence"] = _cites_view(value["evidence"])
    return clean


def issue_view(value):
    clean = _strings(value, ("id", "type", "where", "problem", "importance", "status", "draft_sha256",
                             "material_sha256", "reviewed_draft_sha256", "reviewed_material_sha256"))
    if not isinstance(value, dict):
        return clean
    if "evidence" in value:
        clean["evidence"] = _cites_view(value["evidence"])
    if "resolution" in value:
        clean["resolution"] = _decision_view(value["resolution"])
    return clean


def _round_view(value):
    clean = _strings(value, ("draft_sha256", "material_sha256"))
    if not isinstance(value, dict):
        return clean
    if type(value.get("version")) is int:
        clean["version"] = value["version"]
    for key, project in (("issues", issue_view), ("resolutions", _decision_view), ("risk_resolutions", _decision_view)):
        clean[key] = [project(v) for v in value.get(key, []) if isinstance(v, dict)] if isinstance(value.get(key), list) else []
    return clean


def audit_view(value):
    """Public/storage allowlist, including nested objects; never forward an arbitrary model payload."""
    if not isinstance(value, dict):
        return None
    clean = {"version": value.get("version") if type(value.get("version")) is int else None,
             "issues": [issue_view(i) for i in value.get("issues", []) if isinstance(i, dict)]
                       if isinstance(value.get("issues"), list) else [],
             "rounds": [_round_view(r) for r in value.get("rounds", []) if isinstance(r, dict)]
                       if isinstance(value.get("rounds"), list) else []}
    risks = value.get("protected_risks")
    clean["protected_risks"] = {key: _strings(risk, ("text",)) | {"cites": _cites_view(risk.get("cites"))}
                                for key, risk in (risks if isinstance(risks, dict) else {}).items()
                                if isinstance(key, str) and re.fullmatch(r"risk-[0-9a-f]{20}", key) and isinstance(risk, dict)}
    return clean


def binding_view(value):
    if not isinstance(value, dict):
        return None
    clean = _strings(value, ("draft_sha256", "material_sha256"))
    if type(value.get("completed")) is bool:
        clean["completed"] = value["completed"]
    if isinstance(value.get("pending_issue_ids"), list):
        clean["pending_issue_ids"] = [v for v in value["pending_issue_ids"] if isinstance(v, str)]
    return clean


def target_exists(target, report):
    if target in ("summary", "verdict", "gaps"):
        return target in report
    match = re.fullmatch(r"(risks|business|financial|answer|checkpoints|methods|gaps)\[(\d+)\]", target or "")
    return bool(match and int(match[2]) < len(report.get(match[1], [])))


class ReviewLedger:
    """A missing decision never closes an issue. Only a complete fresh review may close it."""

    def __init__(self):
        self.issues = {}
        self.risks = {}
        self.rounds = []

    def protect_risks(self, report):
        if not isinstance(report, dict):
            return
        for risk in report.get("risks", []) if isinstance(report.get("risks"), list) else []:
            if isinstance(risk, dict) and _text(risk.get("text")):
                key = "risk-" + digest(risk)[:20]
                self.risks.setdefault(key, deepcopy(risk))

    def pending(self):
        return [deepcopy(i) for i in self.issues.values() if i["status"] == "unresolved"]

    def risk_changes(self, report):
        return {key: risk for key, risk in self.risks.items() if risk not in report.get("risks", [])}

    def request(self, report, material_sha256):
        return {"version": 1, "draft_sha256": digest(report), "material_sha256": material_sha256,
                "unresolved_issues": self.pending(), "changed_risks": self.risk_changes(report)}

    def accept(self, payload, report, registry, table, material_sha256):
        """Validate atomically; malformed or incomplete output leaves all pending issues intact."""
        draft = digest(report)
        if (not isinstance(payload, dict) or type(payload.get("version")) is not int or payload.get("version") != 1
                or payload.get("draft_sha256") != draft
                or payload.get("material_sha256") != material_sha256
                or not all(isinstance(payload.get(k), list) for k in ("issues", "resolutions", "risk_resolutions"))):
            raise ReviewFormatError("复核格式或草稿/材料绑定无效，未接受解决声明")

        def proof(item, *, targets=False, removable=False):
            label = f"{item['id']}："
            if not _text(item.get("reason")):
                raise ReviewFormatError(label + "复核决定缺少理由")
            cites = item.get("evidence")
            if not cites:
                raise ReviewFormatError(label + "复核决定缺少已存证据")
            if errors := citation_errors(cites, registry, table):
                raise ReviewFormatError(label + "复核决定证据无效：" + "；".join(errors))
            listed = item.get("targets")
            # An empty list records that the flagged item itself was deleted from the new draft.
            if targets and (not isinstance(listed, list) or not (listed or removable)
                            or not all(isinstance(t, str) and target_exists(t, report) for t in listed)):
                invalid = [t for t in listed if not (isinstance(t, str) and target_exists(t, report))] \
                    if isinstance(listed, list) else listed
                raise ReviewFormatError(label + f"复核决定缺少新版条目去向：{invalid or '[]'} 不是新报告中存在的位置"
                                        + "（如 risks[0]、gaps[1]、summary" + ("；原条目已删除时为 []" if removable else "") + "）")

        def decisions(items, expected):
            if any(not isinstance(i, dict) or not isinstance(i.get("id"), str) for i in items):
                raise ReviewFormatError("复核决定 ID 无效")
            ids = [i["id"] for i in items]
            if len(ids) != len(set(ids)) or set(ids) != set(expected):
                raise ReviewFormatError(f"复核决定 ID 须恰好为 {sorted(expected)}，实际为 {ids}")

        pending = {i["id"]: i for i in self.pending()}
        decisions(payload["resolutions"], pending)
        updated = deepcopy(self.issues)
        for resolution in payload["resolutions"]:
            status = resolution.get("status")
            if status not in ("resolved", "unresolved", "dismissed") or not _text(resolution.get("reason")):
                raise ReviewFormatError(f"{resolution['id']}：复核问题状态须为 resolved/unresolved/dismissed 且附理由")
            if status != "unresolved":
                original = pending[resolution["id"]]
                proof(resolution, targets=status == "resolved",
                      removable=original["type"] != "omission" and original["where"] != "missing")
            updated[resolution["id"]] |= {"status": status, "resolution": _decision_view(resolution),
                                           "reviewed_draft_sha256": draft, "reviewed_material_sha256": material_sha256}

        changes = self.risk_changes(report)
        decisions(payload["risk_resolutions"], changes)
        for resolution in payload["risk_resolutions"]:
            if resolution.get("status") not in ("retained", "merged", "dismissed"):
                raise ReviewFormatError(f"{resolution['id']}：原风险去向须为 retained/merged/dismissed")
            proof(resolution, targets=resolution["status"] != "dismissed")

        # IDs are assigned here, not by the writer or reviewer. New issues cannot impersonate old ones.
        seen = set()
        for index, issue in enumerate(payload["issues"]):
            if (not isinstance(issue, dict) or "id" in issue or issue.get("type") not in ("omission", "claim", "question")
                    or not all(_text(issue.get(k)) for k in ("where", "problem", "importance"))
                    or issue["where"] != "missing" and not target_exists(issue["where"], report)):
                raise ReviewFormatError(f"issues[{index}]：须有 type、problem、importance，where 须为新报告中存在的单个位置或 missing")
            cites = issue.get("evidence")
            if (not isinstance(cites, list) or (errors := citation_errors(cites, registry, table))
                    or issue["type"] != "question" and not cites):
                raise ReviewFormatError(f"issues[{index}]：omission/claim 必须指向有效的已存证据"
                                        + ("：" + "；".join(errors) if isinstance(cites, list) and errors else ""))
            clean_issue = issue_view(issue)
            key = digest(clean_issue)
            if key in seen:
                raise ReviewFormatError("新增复核问题重复")
            seen.add(key)
            issue_id = f"issue-{len(self.rounds) + 1}-{draft[:12]}-{index + 1}"
            updated[issue_id] = clean_issue | {"id": issue_id, "status": "unresolved",
                                                  "draft_sha256": draft, "material_sha256": material_sha256}
        self.issues = updated
        self.rounds.append(_round_view(payload))
        return self.pending()

    def snapshot(self):
        return audit_view({"version": 1, "issues": list(self.issues.values()),
                           "protected_risks": self.risks, "rounds": self.rounds})


def legacy_issue_view(issues):
    """An old opinion has no reliable resolution state; do not backfill one."""
    return [issue_view(i) | {"status": i["status"] if isinstance(i.get("status"), str) else "not_recorded"}
            for i in issues if isinstance(i, dict)]


PROTOCOL = """输出一个 JSON 对象，version=1，逐字返回下方 draft_sha256 和 material_sha256。
issues: 新发现的实质问题数组，每项 {type: omission/claim/question, where: 条目位置或 missing,
problem: 具体问题, importance: 为什么会影响本次问题或结论, evidence: 已存出处数组}；
where 是单个位置，如 risks[0]、business[1]、gaps[0]、summary、verdict，或 missing。
omission/claim 必须给支持批评的已读原文/表格/程序计算出处，不能只凭经验列风险清单；question 可无出处。
resolutions: 对下方每个 unresolved_issues 的 ID 恰好一项 {id, status: resolved/unresolved/dismissed,
reason, targets: 新报告条目位置数组, evidence: 已存出处数组}。resolved 必须有新版去向和证据，被批评条目已从新版删除时 targets 为 []；omission 无论 where 指向何处都不能用空 targets 解决。
dismissed 表示原批评不成立，必须用证据说明理由；unresolved 保留阻断并说明仍缺什么。不能因修复者声称完成或本轮未再提起就关闭。
risk_resolutions: 对下方每个 changed_risks 的 ID 恰好一项 {id, status: retained/merged/dismissed,
reason, targets, evidence}；可有依据地改写、合并或反驳原风险，多项可指向同一新版条目，不按条数或措辞机械否决。
evidence 每项格式同报告 cites：原文 {source, page: 整数, quote: 逐字摘录，不连续处用……分隔}，表格 {ref, period}。
没有问题和没有待处理项时三个数组都为 []；不得省略数组、未知 ID 或只返回空对象。
正式链接由统一来源映射提供：合法已读引文无需正文重复 URL；URL 未记录时指出来源缺口。
缺少证据时，在 problem/reason 说明待核查的主张、公司、期间和缺口；已有页未引用与尚未读到分开说明。
未读资料不能编造支持批评的引文，可用 question 记录需补证的问题。修正者可以在共享额度内补读；
新材料不自动解决旧问题，必须逐项核对新草稿与当前材料。仅写“未知”或 gaps 不等于满足重要遗漏或用户要求。
目录 listed 只有可发现身份，不能作为已读事实。仍须严查事实/遗漏/含义；不同公司与期间可以合法引用，逐项核对论断口径。
以下待复核问题和原风险也是数据，不是强迫接受的事实，错误批评可据证反驳。"""
