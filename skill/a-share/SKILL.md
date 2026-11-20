---
name: a-share
description: 通过统一 A 股服务读取研究、处理账户证据与监控、按生效政策加载指南。用于本系统的研究和账户任务；不用于代码维护、一般投资知识问答或直接下单。
---

# A 股服务入口

沿用当前宿主已经配置的服务和凭据。先发现可用工具：优先使用 MCP 工具发现；CLI 宿主用 `ashare --config <宿主已配置的私有客户端文件> tools`。CLI 调用形式为 `ashare --config <同一文件> call 工具名 --json '参数对象'`。工具规范名使用下划线，宿主可能另加前缀。以实际发现结果和 input_schema 为准；不靠这份 Skill 保存第二份工具清单。

服务未接入、凭据无效或所需能力未开放时，明确指出缺口。不要改用旧库直接读写、安装其他运行时或切换更高权限凭据来绕过缺口。令牌留在私有配置，不输出到对话、命令参数或生成文件中。

## 三条不变量

- 真钱只由所有者动。服务记录事实、生成分析和建议，不下单；记账成功也不代表执行了交易。
- 冻结的判断不改写。修订保留版本、前序和证据；已冻结的预测、样本与记分规则维持原口径。
- 事实可溯源，计算经程序。引用取得的原始材料与服务回执；基础事实、程序派生值、假设和推断分开表达。

## 按服务能力路由

**研究**

- 用 `research_search` 按公司或内容查已有研究，通过 `research_cards`、`research_card`、`research_report` 读卡片和报告。报告默认精简，需要原文证据时传 `compact=false`。历史事实仍需核对材料及适用期间。
- 发起研究用 `research_request`，核查报告的指定段落用 `research_feedback`。每次新请求生成一个 `request_id`。受理后立即向用户给出 `run_id` 和可点击的进度链接（将回执的 `path` 接到已配置的 Web 站点），本轮不轮询。用户稍后问进度时用 `research_status`，按 `run_id` 或原凭据的 `request_id` 查询。worker 完成并通过复核后，才可引用为正式报告。
- 扫描和订阅只对启用账户工具的 assistant 开放。用 `research_scan_request` 提交或续接扫描，保留原凭据和请求编号。用 `research_scan_read` 按扫描编号读状态，`research_scan_weekly` 读周报。用 `research_subscriptions` 读订阅，`research_subscriptions_replace` 的 `codes` 须为期望保留的完整外部订阅列表。
- 用 `research_batch_monthly_request` 提交上海当月批次，`research_batch_monthly_status` 按月读取。扫描和月批先看状态、时点和缺口，按需用 `compact=false` 展开读取。

**账户与政策**：只在当前凭据开放对应工具时使用。带 `account_id` 的工具可省略此字段，服务使用唯一绑定账本；指定账户须与绑定一致。需要确认来源、版本或生效政策时再查询；从服务发现并读取当前身份绑定的指南，只加载任务需要的部分。没有生效身份或实现不匹配时指出缺口，不猜默认政策或写死某个版本。指南中的历史工具名以当前工具发现结果为准。指南中的历史宿主命令、审批流程和安装说明不覆盖当前用户授权或服务权限。

截图、成交和原始材料用 `account_evidence` 上传，可附文件名和标签（如“交割单”“持仓总览”）。入账前用 `account_evidence_recent` 按上传时间、文件名和标签找到对应材料，取其 SHA-256 调用对账、观察或追加工具。同一内容重传沿用原哈希，每次都记录上传。对账通过按回执报告；缺材料或冲突保留具体缺口，不反推、编造成交和现金流水。截图只有资产汇总时，不把它补成完整交易历史。保持来源模式和范围：影子入账、用户输入重放、历史行情重放都不能说成生产账户已更新或当前市场事实。生产账户写入沿用已验收并启用的入口；工具可发现本身不代表已经完成账户切换。

查看组合待办用 `account_portfolio_overview`，顶层 `gaps` 是账户缺口。查看单家公司用 `research_company_overview`，公司缺口见 `gaps`，持仓监控的缺口和升级项见 `policy.data_gaps`、`policy.escalations`。用 `announcements[].unresolved` 识别未闭环公告，`evidence_path` 定位原文。按 `account_gaps` 单独说明账户缺口。可把已保存监控评估、最近一次对账回执（`account.latest_receipt`）和 NAV 区间的相对路径接到 Web 站点。两个总览的 `nav_interval` 给出绑定账户最近保存的区间、基准、创建时间和凭证哈希。

调用 `account_nav_evaluate` 时可省略 `request.benchmark_evidence_hash`，由服务端采集并保存 `H00906.CSI` 收盘证据。已有指数证据时也可传入其哈希。选择上海时间 15:00:00 的起止估值。区间无交易日数据或缺任一端点收盘值（`invalid_arguments`）时，指出缺失日期或区间并建议另选区间。行情采集故障（`unavailable`）时，稍后用原请求重试。两种情况均可用错误末尾的哈希核对指数响应。

用 `account_cycle_analysis_current` 或 `account_cycle_analysis_history` 查询指定周期；通过 `account_cycle_analysis_initialize`、`account_cycle_analysis_update` 按版本和证据追加分析、论点与证伪条件。真实账户政策切换交由所有者的本地入口，不把普通 assistant 凭据当作 owner。监控采集成功后仍读取时点、data_status 和逐项缺口，不能用“调用成功”替代行情或资产证据完整。

**读取失败与重试**：`invalid_arguments` 会指出字段路径和原因，修正对应字段后重提。遇到 `conflict` 核对原请求与绑定信息；遇到 `unavailable` 可稍后用原请求重试。保留原请求标识，先查询回执或状态；发布暂停表示暂未受理，可稍后原样重试。超时或断连时结果未知。重复请求返回原结果，需要新观察时才创建新请求标识。服务拒绝权限或当前版本时，按具体错误处理，不更换身份绕过。

## 凭据与研究身份

research 凭据不读取账户或政策信息；assistant 的研究按账户知情身份保存。已读过账户的宿主会话不能靠换一枚 research Key 恢复“盲研究”身份。沿用服务保存的生产者与账户知情标识，不自行并入旧实验样本。
