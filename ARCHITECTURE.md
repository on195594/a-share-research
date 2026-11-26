# 系统架构

Web、tools 和 worker 共用一个 SQLite 账本。worker 负责材料与发布，按任务启动 `agent-runner` 执行模型调用；Web 账户表单写入与监控评估经 `tools` 落账，研究申请、反馈和价格任务可由 Web 或 `tools` 经共享 `service` 受理。产品目标见 [PRODUCT.md](PRODUCT.md)，研究规范见[研究与主动跟踪规范](docs/research.md)，开发授权见 [AGENTS.md](AGENTS.md#授权)。

## 能力与状态归属

包内依赖方向为 `policy → account → core`，研究模块也从 `core/` 取数和计算。

| 模块 | 职责与状态 |
|---|---|
| `core/`、`market.py`、`sources.py` | 数据提供方、公告与卖方覆盖采集、来源与时点、行情接入和确定性计算。 |
| `research.py`、`research_mcp.py`、`research_tools.py`、`runner_process.py`、`prompts.py` | 冻结研究身份与材料，执行取证和计算，启动 runner 子进程，用 `prompts/` 模板渲染全部提示词并裁决复核、修补或结束，持久化工具轨迹与模型调用，判定终稿能否发布。 |
| `drafts.py`、`checking.py`、`reviewing.py` | 按原稿索引原子合并补丁，核对事实与计算，绑定材料、稿件、历次问题及决议。 |
| `agent-runner/` | 按角色和阶段调度模型、切换工具、判定停止和执行调用上限；不持有阶段提示词模板（仅在格式或补丁无效、未提交工具时发送单句工具纠正提示），对话只在子进程内存中保存。 |
| `account/` | 保存凭证、账户事件、资产观察与对账回执，从有效事件重建持仓和现金，构造每日资产和资金流并调用 `core/account_nav.py` 计算单位净值；按账户与持仓周期追加论点版本、L3、预警和 Tier。 |
| `overview.py`、`reading.py` | 投影公司总览与组合总览（按绑定账本与当前持仓周期读取股数、成本及在仓论点，隔离已清仓周期）及报告检索视图。 |
| `policy/` | 管理版本、激活记录与评估回执。`policy/v0/` 提供 v0 规则与监控指南；政策身份（schema 3）只绑定 `RULES.md`、`guide/` 与参数，与业务代码和 `uv.lock` 解耦。 |
| `gate.py` | 提供账户、政策与证据记录共用的规范 JSON 编码与哈希；严格解析拒绝重复键和非有限数字。 |
| `tools/`、`skill/a-share/` | 服务端注册表定义工具名、参数、读写属性与权限；调度服务受理定时运行并按账本确认记录继承状态；统一分发外部 Agent Skill。 |
| `ops/hermes/` | 按时触发脚本并投递输出；业务脚本通过 `ashare` 调 `schedule_run` / `schedule_ack`；记分脚本仍在宿主保存通知键。见 [Hermes 手册](ops/hermes/README.md)。 |

## 进程、双入口与鉴权

- **web**：提供 HTML 页面、GitHub 所有者会话（8 小时到期；可在 `/connections` 启用“记住此设备 30 天”恢复登录，设备截止日固定不顺延，过期会话上的表单写请求须先刷新重提，退出撤销当前设备及派生会话）、OAuth 发现与 PKCE S256 授权管理。账户表单校验会话、同源与 CSRF 后，以专属 `web-owner-proxy` grant 经内网调用 tools 的 `/mcp`；页面读取共用只读视图。
- **tools**：接收 nginx `location = /mcp` 转发的 Streamable HTTP MCP 请求，按公网地址校验 Host、Origin（若提供）与非 owner PAT 或绑定本资源的 OAuth access token（`owner` 凭据仅限本机策略工具）。成功回执同时返回文本和结构化数据，参数错误给出字段路径。写入与采集共享 2 个并发名额；读取只登记在途。发布暂停标记（`DATA_DIR/.tools-paused`）跨重启保留，暂停时拒收新请求并等待在途结束。旧 `/tools` REST、`/api` Agent JSON 与 `agent:` 会话热备到段 4a 退役为止，见[实施计划](docs/implementation-plan.md#web-与-mcp-双入口实施)。
- **worker**：领取任务前调用 runner `--check` 离线解析作者与复核员模型和凭据。一次执行一个单股任务，单股和扫描队列空闲时推进批次及 `report_rc` 补取。worker 持有研究行情凭据与宿主 `~/.pi`；tools 行情凭据来自独立 `.env.tools`；web 不持模型与行情凭据。

## 研究与模型调用

1. **采集与子进程边界**：`research.execute` 在采集前冻结判断契约、模型配置档哈希和生效上限，采集完成后保存材料快照并启动独立 `node agent-runner/main.ts run` 子进程（Node 24 直接运行 TypeScript，加载宿主 pi 的 `ModelRuntime`）。总时限从采集前起算。runner 通过 stdio MCP 读取 `research://run/context` 与当前角色可用工具，不开端口也不发单次令牌。运行时随宿主 pi 升级，升级后首跑核对 `calls.identity` 中的 `$.pi_version`（报告投影为 `model_calls[].pi_version`）。
2. **提示词缓存与预算**：六个 `*_system.j2` 保持零动态变量，静态指令与工具声明构成 `SystemMessage` 前缀；`output_schema`、`cards`、`acquire` 与名单规则进用户消息。`sessionId` 仅按 `{ system, tools }` 生成缓存键，使跨公司同阶段共享服务端提示缓存而不共享对话上下文。
3. **取证、复核与修补**：worker 在内存中累积工具证据与轨迹，在提交稿件/补丁/决议及阶段切换或退出前批量写入 `runs.material`。来源身份或已读页冲突时由 worker 结束运行。机械检查通过后由独立复核员提交整轮决议；需要修补时 runner 另开新对话，只传索引化原稿、刷新材料与问题。每次模型尝试都经 `record_call` 记账；取证瞬时错误重试在扣除 `draft_call_reserve` 后的预算内执行，退避期间超时经 `research/model_error` 上报并记为 `timeout`。
4. **交付、重放与批次比较**：终稿在同一事务内保存报告、估值、冻结立场、事件证据与任务终态。`--replay` 按来源任务哈希复用冻结材料并仅注册试稿/复核/修补/审计工具。普通带请求编号任务中断最多重跑一次，自动月批研究中断记为失败。批次比较统一经 `batch` stdio MCP 与 `json_schema` 约束的 `submit_selection`、`submit_ranking` 结构化提交，worker 即时记账并校验草案，根据通过结果冻结名单。角色、模型与上限见[模型配置档](a_share_research/model_profile.json)。

## 状态与定时运行边界

- **证据与任务幂等**：研究证据保留原文、出处、时间、单位、期间及算式；`exact_values` 保存未舍入值（旧报告缺此字段按原精度读取）。研究与价格任务状态均为 `queued`、`running`、`done`、`failed`：研究请求先按请求者与 `request_id` 查原任务再查公司目录，同号异内容报冲突；价格任务先校验代码与目标报告，再按 `request_id` 复用已有任务；CLI 自管任务由原进程负责。PDF 按内容哈希保存，页文本缓存绑定 PDF、提取器版本、模式和页文摘要；模型调用记录中的共享资料块与逐轮增量须能还原完整提示词并匹配调用时记录的 `prompt_sha256`；报告正文哈希发布时生成（旧记录缺哈希不补写），只读完整性巡检可在线核查库、快照、调用提示还原、引用与原件。
- **账户与净值**：账本为唯一写入来源，成交核对凭证、账本版本与期末快照后入账（更正采用作废原事件加替代流水），缺完整材料时仅记资产观察。入账只校验经济账本（股数、现金、成本与周期），不依赖 v0 政策投影：新开仓缺 v0 政策身份或框架时照常建立成本周期，持仓与总览读取继续可用并列出 `policy_gaps`，完整性巡检不计为损坏；v0 监控及其派生评估以 409 指明待补的公司与周期，缺 v0 框架须作废原买入并重提完整流水（同成交时刻有后续加仓须按原顺序一并作废重提），仅有未初始化周期分析时提示调用 `account_cycle_analysis_initialize`，已入账的买入不能经重复提交补填开仓元数据。成交明确采用的报告（`stated_report_id`）才进入交易归因记分，成交当时可查的报告（`available_report_id`，见 `account_research_coverage`）仅用于覆盖率诊断（见[工具手册](a_share_research/tools/README.md#账户凭证与对账)）。`account_nav` 由 tools 按账本持仓、现金与收盘价及交易时段修正 Dietz 即时计算，不落库；`account_nav_refs` 为兼容旧库与回退镜像保留的退役空表。
- **身份与凭据**：`api_keys` 保存稳定 grant 编号、角色与撤销标记；OAuth 授权码与令牌只存摘要，撤销即刻使续签、调用和写事务内授权失效。
- **定时运行服务端状态（`scheduled_runs`）**：按 `(principal_key_id, job, slot)` 唯一记录政策激活、评估或扫描编号、最终正文、脱敏失败原因及 `acked` 状态，宿主业务脚本只保留任务锁不做本地待查对账。止损档位、机会与月批事件严格按已确认记录去重（未确认可重推；`acked` 表示脚本已处理回执，最终送达查 Hermes）。定时评估使用当时生效政策，采集中切换且确定旧评估未写入时服务端换号重试一次。跨期不补造过期日报或周报，跨日扫描先补已提交披露再评当日机会。

## 数据层与迁移治理

- **公司标识与查询**：`company_key` 是六位 ASCII 数字生成列，从 `code` 生成（接受六位或 `.SH`/`.SZ`/`.BJ` 九位代码，非空约束拒绝全角与无效后缀；原始 `code` 留作存证）。SQL 查询前统一调用 `stock_codes.normalize_key` 归一化入参，数据源层自行处理如 `T00018.SH` 等特殊标识。
- **表布局与关联**：大字段置表尾（`runs` 的 `request`/`identity`/`material`、`reports.body`、`calls` 的 `response`/`prompt`）避免小列读取遍历溢出页。`runs.parent_report_id`、`runs.is_feedback`、`runs.batch_id` 为生成列，供关联父报告、纠错和月批（断链或孤立任务由完整性巡检列入 `dangling_parent_reports` / `orphan_batch_runs`）。`runs.id`、`reports.id` 使用 `AUTOINCREMENT`。
- **记分样本常量**：`db.RESEARCH_ORIGINAL` 选 `kind='research'`、`replay_of IS NULL`、非纠错任务；`db.FORMAL_REPORT` 另要求 `passed=1` 与任务状态 `done`（批次复用正式报告另按时效过滤并排除围绕原报告作答的接续报告）。冻结记分按各自定义选样：`validation.source()` 按请求中 `feedback` 真值排除纠错、按问题是否等于批次问题区分批次与主动研究，且按通过报告取样的冻结查询不额外要求任务状态为 `done`；统一这些条件须另开记分版本（见[判断验证](docs/judgment-validation.md)）。
- **验收与治理规则**：
  1. 带公司代码的新表须有 `company_key` 且跨表只用其比较；表间关系用列表示；大字段置表尾，其后追加高频读取列须配索引或重建表。
  2. 涉及选样的变更须在同一生产数据副本比对新旧样本编号，并用通过报告的失败、运行中和纠错变体验证冻结语义。
  3. 迁移在事务内执行并支持重入：用 `table_xinfo` 查生成列，重建后核对索引、触发器、外键、序列、`integrity_check`，并对按设计原样保留的非生成列按列名逐行比对（含冻结 JSON 与哈希）；验证重建中断回滚、空库首次并发切 WAL 启动可用，以及存量库双连接并发升级时同一迁移只执行一次。
  4. 在生产副本上用[实施计划回退基线](docs/implementation-plan.md#当前部署)中的实际回退镜像验证读写、完整性与业务兼容；不兼容迁移须单独设计回退。
  5. 从旧备份恢复数据库会丢失备份后的写入，须由所有者决定并确认受影响写入：先排空并停服停止全部写入，再读取现库 `sqlite_sequence` 的 `runs`、`reports` 高水位及账户事件引用的最大报告编号；恢复备份并完成所需迁移后，开放写入前将两个序列分别抬至相应高水位，核对账本引用的报告均存在且对应正确；高水位无法可信重建时，须先完成归因核对。
