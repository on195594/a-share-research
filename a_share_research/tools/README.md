# 工具入口

`/mcp` 按调用授权开放工具。用 `ashare tools` 或 MCP 的 `tools/list` 获取实际可见的名称、输入输出 schema、读写属性与错误分类。[接入与授权](../../README.md#web-与-mcp-接入) · [CLI 配置与退出码](../../ops/clients/README.md#cli-与本机-cron)

```sh
ashare --config /absolute/private/client.json tools
ashare --config /absolute/private/client.json call research_card --json '{"code":"600519"}'
```

`research` 和 `assistant` 均可读取、提交研究与操作月批。启用账户工具的实例另向 `assistant` 开放扫描、订阅、公司与组合总览、账户和政策工具。远程 MCP 拒绝 `owner` 凭据。调用的业务权限、参数与结果类型由[注册表](registry.py)定义，进程内可用 `registry.invoke(context, name, arguments)` 走同一套校验。

## 服务与控制

开发服务使用已迁移至当前 schema 的数据库副本。默认监听 `127.0.0.1:8621`。指定 `ASHARE_PUBLIC_MCP_URL` 启用 MCP，指定 `ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED=1` 启用账户工具（默认 0）。Web 请求的 Host 取自 `PUBLIC_BASE_URL`，与 tools 地址不匹配时返回 421。

```sh
ASHARE_PUBLIC_MCP_URL=http://127.0.0.1:8610/mcp DATA_DIR=/absolute/private/copy \
  uv run python -m a_share_research.tools.server --control-socket /absolute/private/control/control.sock
```

控制目录须由当前用户持有，权限为 0700，socket 为 0600。进程退出且 socket 文件残留时，清理该文件后再启动。`/health` 查询进程存活状态；`/ready` 核对数据库版本、所需表及暂停状态，返回 `database_ready`、`schema_version`、`paused`、`in_flight`。暂停或数据库未就绪时返回 503；启动与检查均不创建或迁移数据库。

读操作用只读连接。写入和采集共用两个并发名额；名额用尽返回 `busy`。运维经本地 socket 执行 `python -m a_share_research.tools.control status|pause|resume --socket 路径 --timeout 30`。`pause` 停止受理并等待在途操作结束，超时后仍保持暂停。`DATA_DIR/.tools-paused` 使暂停状态跨重启生效。生产发布由[发布工具](../../README.md#发布与政策激活)在政策门禁通过后恢复受理。

## 研究、订阅与扫描

| 工具 | 用法 |
|---|---|
| `research_search`、`research_methods` | 前者用 `q` 搜公司或报告，可选 `code`、`section`、`history`、`page`。后者用 `query` 搜已审核方法卡，返回适用条件、来源和目录哈希。 |
| `research_cards`、`research_card`、`research_report` | 读取卡片列表、单公司卡片或按 `report_id` 取报告。报告默认为精简视图，`compact=false` 展开证据。 |
| `research_request`、`research_feedback` | 研究提交六位 `code`、`request_id`，可附 `question`、`parent_report_id`。反馈提交 `report_id`、`location`、`text` 和 `request_id`。 |
| `research_status` | 按 `run_id` 或原凭据的 `request_id` 二选一找回任务；`compact=false` 展开诊断与预算。 |
| `research_batch_monthly_request`、`research_batch_monthly_status` | 以 `month`（YYYY-MM）提交上海当月批次或查询指定月状态；查询可用 `compact=false` 展开名单与缺口。 |
| `research_subscriptions`、`research_subscriptions_replace` | 读取订阅及来源；传 `codes` 完整替换外部订阅，空列表清空外部订阅。 |
| `research_scan_request`、`research_scan_read`、`research_scan_weekly` | 按原凭据与 `request_id` 提交或找回扫描；按 `scan_id` 读结果或取近七日汇总，可用 `compact=false` 展开。 |

提交研究或反馈后，立即给用户任务编号与进度页（Web 站点加回执的 `path`）。任务由 worker 执行，用户再次询问时查询进度。响应丢失先以原身份和原编号找回；同号改内容会冲突。

## 账户凭证与对账

带 `account_id` 的工具默认使用 `account_source` 唯一绑定的账本；显式传入须与绑定一致。`account_current` 读取已核对的在仓代码及来源，`account_positions` 读取成本和持仓周期，现金流只从期初累计。

1. 上传原件。向 `account_evidence` 提交 `content_base64`（解码后最多 20 MiB），可带 `filename` 和 `label`。服务按 SHA-256 保存内容，每次上传都记录来源。`account_evidence_recent` 倒序返回近期凭证，每个哈希取最近一次上传记录，`limit` 为 1–100，默认 10。
2. 用 `account_state` 读取版本、现金、股数和容差。有完整原件时用 `account_submit` 提交预期版本、事件、证据哈希和期末券商快照。更正时用 `account_events` 查原事件编号，提交作废与替代事件。仅有不完整截图时用 `account_observe` 保存资产观察。
3. 读取 `data.status`。只有 `posted` 表示成功入账；`missing_material`、`conflict` 或 `service_failure` 保留原交易投影。通过 `account_receipt` 按回执编号核查。成功批次的相同输入返回原回执。Web 表单写入也记录实际授权的 `key_id`。

观察和快照只有在原件给出估值时点时才填 `portfolio_value_as_of`（完整、带时区的时间）。观察须有总资产，`observed_at` 不早于估值时点。快照的估值时点不得早于期初或本次有效事件，不得在未来。

`account_nav` 按绑定账本逐日计算单位净值，无需选择估值或声明资金流水。每个交易日收盘的总资产为账本持仓乘以 TuShare 不复权收盘价再加账本现金；分红与送转记在交易日当天的，即使晚于 15:00 也计入当日收盘。单日收益按修正 Dietz：转入转出计入上一收盘之后、本收盘及之前（15:00:00 及之前算当日，之后算下一交易日），并按发生时刻之后剩余的交易时段占比（09:30–11:30、13:00–15:00，开盘前为 1、收盘为 0）计入收益分母，同一时刻的流水按入账顺序重放。账户清零期间净值保持不变。基准为服务端采集的 H00906.CSI 全收益。返回逐日 `days`，以及开户以来、近1月、今年以来的 `periods`；传 `start`/`end`（`YYYY-MM-DD`）另加“指定区间”，取各自当日或之前最近的收盘，起点早于开户时截到开户日（结果的 `start` 为实际起点，起点相同的标准窗口只列一次），终点早于开户或起点晚于终点时报错。持仓某日没有收盘价时，按 TuShare `suspend_d` 区分：全天停牌沿用前收盘，否则视为未发布，序列截至此前一个交易日并在 `gaps` 说明。收益率为小数，超额为百分点。

`research_company_overview` 与 `account_portfolio_overview` 对应 Web 的公司与组合总览，返回已有评估、当前持仓周期、研究与公告复核和相应的缺口。通过 `account_research_coverage` 可区分成交明确采用的报告（`stated_report_id`）与成交时可查的最近正式报告（`available_report_id`）。

## 成本、周期与分析

已配置成本期初的账本在入账事务中同时重建成本及周期。新开仓在 `batch.position_entries` 中按本批事件下标（字符串键）提供 `policy_identity`；已绑定 v0 政策期初时还需对应参数版本的 `framework`。分红可通过 `batch.position_dividends` 指定 `cycle_id`，有多个周期或没有活动周期时须明确绑定。每股现金除权数用 `batch.position_distributions` 指定，与实际分红到账金额分别提供。

- `account_cycle_analysis_initialize` 接收 `cycle_id` 及 `initialize` 对象。对象内提供 `expected_account_version` 与 `evidence_hashes`，期初导入的旧周期还须提供当前的 `legacy_revision_id`。新周期从空论点、待查状态开始。
- `account_cycle_analysis_current`、`account_cycle_analysis_history` 按账户和周期读取最新分析及分页历史。`account_cycle_analysis_update` 以分析版本 `expected_version`、证据、原因和 `changes` 追加论点、L3、预警或 Tier 变更。论点重写须覆盖原活动条件，并与条件状态更新分批提交。已关闭或失效的周期保留历史。

开发副本中，`account.position_store.initialize` 与 `policy.v0.account_store.initialize` 分别在首笔金融事件前绑定成本期初与 v0 政策期初。用 `POLICY_V0_ACCOUNT_SHADOW=账户编号` 在只读连接内读取 v0 形状的持仓、事件投影，可联用 `POLICY_V0_ANALYSIS_SHADOW=1` 读取周期分析。`a_share_research.tools.lifecycle.read(conn, principal, holding_id)` 是本机历史诊断函数，按原始日期和事件编号重放，返回成本、盈亏差异或资料缺口。

## 政策登记与切换

`policy_current` 读取激活编号、身份、参数和 `installed` 状态。`policy_guide` 省略 `path` 可列出指南，再按返回路径读取该版本。`policy_evaluation` 按 `evaluation_id` 查询留存的输入、结果和身份。

只有本机 owner 可登记与切换。用 `DATA_DIR` 指向已迁移实例，以私有环境变量 `ASHARE_OWNER_TOKEN` 提供凭据。登记使用已解析的实际参数 `risk_policy`，输入文件须属当前用户且无组和其他用户权限。

```sh
python -m a_share_research.policy.registry register --parameters /private/effective-parameters.json
python -m a_share_research.policy.registry current
python -m a_share_research.policy.registry activate policy:IDENTITY \
  --expected-id 0 --request-id unique-owner-decision --reason '切换依据'
```

`register` 保存身份。`activate` 首次用 `--expected-id 0`，此后使用当前激活编号。实装版本、参数有效期和账户范围须匹配；同一请求编号与内容复用原激活记录，同号改内容或预期编号过时会被拒绝。

## 定时运行与确认

启用账户工具的实例向 `assistant` 开放以下两个工具。服务端按 grant 编号保存运行记录。

| 工具 | 输入与回执 |
|---|---|
| `schedule_run` | 传 `job`、`slot` 受理或查回本时段。上海时间格式为止损 `stop_alert` 用 5 分钟整点 `YYYY-MM-DDTHH:MM`，日报 `daily_brief` 与扫描 `advisor_scan` 用日期，周报 `advisor_weekly` 用 ISO 周 `YYYY-Www`，月度对比 `benchmark` 与月批发起 `monthly_submit` 用月份 `YYYY-MM`，月批状态 `monthly_status` 用整点 `YYYY-MM-DDTHH`。 |
| `schedule_ack` | 传 `run_id` 确认本 grant 已输出的 `ready` 回执。成功返回 `acked`，同一编号可重复确认。 |

顾问扫描返回 `running` 与 `scan_id` 时，按原时段轮询。就绪行的 `text` 是最终推送正文，空串表示本时段静默。扫描或周报的政策评估失败时，`text` 保留已取得的披露或周报内容。采集失败返回 `failed`、`error_code` 与脱敏的 `error`，重试原时段会重新评估。服务用当时的生效激活编号保存政策评估和关联编号。过期与送达语义见 [Hermes 手册](../../ops/hermes/README.md)。

## 服务端监控与测算

先用 `policy_current` 取得 `expected_activation_id`，每次新观察提供新的 `request_id`。服务从核实的来源采集并保存输入，采集后重新核对凭据、政策和账户来源，再原子追加评估。生产来源为明确绑定的账本。采集子进程限时 25 秒；监控、日报和基准可使用 tools 的行情凭据。阅读评估时须同时查看实际时点和缺口。

| 工具 | 输入或结果 |
|---|---|
| `policy_monitor` | 保存持仓、资产观察、研究卡片、行情与监控判断。 |
| `policy_holdings_check` | 保存逐仓检查、P3 流动性延期证据与复核期限。读取逐项缺口判断资料是否齐全。 |
| `policy_position_size` | 附 `monitor_evaluation_id` 与 `proposal`（代码、价格、框架、可选现金），基于既有监控快照试算仓位。 |
| `policy_portfolio_scenario` | 附 `monitor_evaluation_id` 与 `proposal`，明确 `sales` 股数、`fees` 和可选现金，返回静态卖出情景。 |

仓位和情景工具核对父监控回执与当前来源，使用快照中的价格与分母。账户或政策变化后，应重新采集监控再测算。调用者提供的价格、股数及费用是测算条件。

### 交互式评估的政策切换与重试

采集先按原凭据和 `request_id` 查找回执。已有相同请求返回原结果；同号不同内容为 `conflict`。只有确定原请求未写入且原生效指针确已变化时，才返回 `policy_activation_changed`。一般身份、权限与账户来源冲突须按各自错误处理。

CLI 收到该确定拒绝时退出 20；暂停、繁忙或拒绝连接时退出 21；结果未知时退出 22，应先查回执再用原身份和原请求重试。定时任务由服务端处理政策切换后的重新受理。
