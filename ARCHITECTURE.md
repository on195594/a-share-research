# 系统架构

Web、MCP 与 worker 共用一个 SQLite 账本。产品目标见 [PRODUCT.md](PRODUCT.md)，授权与研究边界见 [AGENTS.md](AGENTS.md)。

## 能力与状态归属

包内依赖方向为 `policy → account → core`，研究模块也从 `core/` 取数和计算。

| 模块 | 职责与状态 |
|---|---|
| `core/`、`market.py` | 数据提供方、来源与时点、行情接入和确定性计算。 |
| 研究模块 | 受理任务、取证、核验、发布报告与论点卡，保存研究材料、复核记录与冻结判断并记分。 |
| `account/` | 保存凭证、账户事件、资产观察与对账回执，从有效事件重建持仓和现金。分析历史按账户和持仓周期追加，涵盖论点版本、L3、预警和 Tier。 |
| `policy/` | 管理版本、激活记录与评估回执。`policy/v0/` 提供 v0 规则与监控指南。政策身份绑定实现代码、运行依赖、参数、规则和指南，每次评估记录所用身份。 |
| `gate.py` | 提供账户、政策与证据记录共用的规范 JSON 编码与哈希。 |
| `tools/`、`skill/a-share/` | 服务端注册表定义工具名、参数与权限。Skill 从当前生效政策读取指南。 |
| `ops/hermes/` | 调度脚本、送达通知并保存去重回执。调度和故障处理见 [Hermes 手册](ops/hermes/README.md)。 |

## 进程与入口

- **web** 提供 HTML 页面、GitHub 所有者会话、OAuth 发现与授权服务，以及授权管理页 `/connections`。账户表单校验会话、同源与 CSRF 后，以专属 `web-owner-proxy` grant 经内网调用 tools 的 `/mcp`，提交凭证、记账或评估 NAV。写入与回执归 tools。
- **tools** 接收 nginx `location = /mcp` 转发的 Streamable HTTP MCP 请求。服务按公网地址校验 Host 和 Origin，接受有效的非 owner PAT 或绑定本资源的 OAuth access token。五个本机宿主使用原生 HTTP MCP，Hermes 业务脚本经 `ashare` 调用。政策激活仅走本机 owner 入口。接入方法见 [README](README.md#web-与-mcp-接入)。
- **worker** 执行研究、核验、扫描与记分，经 pi 调用模型。模型凭据只注入 worker，tools 从独立环境文件读取行情凭据。研究模型只使用受控研究工具。
- MCP 与 tools 业务 REST 热备共用注册表与 Gate。写入与采集共享两个并发名额。工具发现与读取只登记在途。发布暂停时拒收新请求，等待在途请求结束，暂停标记跨重启保留。旧入口的退役条件见[实施计划](docs/implementation-plan.md#web-与-mcp-双入口实施)。

## 数据与兼容

- **研究证据**保留原文、出处、时间、单位与期间。派生数字保存计算输入与算式。`exact_values` 保存未舍入值，旧报告缺此字段时按原精度读取。
- **账户**以绑定账本为唯一写入来源。成交批次核对凭证、账本版本与期末快照后入账，更正时作废原事件并追加正确记录。缺少完整成交材料时可单独记录资产观察。成交明确采用的报告进入交易记分，成交时可查的报告仅用于覆盖率诊断。
- **扫描**由服务端合并已核实持仓、显式关注、最近冻结及仍在跟踪批次的 L5 候选和有正式论点卡的公司。同一代码只扫描一次，保留全部来源。扫描 worker 向 `disclosure_checks` 追加披露判定。政策监控在进程内按持仓读取研究卡。
- **身份与凭证**由 `api_keys` 保存稳定 grant 编号、角色与撤销标记。Web 管理 OAuth 客户端、一次性授权码和令牌换发，tools 验证 access token。授权码和令牌只存摘要。`account_evidence_uploads` 保存凭证上传记录，`account_nav_refs` 保存净值区间凭证引用。

查询报告或账户总览不会自动提交研究、修改账户或切换政策。软件、接口、判断契约与投资政策分别版本化。应用回退保留账本与后续写入，当前兼容基线见[实施计划](docs/implementation-plan.md#当前部署)。
