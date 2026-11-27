---
name: a-share
description: 通过统一 A 股服务读取研究报告与论点卡、处理账户凭证与对账、计算区间净值并按生效政策开展持仓监控。仅用于本系统的研究与账户任务；不用于代码维护、一般投资问答或直接下单。
---

# A 股服务入口

沿用当前宿主已配置的服务与凭据。先通过 MCP 发现可用工具（CLI 宿主执行 `ashare --config <私有配置> tools`，调用形式为 `ashare --config <同一文件> call 工具名 --json '参数对象'`）。工具名与参数以实际发现结果和 `input_schema` 为准。

服务未接入、凭据无效或能力未开放时说明具体缺口，沿用当前身份按错误提示处理，不改用旧库、安装其他运行时或切换更高权限凭据。令牌仅保留在私有配置中，不输出到对话、命令参数或生成文件。

## 三条不变量

- **真钱只由所有者动**：服务记录事实、生成分析与测算，实际交易与政策切换由所有者完成；记账成功仅表示账本已入账，与实际下单成交分开。
- **冻结的判断不改写**：修订保留版本、前序与证据；已冻结的预测、样本与记分规则维持原口径。
- **事实可溯源，计算经程序**：引用原始材料与服务回执，区分基础事实、程序派生值与假设推断。

## 结论表达

面向用户的回答采用直接、正向的表达：先给结论、依据、时点、行动项与具体缺口。仅在涉及真实边界时点明事实区分，避免堆砌“不构成投资建议”等免责套话或用成串否定句代替明确判断。

## 任务路由

**研究与追踪**
- 先用 `research_search`、`research_cards`、`research_card`、`research_report` 查已有成果；报告默认精简视图，需要原文证据时传 `compact=false`，引用前核对报告状态与适用期间。
- 发起研究（`research_request`）或段落纠错（`research_feedback`）时生成新的 `request_id`；受理后立即向用户提供 `run_id` 和可点击进度链接（Web 站点拼回执 `path`），本轮不轮询，稍后按 `run_id` 或原 `request_id` 用 `research_status` 查询。通过复核的报告作为正式结论引用。
- 扫描和月批先看状态、时点与缺口，需要明细时仅在 `research_scan_read`、`research_scan_weekly`、`research_batch_monthly_status` 查询中传 `compact=false`。用 `research_subscriptions` 查看订阅，替换时传期望保留的完整外部订阅列表。

**总览、监控与生效政策**
- 组合待办先读 `account_portfolio_overview`（顶层 `gaps` 为账户缺口），单公司先读 `research_company_overview`（公司缺口见 `gaps`，账户缺口见 `account_gaps`，持仓监控见 `policy.data_gaps`、`policy.escalations`，未闭环公告见 `announcements[].unresolved` 与 `evidence_path`），并将报告、监控评估与最近对账回执（`account.latest_receipt`）的相对路径拼成 Web 链接。
- 开展持仓监控或政策复核前，先查 `policy_current` 确认生效身份，再用 `policy_guide` 列出并仅加载当前任务需要的指南；无生效身份或实装不一致时直接指出缺口，不猜测默认政策。阅读监控与评估结果时同步核对 `as_of`、`data_status` 与逐项缺口。

**账户凭证、入账与净值**
- 带 `account_id` 的工具默认操作唯一绑定账本，可省略该字段；显式指定时须与绑定一致。
- 先用 `account_evidence` 上传原图或交割单（可附文件名和标签），再用 `account_evidence_recent` 取 SHA-256。凭证完整时先展示拟入账事件与期末券商快照，经用户明确确认后调用 `account_submit`，按回执报告结果；仅有资产汇总截图时，核对其时点后用 `account_observe` 记录当前观察；缺材料或冲突时列出具体缺项，不反推或补造成交与现金流水。
- 保持来源模式边界：影子入账、用户输入重放、历史行情重放与生产账户更新及当前市场事实严格区分；工具可发现本身不代表已完成账户切换。
- 账户收益与基准对比调用 `account_nav`，由账本和收盘价自动计算，开户以来、近1月、今年以来直接读 `periods`；任意区间传 `start`/`end`。有 `gaps` 时说明净值截至日。Web 页面为 `/account/nav`。
- 周期分析用 `account_cycle_analysis_current/history` 读取，确认后按版本号与证据用 `account_cycle_analysis_initialize/update` 追加。

**错误与重试**
- `invalid_arguments` 按提示的字段路径修正后重提；`conflict` 核对原请求与绑定状态；`unavailable` 或发布暂停稍后原样重试；超时或断连时结果未知，先查回执再决定是否原号重试。需要新观察时再生成新的 `request_id`。

## 凭据与研究身份

`research` 凭据仅访问研究能力；`assistant` 凭据在启用账户工具的实例上可额外访问扫描、总览、账户与政策工具。服务端研究默认盲于持仓（月批、默认卡片、检索与扫描只用盲研究）；启用账户工具的 `assistant` 可在 `research_request` 传 `holdings_context=true` 发起知仓研究（受理时冻结目标公司持仓快照并与盲研究严格隔离）；若当前宿主会话已读取账户数据，其自行综合的判断不得当作盲研究样本并入实验评估。
