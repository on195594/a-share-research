---
name: a-stock-monitor
description: A 股已有持仓的监控、止损、L3、仓位预算与账户对账指南。纯公开首次研究直接走研究工具。
license: Proprietary
---

# A 股持仓监控指南（政策 v0）

## 执行流程

### Level 1：获取快照

- **只查持仓**：直接用 `account_positions` / `account_state` / `account_portfolio_overview`；存在缺口时列出缺项即可。
- **假设卖出测算**：用户明确给出代码、股数与费用时，复用本轮完整监控快照走 `policy_portfolio_scenario`（见 `references/portfolio-risk.md`）；提问含糊时先确认是假设计算还是减仓复核。
- **常规监控**：先调用 `policy_current` 取 `activation.id` 作为 `expected_activation_id`，再调用 `policy_monitor` 采集本轮 `monitor-v1` 快照（`account_portfolio_overview` 仅用于查看已保存的历史评估，不替代本轮快照）。

快照状态、动作、风险、缺口与停止原因均以结构化字段为准：

1. 先核对用户最新持仓/成交与快照是否一致；存在股数、清仓或账户范围差异时，转 `references/data-operations.md` 对账。
2. 缺少 required 数据时执行 fail-closed：不得输出“确定性无动作 / 继续持有 / 仓位不变”，冻结相关交易与风险变更，点名最小补数动作。
3. 快照满足干净快速路径（`stop_reason=clean_fast_gate`）且无账户差异时，直接完成默认交付并结束。
4. 存在 `escalations`、数据缺口或账户差异时，仅针对相关持仓和原因加载 Level 2 参考资料。

### Level 2：按需加载参考资料

动作优先级以 `policy_monitor` 输出和 `references/decision-table.json` 为准，按触发原因加载对应文档：

| 场景 / 升级原因 | 参考资料 |
|---|---|
| `research_thesis_review`、日常异常、公告、活动预警或 L3 | `references/daily-monitoring-and-l3.md` |
| `review_prompts` 研究提醒、估值退出、技术候选或 12 个月复查 | `references/valuation-and-annual-review.md` |
| C/D 框架轻仓试探加仓复核 | `references/step3.5-cd-accumulation.md` |
| A/E/F 框架 Tier 分批止盈与补仓复核 | `references/step4-tier-system.md` |
| 账户差异、已成交补账或结构化状态更新 | `references/data-operations.md` |
| `risk_budget_exceeded`、组合复盘/换仓、加仓定仓或卖出情景 | `references/portfolio-risk.md` |
| Research 报告、论点卡读取与追问纠错 | `references/research-service.md` |

关键升级项语义：
- **`l3_watch`**：已核实的观察状态，冻结该股新增风险，首屏列出并在 `next_review` 到期时复核。
- **`research_thesis_review`**：复核公开证据。仅将干净快速路径的 `no_action` 升级为 `review_candidate`，保留独立止损形成的 `trade_candidate`。止损、预算与 L3 状态沿用快照。
- **`risk_budget_exceeded`**：`code=null` 为组合级复核，基于本轮快照的 `principal`/`unrealized_gain`、`denominator_components` 与 `data_gaps` 只读分析，由所有者决定调仓方案；只读复核的 `requires_user_confirmation=false` 不授予写权限。

## 工具边界

- 本轮持仓报价、风险预算和动作以 `policy_monitor` 快照为准，避免重复查价或手工重算。
- 用户要求消息面时，围绕活动 L3 的核心主题和公司公告检索主来源，每主题取一个权威源，仅在失败或冲突时补充核验。
- 得到可执行动作候选、明确缺口阻断或干净快速路径后即交付，可选背景资料作为按需展开内容。

## 默认交付

结论采用简洁、正向的中文表达，不堆砌免责套话；首屏包含以下要点，细节与推导按需展开：

1. **本次结论**：写明 `action_status`、`data_status`、`as_of`、覆盖率及分母/政策来源；缺关键数据时如实说明受阻项与最小补数动作。
2. **关键变化**：仅与本会话或用户提供的上次结论对比；无可比记录则标明“首次基线”。
3. **研究提醒**：逐条转述 `review_prompts` 要点（仅作提示，不进入 `escalations`、不改变 `action_status`，也不据此推导卖出股数或候选买入量），保留边界提示与 `open_questions`；无提醒写“无”。
4. **首要风险与缺口**：区分持有逻辑、现价新增价值与账户风险贡献；缺口写清缺什么、谁来补、影响哪项判断及下次复核节点。
5. **最小待办与改判条件**：给出明确行动项及下次改变判断的触发条件。
6. **待确认写入**：如需更新 L3、预警或 Tier，将 `account_cycle_analysis_current` 返回的 `data.version` 填入 `update.expected_version`，列出拟提交的 `account_cycle_analysis_update` 载荷（实际 ID、日期、结论/证据、复核日）；无拟议写入写“无”。

## 写入确认

- 建议、授权、申报与成交严格分开；账户流水（`account_submit`）与周期分析（`account_cycle_analysis_update`）仅在用户确认具体变更后提交。
- 存在待确认写入时，整体任务为结构化 `blocked`（保留快照 `action_status`），不因快照 `data_status=complete` 宣告任务完成。
- 用户确认的已成交事实照实入账；即使成交导致预算超限，也正常记录成交并报告最新风险状态。
