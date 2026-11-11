---
name: a-stock-qa
description: "Use for a-stock-research report QA (A股投研QA). Others: SKIP."
license: Proprietary
compatibility: Reads only the supplied immutable text snapshot and this Skill's rubric.
---

# A股研究报告 QA

## 路由与输入

- 当前仅支持 `a-stock-research`；其他类型返回 `process_verdict: SKIP`。
- 只读取完整报告正文的不可变快照和 `references/rubrics/a-stock-research.md`。不运行 runtime CLI、不联网、不读取账户状态、不修改报告或任何持久状态。
- 宿主在 QA 前后核对同一快照的 SHA-256。正文缺失/不可读、只有摘要、内容漂移或工具异常时返回 `process_verdict: INVALID_RUN`；不得计作流程不合规。
- QA 必须由宿主独立调用；报告生成者自行模拟检查不算独立 QA；宿主无独立调用能力时返回 `process_verdict: SKIP` 并说明原因，不得伪称 QA 已完成。

## 执行与停止

1. 加载 research rubric，逐项按 rubric 的适用条件、级别和证据要求输出 `PASS | FAIL | SKIP`，每项附一句依据。
2. 仅按 rubric 级别聚合 `COMPLIANT | PARTIAL | NON_COMPLIANT`；rubric 外建议放 `Advisory`，不得改变 verdict。
3. 分别输出 `Process verdict`、`Data status` 和 `Decision status`；三者不得互相冒充。
4. 输出输入哈希、逐项结果和结论后立即停止。不得补搜事实或替研究报告修正缺口。

## 输出合同

字段统一为 `process_verdict`、`data_status`、`decision_status`、`input_sha256`、`checks` 和 `reason`；正文中的 verdict 指 `process_verdict`，不是另一个字段。
`checks` 每项包含 `name`（检查项）、`result`（PASS / FAIL / SKIP）和 `note`（一句依据）。
- Process verdict：`COMPLIANT | PARTIAL | NON_COMPLIANT | SKIP | INVALID_RUN`；前三级仅用于有效且独立完成的检查，后两级表示未产生合规结论。
- Data status：`COMPLETE | INCOMPLETE`；Decision status：`FORMED | NOT_FORMED`，均仅描述报告文本。
- `SKIP/INVALID_RUN` 时，`data_status` 与 `decision_status` 为 `null`、`checks` 为空，并在 `reason` 说明原因；哈希无法取得时为 `null`，不得伪造。正常完成时 `reason` 可为 `null`。

## 安全边界

- `COMPLIANT` 只表示该文本满足流程 rubric，不证明事实真实、数据完整、外部来源有效、评分已校准或建议可交易。
- 报告修订后旧 verdict 立即失效，必须对新快照重新执行。
- 无法从完整正文确认某项时按 rubric 保守判定；输入本身无效时使用 `INVALID_RUN`，不是 `NON_COMPLIANT`。
