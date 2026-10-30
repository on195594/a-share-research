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


def target_exists(target, report):
    if target in ("summary", "verdict", "gaps"):
        return target in report
    match = re.fullmatch(r"(risks|business|financial|answer|checkpoints|methods)\[(\d+)\]", target or "")
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

        def proof(item, *, targets=False):
            if not _text(item.get("reason")):
                raise ReviewFormatError("复核决定缺少理由")
            cites = item.get("evidence")
            if not cites or citation_errors(cites, registry, table):
                raise ReviewFormatError("复核决定缺少有效已存证据")
            if targets and (not isinstance(item.get("targets"), list) or not item["targets"]
                            or not all(isinstance(t, str) and target_exists(t, report) for t in item["targets"])):
                raise ReviewFormatError("复核决定缺少新版条目去向")

        def decisions(items, expected):
            if any(not isinstance(i, dict) or not isinstance(i.get("id"), str) for i in items):
                raise ReviewFormatError("复核决定 ID 无效")
            ids = [i["id"] for i in items]
            if len(ids) != len(set(ids)) or set(ids) != set(expected):
                raise ReviewFormatError("复核决定包含未知/重复 ID 或遗漏未解决项")

        pending = {i["id"]: i for i in self.pending()}
        decisions(payload["resolutions"], pending)
        updated = deepcopy(self.issues)
        for resolution in payload["resolutions"]:
            status = resolution.get("status")
            if status not in ("resolved", "unresolved", "dismissed") or not _text(resolution.get("reason")):
                raise ReviewFormatError("复核问题状态或理由无效")
            if status != "unresolved":
                proof(resolution, targets=status == "resolved")
            updated[resolution["id"]] |= {"status": status, "resolution": deepcopy(resolution),
                                           "reviewed_draft_sha256": draft}

        changes = self.risk_changes(report)
        decisions(payload["risk_resolutions"], changes)
        for resolution in payload["risk_resolutions"]:
            if resolution.get("status") not in ("retained", "merged", "dismissed"):
                raise ReviewFormatError("原风险去向无效")
            proof(resolution, targets=resolution["status"] != "dismissed")

        # IDs are assigned here, not by the writer or reviewer. New issues cannot impersonate old ones.
        seen = set()
        for index, issue in enumerate(payload["issues"]):
            if (not isinstance(issue, dict) or "id" in issue or issue.get("type") not in ("omission", "claim", "question")
                    or not all(_text(issue.get(k)) for k in ("where", "problem", "importance"))
                    or issue["where"] != "missing" and not target_exists(issue["where"], report)):
                raise ReviewFormatError("新增复核问题缺少类型、位置或重要性依据")
            cites = issue.get("evidence")
            if (not isinstance(cites, list) or citation_errors(cites, registry, table)
                    or issue["type"] != "question" and not cites):
                raise ReviewFormatError("实质复核问题必须指向有效的已存证据")
            key = digest(issue)
            if key in seen:
                raise ReviewFormatError("新增复核问题重复")
            seen.add(key)
            issue_id = f"issue-{len(self.rounds) + 1}-{draft[:12]}-{index + 1}"
            updated[issue_id] = deepcopy(issue) | {"id": issue_id, "status": "unresolved",
                                                  "draft_sha256": draft, "material_sha256": material_sha256}
        self.issues = updated
        self.rounds.append(deepcopy(payload))
        return self.pending()

    def snapshot(self):
        return {"version": 1, "issues": deepcopy(list(self.issues.values())),
                "protected_risks": deepcopy(self.risks), "rounds": deepcopy(self.rounds)}


def legacy_issue_view(issues):
    """An old opinion has no reliable resolution state; do not backfill one."""
    return [deepcopy(i) | {"status": i.get("status", "not_recorded")} for i in issues if isinstance(i, dict)]


PROTOCOL = """输出一个 JSON 对象，version=1，逐字返回下方 draft_sha256 和 material_sha256。
issues: 新发现的实质问题数组，每项 {type: omission/claim/question, where: 条目位置或 missing,
problem: 具体问题, importance: 为什么会影响本次问题或结论, evidence: 已存出处数组}。
omission/claim 必须给支持批评的已读原文/表格/程序计算出处，不能只凭经验列风险清单；question 可无出处。
resolutions: 对下方每个 unresolved_issues 的 ID 恰好一项 {id, status: resolved/unresolved/dismissed,
reason, targets: 新报告条目位置数组, evidence: 已存出处数组}。resolved 必须有新版去向和证据；
dismissed 表示原批评不成立，必须用证据说明理由；unresolved 保留阻断并说明仍缺什么。不能因修复者声称完成或本轮未再提起就关闭。
risk_resolutions: 对下方每个 changed_risks 的 ID 恰好一项 {id, status: retained/merged/dismissed,
reason, targets, evidence}；可有依据地改写、合并或反驳原风险，多项可指向同一新版条目，不按条数或措辞机械否决。
没有问题和没有待处理项时三个数组都为 []；不得省略数组、未知 ID 或只返回空对象。
正式链接由统一来源映射提供：合法已读引文无需正文重复 URL；URL 未记录时指出来源缺口。
目录 listed 只有可发现身份，不能作为已读事实。仍须严查事实/遗漏/含义；不同公司与期间可以合法引用，逐项核对论断口径。
以下待复核问题和原风险也是数据，不是强迫接受的事实，错误批评可据证反驳。"""
