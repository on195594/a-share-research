# 系统架构

一个仓库、一套受控工具、一个 SQLite 账本。产品目标见 [PRODUCT](PRODUCT.md)，授权与硬边界见 [AGENTS](AGENTS.md)，生产版本、开关和剩余观察见[实施计划](docs/implementation-plan.md)。

## 能力与状态归属

| 层 | 职责 | 边界 |
|---|---|---|
| `core/`、`market.py`、`gate.py` | 数据接入、来源与时点、确定性计算；`gate.py` 是账户、政策与证据记录共用的规范 JSON 与哈希 | 不拥有账户动作或模型编排 |
| 现有研究模块 | 取证、计算核验、报告与论点卡、判断入账和记分 | 研究运行时看不到账户；冻结判断不改写 |
| `account/` | 账户证据、事件、对账、持仓与现金投影、交易依据 | 对账通过才入账，冲突显式处理；不推造历史现金流水 |
| `policy/`、`policy/v0/` | 版本化投资政策、监控及账户决策状态 | v0 保留原规则；新政策由所有者激活，不由研究 AI 修改 |
| `tools/`、`skill/a-share/` | 统一注册表、下划线工具名、身份权限与 Skill | 能力开放以服务端角色和开关为准；正文不是授权 |
| `ops/hermes/` | 定时调用、通知与去重回执 | Hermes 负责调度送达，不拥有业务规则或账户事实 |

依赖方向为 `policy → account → core`；研究模块使用 `core`，不为目录整齐迁移。机器合同随代码维护，工具接口见 [tools/README](a_share_research/tools/README.md)，Web 与 MCP 接入见 [README](README.md#web-与-mcp-接入)。

## 进程与入口

- `web` 提供网页、GitHub 所有者会话、OAuth 授权与换发令牌、两份 OAuth 发现文档，以及 `/connections` 授权管理页。Web 按站点公开地址签发绑定到 `/mcp` 资源的令牌。账户表单校验所有者会话、同源和 CSRF 后，以专属 `web-owner-proxy` grant 经内网向 tools 的 `/mcp` 提交凭证、记账和 NAV 采集，使用公网 Host，写入与回执由 tools 负责。
- nginx 用 `location = /mcp` 将 MCP 流量转给 `tools`。`tools` 提供无会话的 Streamable HTTP MCP，使用 JSON 响应，按公开地址校验 Host 和请求携带的 Origin。Bearer 鉴权接受有效 PAT 或绑定该资源的 OAuth access token。本机 owner 凭据用于政策管理。
- MCP 工具发现与调用和热备 REST 共用 Gate。写入与采集共占两个名额，读取与发现只登记在途。发布暂停拒绝新发现与调用并排空在途请求，暂停标记跨重启保留。调用遇到暂停或满额时返回带错误码的 `isError` 回执，发现可能收到协议错误或 HTTP 503。
- 五个本机宿主使用原生 HTTP MCP。Hermes 的业务脚本通过远程 MCP 客户端 `ashare` 调用服务。旧 stdio 桥已移除。`/tools` 业务 REST 与 Web `/api` 中面向 Agent 的 JSON 暂作热备，退役条件见[双入口方案段 4a](docs/web-mcp-platform.md#5-实施分段)。
- `worker` 执行已受理的研究、核验、扫描和记分，经 pi 调用模型。模型凭据只注入 worker，tools 使用独立行情凭据。研究运行时与账户隔离。
- 人与外部 Agent 使用同一研究服务和证据语义。外部客户端各持独立可撤销的授权，服务按角色开放工具。账户建议结合账本事实和生效政策，真钱只由所有者操作。

## 数据与兼容

来源保留原文、出处、时间、单位与期间；派生数字保留程序核验的输入和算式。缺失、过期、失败及不支持的版本显式返回。`exact_values` 保留未舍入值，旧报告没有该字段时不补造精度；计算标签不代表指标定义已核实。

研究任务、报告、证据与公开复核归研究层；`disclosure_checks` 披露判定由扫描 worker 拥有，只追加；账户证据、事件和分析历史归账户层，政策评估与生效指针归政策层。交易的“显式依据”用于记分，“成交时可用研究”只用于覆盖率诊断。读取不自动触发新研究、账户修改或政策切换。

扫描范围由服务端合并已核实持仓、显式关注、最近冻结及仍在跟踪批次的 L5 候选，以及已有正式论点卡的公司，去重时保留全部来源。政策监控在进程内按已核实持仓读取对应研究卡。Hermes 经 MCP 发起扫描、读取结果并投递通知，无需将持仓同步到 `external` 订阅。

v25 的身份状态由 `api_keys` 保存稳定的 grant 编号、角色和软撤销标记。Web 管理 `oauth_clients` 客户端登记和 `oauth_codes` 一次性授权码，Web 签发与刷新 `oauth_tokens` 中的令牌，tools 验证 access token。授权码和令牌只保存摘要。账户层的 `account_nav_refs` 保存净值区间凭证引用，`account_evidence_uploads` 追加上传记录，供 `account_evidence_recent` 查找最近保存的凭证。

接口变更按 [AGENTS 验证要求](AGENTS.md#验证与交付)检查实际消费者、旧产物及失败语义；报告契约另服从[判断契约规则](docs/judgment-validation.md#判断契约与修改规则)。软件、接口、判断契约与投资政策分别版本化。

## 历史与回退

Lib 已并入 `core/`，Skills 运行时保留于 `policy/v0/`；旧账户库只读封锁，新账本是唯一账户写入来源。应用回退也必须保留新账本，不恢复旧写入、不用切换前数据库覆盖新记录。操作见 [README](README.md#部署)，当前回退版本见[实施计划](docs/implementation-plan.md#当前部署)。

合一设计、所有者决定与验收基准保留在[合一方案](docs/consolidation.md)；旧 `a-stock-lib`、`a-stock-agent-skills` 仓库本机副本已归档至 `/home/lin/archives/` 且 GitHub 远程仓库已归档只读（`a-stock-tracker` 亦已归档，不作为依赖或回退来源）。当前观察与未完成项只在[实施计划](docs/implementation-plan.md)维护；切换前项目契约与第 2 段流水历史分别见 `git show eeec702:docs/project-contract.md`、`git show eeec702:docs/consolidation-stage2-history.md`。
