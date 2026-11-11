# 工具入口

HTTP 与进程内入口默认只读，单文件 CLI/MCP 客户端在 `scripts/ashare.py`；开发实例可显式开启外部研究和账户影子账本。生产服务尚未启用；发布编排已支持 tools 暂停排空，新材料取证与账户切换按 `docs/consolidation.md` 第 2 段继续实现。

开发验证使用已迁移至 v20 的数据库副本。服务启动不会创建库或执行迁移，固定监听回环地址 8621，最多并发两个调用：

```sh
DATA_DIR=/absolute/private/copy uv run python -m a_share_research.tools.server \
  --control-socket /absolute/private/control/control.sock
```

凭据由现有本地 `api_keys create NAME --role research|assistant|owner` 命令创建，只显示一次明文；开发时同样指定副本的 `DATA_DIR`。通过环境变量 `ASHARE_TOKEN` 提供给客户端，不在命令参数中传令牌：

```sh
python scripts/ashare.py tools
python scripts/ashare.py call research.card --json '{"code":"600519"}'
python scripts/ashare.py call policy.v0.read --json '{"command":"holdings"}'
```

`ASHARE_URL` 缺省为 `http://127.0.0.1:8621`，客户端仅允许回环 IP、禁止代理和重定向，不自动重试。

宿主可改用 `python scripts/ashare.py --config /absolute/private/client.json tools`，文件仅含 `url` 与 `token`，须为属主私有常规文件且路径无符号链接。显式配置覆盖本次调用的环境变量，不把令牌放在命令参数里；同一全局参数适用于 `call` 和 `mcp`。五宿主配置包、独立凭据路径与统一 Skill 见[客户端准备手册](../../ops/clients/README.md)，生成包不会安装或启用宿主。

### MCP stdio 客户端

宿主通过 `python /absolute/path/scripts/ashare.py --config /absolute/private/client.json mcp` 启动桥接进程，也可沿用私有环境注入 `ASHARE_TOKEN` / `ASHARE_URL`；不把令牌写进命令参数、仓库或共享配置。此单文件仅用标准库，不能直接读取数据库，也不持模型凭据。宿主安装与启用按整段验收另行执行。

支持 [MCP 生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)中的初始化、版本协商及 ping，协议版本为 2025-11-25 / 2025-06-18。[stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) 使用 UTF-8 JSON 行，stdout 仅输出协议消息；EOF 退出。单条输入上限 32 MiB，重复 JSON 键、非有限数字、批量请求和非法请求编号均拒绝。

`tools/list` 每次向 HTTP 服务发现完整列表，映射注册表输入/输出 Schema、只读及幂等提示；无需 cursor，不宣称列表变更通知。`tools/call` 直接交给服务校验角色和参数；[工具结果](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)同时提供 structuredContent 和 JSON 文本。服务拒绝或连接失败以 isError 返回安全错误，未知工具/不存在资源的 HTTP 404 转为协议参数错误；政策子命令非零退出也标为 isError，保留原结果。写入业务回执须继续核对其中的状态，HTTP/MCP 成功本身不代表已入账。

桥接串行处理请求，HTTP 超时 30 秒，不缓存权限、不重试写入，不提供资源、提示词、采样或后台任务。取消通知不承诺撤回已受理请求；连接断开/超时后应查询原回执，再按原请求标识重提。服务端仍按实时凭据、功能开关和暂停 Gate 受理，因此长期存活的宿主连接不能绕过撤销或发布排空。

- `research`：读取研究卡片及报告；看不到也不能调用账户和政策工具。
- `assistant`：研究读取，加成交时可用研究诊断、v0 五项 R0 读取命令。
- `owner`：本地政策登记与切换；影子开关启用时可读取政策身份、指南和保存的评估；没有 HTTP 切换工具，也不允许调用旧研究 API。

旧 Key 迁移后仍为 research。角色不可原地更改；更换角色须创建新 Key 并撤销旧 Key。assistant 发起的旧研究 API 请求仍交由隔离 worker 研究；外部草稿提交和独立记分使用下述单独开启的入口。

`GET /tools` 按角色返回注册表及输入/输出 JSON Schema。`POST /tools/{name}` 接收 JSON 对象，成功为 `{ok:true,result:...}`，错误为 `{ok:false,error:{code,message}}` 与对应非 200 状态码。业务载荷保留现行 research-v1 数据结构。进程内入口为 `registry.invoke(Context, name, arguments)`，执行相同角色和参数检查；调用者须提供与该凭据对应的 Context。

政策命令使用固定参数列表的子进程，不接收 shell、任意 argv 或环境参数；超时 30 秒，原 stdout、stderr 与退出码保留在结果中。HTTP 成功表示命令已执行，调用者还须检查 `exit_code`。当前不开放 R1 缓存刷新或任何 W1 写入。只读调用以 SQLite `mode=ro` 打开数据库；显式开启的写工具按请求使用 `mode=rw`。

`/health` 只表示进程存活。`/ready` 只读核对数据库版本、公共表及已启用工具需要的表，返回 database_ready/schema_version/paused/in_flight；暂停或数据库未就绪时为 503，不创建或迁移库。

进程内最多两个调用。运维经私有 Unix socket（目录 0700、socket 0600）调用 `python -m a_share_research.tools.control status|pause|resume --socket 路径 --timeout 30`；普通 HTTP 凭据没有暂停/恢复入口。pause 先拒绝新发现/调用，再等待在途请求归零，超时仍保持暂停。`.tools-paused` 标记在数据目录保存并跨重启生效；resume 核对并移除标记后恢复受理，未知文件或符号链接不会被覆盖/删除。

Compose 的 `tools` profile 复用 worker 镜像，独立 `.env.tools` 仅放 TUSHARE_TOKEN，没有 Pi 挂载或模型凭据；端口只映射宿主 127.0.0.1:8621，限制 0.4 CPU。三个功能开关 `ASHARE_TOOLS_EXTERNAL_ENABLED`、`ASHARE_TOOLS_EXTERNAL_REVIEW_ENABLED`、`ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED` 默认均为 0，仅接受 0/1，复核须同时启用外部研究。首次发布使用固定部署入口的 `--with-tools --allow-config`；已有 tools 容器会自动纳入后续发布，不得遗漏。生产启用仍需整段验收。

## 外部研究提交暂存（开发入口，默认关闭）

数据库迁移 v12 新增独立的 `external_research` / `external_submissions`，默认不把草稿送入 worker 队列。测试或开发实例须显式调用 `create_app(database, external_enabled=True)`；命令行服务仍默认关闭，未提供生产启用开关。

开放 `external.start`、`external.material`、`external.status`、`external.submit` 与 `external.review`（后者还需独立队列开关）。start 用 `source_run_id` 引用已完成 worker 研究的服务端材料，不接收客户端上传的材料或账户知情标记。当前只支持复用已有材料，新增受控取证尚待接入。材料以原始 JSON 保存（字段/页面顺序属于既有哈希合同）。

研究归创建 Key 所有；research 标盲研究、assistant 标账户知情；撤销凭据后不能再访问。提交带 `expected_version` 和 `draft`，机械检查逐项返回问题。草稿和材料哈希、检查结果及回执一起原子保存，每个提交版本不可修改或删除。同内容重提返回原回执（即使携带旧版本）；不同内容须匹配当前版本并生成新版本，旧版不覆盖。草稿幂等哈希使用规范 JSON，不冒充后续独立复核的绑定哈希。

`accepted=true` 仅表示通过机械检查，`review_status=not_queued` 明确尚未独立复核，不可当成正式报告或交易依据。暂存阶段不写 `runs`、`reports` 或冻结批次；显式开启独立复核后按下述流程处理。

## 独立复核与发布（尚未生产启用）

开发实例须同时显式设 `create_app(database, external_enabled=True, external_review_enabled=True)`，才能调用 `external.review`，参数为已通过机械检查的本人 `submission_id`。同一提交只生成一个 `kind=external` 任务。worker 需 `EXTERNAL_REVIEW_ENABLED=1` 才领取；2026-11-29 至 12-04 月批保护期内即使配置开启，也不接受新排队或领取外部任务。

复核只复用固定草稿和材料，不重新取证、成文或自动修稿；格式错误沿用现行协议最多修正一次。每版独立复核与发布，旧版不可覆盖。所有者任务优先，其次外部复核，最后批次研究；复核中断恢复为已提交，已记录调用数继续计入上限。报告和终态在同一事务提交，失败保留可核查问题，不展示未通过的正文。`external.status` 返回最新版本状态及各版复核任务/报告编号；报告仍可通过 `research.report` 读取。

发布正文带 `producer`（外部凭据编号、提交编号、账户知情身份）；作者明确为 external，不伪装成 worker 模型，复核模型另行记录。旧研究实验与论点卡默认池保持只选 `kind=research`。`external_scoring.load_forecasts/load_agenda` 要求生产者编号和账户知情身份，先过滤身份与判断契约再去重，复用既有 V1/V5 记分函数，不混合不同生产者。

完整性巡检检查外部材料/草稿哈希、复核任务与报告关联、生产者身份；发布停服指纹包含新表及已有账户表。第三服务暂停/排空和 Compose 配置已接入固定发布入口；真实生产发布、宿主与切换后回退仍待验收，不据模块测试提前启用生产。

## 账户影子账本（开发入口，默认关闭）

v14 新增期初与投影、批次回执、事件、作废记录、资产观察五张独立表；不改写旧持仓/成交表，不切换账户事实来源。开发实例须显式设 `create_app(database, account_shadow_enabled=True)`。账户工具仅 assistant 可发现和调用，research/owner 不可访问。

- `account.evidence` 接收 base64 原始证据（解码后最多 20 MiB），返回 SHA-256。文件原子写入数据库同目录下的私有 `account-evidence/`，重传幂等，读取验证哈希并拒绝符号链接路径。
- `account.state` 返回账本版本、持仓股数、现金及显式容差。建立期初目前仅供内部调用，必须提供期初、时点、证据和经确认的容差，不向助手开放猜测期初或修改容差的入口。
- `account.submit` 接收版本号、事件、原事件作废编号、证据哈希和完整期末快照；按业务身份去重，重新计算有效事件，校验股数、现金和总资产后事务入账。更正用作废加替代事件，保留原始内容。报告依据必须为同公司、不晚于成交日、通过复核的正式报告。
- `account.events` 按事件编号分页返回原事件及当前是否有效，可据此取得更正所需编号；`account.receipt` 查询不可变批次回执。
- `account.observe` 将不完整截图保存为资产观察，只记录明确提供的金额，不推断缺失交易；重复提交复用观察编号。

观察和完整快照均可提供 `portfolio_value_as_of`，只在原始证据明确给出估值时点时填写。须为完整、带时区的日期时间，程序统一为上海时区；日期、无时区时间和数字时间戳均拒绝。观察同时须有 total_assets，且 observed_at 明确时区、不早于估值时点；快照的时点不得早于期初或本次有效事件。未来估值拒绝：观察返回 422，快照返回 conflict 回执且不写交易。记录时间保留必要的微秒精度。

字段缺省或 null 时，序列化完全省略该字段，旧请求哈希和重复回执保持不变。补充或更正时点生成新观察/批次，不改旧记录；不能把观察时间或文件保存时间复制成估值时点。已保存的明确时点进入监控来源及分母证据，仍须按其实际日期判断是否过期。

提交结果的 `data.status` 为 `posted`、`missing_material`、`conflict` 或 `service_failure`。HTTP 200 只表示返回了业务回执，必须检查此状态。成功同请求重提复用原回执；失败批次可补齐条件后重试。SQL 入账失败先回滚，数据库仍可写时单独持久化失败回执；数据库不可用时返回 HTTP 503，不声称已存回执。所有失败均不改变交易投影。HTTP 拒绝重复 JSON 键和非有限数字，避免重复持仓键被静默覆盖。

完整性巡检检查事件归属、业务身份、作废关系、回执和投影重放，发布数据库指纹覆盖五张新表。物理证据备份与独立目录恢复见 [README](../../README.md)，成本、生命周期及旧表投影见下文；分析状态生产接入、完整券商流水对账和生产切换验收仍需完成。


## 分析状态追加历史（影子入口）

v15 的 `account_analysis_history` 按持仓周期 `holding_id` 保存完整快照，包含 L3、预警、Tier 和论点版本；每次追加关联前一版本、创建凭据、请求/快照哈希和证据。当前值取最新追加行，历史不可更新或删除。同公司不同持仓周期不会合并。内部 `analysis.initialize` 原样保存一次旧表基线，明确标为 `legacy_baseline`，保留空证据和退休条件，不把当前快照当成已恢复的历史。

五个工具沿用 `account_shadow_enabled` 开关，仅 assistant 可用：

- `account.analysis.preview`：读取已确认基线、当前镜像、影子快照及当前版本/镜像哈希，供协调前核对。
- `account.analysis.rebase`：按持仓周期显式选择保留影子或采用镜像，带证据与理由原子追加协调历史。
- `account.analysis.current`：读取指定持仓周期最新影子快照。
- `account.analysis.history`：按修订编号分页读取完整历史及来源。
- `account.analysis.update`：传 `holding_id` 及 `update`（`expected_version`、`evidence_hashes`、`reason`、`changes`）；支持活动 L3 的状态/证据/复核日期、论点整组重写、预警开启/更新/pending/resolved，以及未豁免 Tier 的 pending/completed 状态。一个请求内多项变更全成全败，同请求重提返回原修订，版本冲突返回 409。

新写入须引用已存证据；已关闭持仓、退休 L3、已解除预警不能更新。买入时豁免和路径配置不属于普通状态更新。发现旧镜像基线或公司身份已变化时拒绝继续叠加，等待核对，不静默合并。所有操作只写影子历史，不改旧四表或成交。旧宿主仍未切换，不能据此取消生产流程的确认要求。


`changes` 中的 `kind=thesis` 接收 `l1`、`l2`、`rewrite_reason`、`retire_l3_ids` 和 `new_l3`。退休编号必须覆盖该持仓周期全部活动条件，整批保存旧论点 superseded、旧 L3 退休及新论点/新 L3；新条件初始为 pending。每个新条件须提供 condition/scope/action/materiality_basis/temporary_exit_rule，可选 evidence_cadence；non_core 只能 review。论点重写与 L3 状态更新分批提交，避免同批先退休再修改的顺序歧义。新实体编号跨持仓唯一，并保留旧库已发放编号的上限。

`kind=alert_open` 接收 level/category/reason_code/reason/evidence/review_due。未解除的同原因预警复用实体，pending 不会被重复开启改为 active；流动性延期预警已有复核期限不延后。已解除后再次出现同原因则创建新实体。复核期限接受日期或带时区时间。

开发验证可显式设置 `POLICY_V0_ANALYSIS_SHADOW=1` 后运行 v0 只读命令。连接内临时表保留旧 schema 和索引，并投影最新分析历史，因此原监控、L3 和预警读者不用改计算规则；主库以只读模式打开，投影完成后禁止临时表写入。所有活动持仓须有一致基线，缺失、身份漂移或快照哈希不一致会拒绝读取；已关闭周期保留旧投影。该开关不自动注入生产或工具子进程，默认旧来源不变。生产读源切换仍待验收。


镜像刷新后的协调先调用 `account.analysis.preview`，再向 `account.analysis.rebase` 提交 `rebase` 对象：`choices` 每项包含 holding_id/expected_version/legacy_hash/select（legacy 或 shadow），批次提供 evidence_hashes/reason。每个周期选择整组状态；需要组合判断时，可先采用镜像，再用有证据的状态更新或论点重写表达保留意见。服务按请求时版本和预览哈希核对，任何漂移或失败整批回滚；同请求重试复用原回执，即使镜像之后再次更新。

协调以 `legacy_rebase` 追加，保存本次镜像全文、选择、来源凭据和证据，最初基线与旧影子版本保持可查。后续更新和只读投影核对最近确认的镜像。新增编号若与其他持仓周期冲突，会列出冲突周期，允许在同一批协调多个周期；整批候选投影的实体编号必须唯一。公司身份变化、已关闭周期或损坏的基线拒绝协调。完整性巡检检查协调内容及跨周期回执齐全。


## 旧持仓周期重放诊断

`account.lifecycle.replay` 接收 `holding_id`，沿用 assistant 权限与 `account_shadow_enabled` 开关，只读返回原持仓/事件、重放字段、逐字段差异及卖出盈亏差异。经济引擎按原 event_date/id 顺序用 Decimal 计算加权成本、股数、分红、卖出净额及已实现盈亏；v0 适配层计算参考成本和主仓位字段。买入费用进入经济成本，单独现金分红保留为收入。日期精度、推断事件标记与原 notes 随结果保留。

除权调整需可解释的 DPS/送转记录，并交叉核对实际股数和调整后参考成本；无法解释时返回 `status=incomplete`。同日分红逐笔保留，同时列出 DPS 推导额与同日现金记录差异，关联状态为未记录。旧止损线从原快照保留，当前政策重算只用于列出差异；历史政策身份未记录时不会假定已知。

结果的 `projection_scope=historical_lifecycle`。经济金额以十进制字符串输出；保存字段和事件推导字段分别列明。关闭周期原表残留股数会在 differences 中显示。此入口是旧历史诊断；新 journal 的持续投影还需显式期初成本、周期身份和新开仓政策元数据。历史回放一致不能补足账户期初现金、资金划转或真实费用证据，也不能替代完整券商对账。


## 新账本成本与周期投影

v16 新增 `account_position_books`。内部 `position_store.initialize` 在首笔金融事件之前，把有证据的成本期初与账户股数期初绑定：每个周期提供 cycle_id/code/shares/cost_price/buy_date/policy_identity，可携旧持仓编号 legacy_holding_id。股数必须一致，买入日不得晚于账户期初；期初不可覆盖。同内容重复初始化返回当前投影。policy_identity 保存来源引用，政策参数与行动尚未据此执行。

`account.state.cost_basis_configured` 表示是否已配置；`account.positions` 返回版本和成本周期投影。现金流字段明确为 `since_opening`：期初之前的投入、分红和已实现收益均不反推。影子账本尚未绑定成本时，读取成本会返回明确缺口。

配置后，`account.submit` 同事务重建所有有效事件的成本与周期、核对股数，并更新现金/股数/成本版本；作废更正一起重放。成功回执带 position_before/position_after。加仓费用税费进入加权成本，部分卖出保留每股成本并累计已实现盈亏，全部卖出关闭周期，下次买入创建由业务身份确定的新周期。失败批次不改变这些投影，SQL 故障沿用回滚和可查询失败回执。

新开仓须在 batch.position_entries 中，以本批买入事件的字符串下标提供 policy_identity（可选 name），例如 `{"0":{"policy_identity":"已核对的政策来源引用"}}`。缺少上下文返回 missing_material。已有周期的上下文不能通过重复成交覆盖，须作废更正；空扩展字段不改变旧请求哈希。

分红可以用 batch.position_dividends 将本批事件下标绑定明确 cycle_id。同股票已有多个周期或没有活动周期时必须绑定；允许向已关闭周期记分红，周期须匹配公司且已在付款前存在。股数和退出日期保持原值。业务身份、周期和元数据均保留在不可变批次，重复请求不会重复计入收益。

当前成本投影的 policy_evaluation 为 pending；账户层不执行投资规则。下面的独立适配层可显式提供 v0 旧表形状，生产账户来源继续按切换验收控制。

## v0 政策期初与旧表读取（影子入口）

v17 新增不可变周期/事件整数编号、政策投影参数和期初快照。内部 `policy.v0.account_store.initialize` 在首笔金融事件前绑定成本期初与完整旧持仓/事件快照，要求证据、在仓周期、公司、股数、成本、日期一致，并具备参考成本、初始股数、主仓位和已保存止损字段。旧历史所有列原样保留；后续镜像刷新不会自动覆盖期初。

`policy.v0.positions` 接收 account_id，使用 assistant 权限与账户影子开关，返回 holdings/holding_events、账本版本、期初哈希和 profile_identity。该 profile 记录当前投影算法哈希及止损/主仓位参数，仅供此适配器复算；完整投资政策身份与追加生效指针见下节；此投影 profile 不自动升级为完整身份，真实账户切换仍待验收。

新开仓的 position_entries 除 policy_identity 外，须明确 framework，可提供 framework_confident 和 name。policy_identity 须引用已登记 profile；金融事实可独立入账，但缺少这些政策条件时政策读取明确报缺口。旧周期先保留保存的止损线，买入或除权触发参考成本变动时才按绑定参数重算。

`batch.position_distributions` 以事件下标提供每股现金除权金额，仅绑定分红或送转事件。到账现金与每股除权是独立事实，不能彼此反推；没有每股信息的分红只计收入。已有元数据不能通过重复事件补加或覆盖，须作废更正；同批同业务事件的元数据冲突会被拒绝。新增可选字段为空时保留旧请求哈希。

显式设置 `POLICY_V0_ACCOUNT_SHADOW=账户编号` 后，v0 连接在同一只读事务内生成 TEMP holdings/holding_events，再启用 query_only；可与 `POLICY_V0_ANALYSIS_SHADOW=1` 联用。主库旧表不改，未设置时沿用旧来源。编号在金融入账事务内固定，读请求不分配编号，作废后也不复用。账户周期分析读取规则见下节；宿主流程和生产切换仍须整段验收。

## 账户周期分析分支（影子入口）

v18 在同一 `account_analysis_history` 表增加 account_id/cycle_id 作用域。旧记录保持原列值、编号、哈希和父链，旧工具只读取空作用域；不同账户即使使用相同持仓整数编号，也不会混用状态。新入口均要求 assistant 及账户影子开关：

- `account.cycle_analysis.initialize`：提供 account_id、cycle_id，以及 initialize 对象中的 expected_account_version、evidence_hashes。新周期从空论点/L3/预警和 pending Tier 开始，不借用上一个周期的判断。已有期初周期还须提供 legacy_revision_id，明确选择其当前旧分析版本；导入快照与来源版本完整保留。
- `account.cycle_analysis.current/history`：读取指定账户周期的最新值或分页历史。周期清仓或因交易作废失效后，历史仍可查询。
- `account.cycle_analysis.update`：提供 account_id、cycle_id、update，沿用既有 expected_version/evidence_hashes/reason/changes 合同和论点、L3、预警、Tier 转移逻辑；该版本号属于分析分支，与初始化核对的账户版本分开。一次提交原子追加，重复请求返回原回执，关闭或失效周期拒绝新更新。

已导入分支独立追加，旧镜像或旧分析后续更新不会自动覆盖它。基线不能重建覆盖，旧来源始终可追溯。没有显式导入分支的期初周期继续使用既有旧分析投影；新在仓周期缺少分析基线时，双投影读取明确拒绝，不拼接其他账户或旧周期的分析。双投影先生成持仓，再按账户/周期加载分析，最后封闭写入；已关闭周期保留其分支的最后快照供历史读取。

未识别作用域的旧工具版本会混读新分支，不能作为含新分支数据库的回退目标。按合一方案，正式切换后的前一级回退镜像也必须能读写新账本；回退保留新账本，不恢复旧 Skills 或旧账户库。证据备份与独立目录恢复已接入 [README](../../README.md)，镜像兼容性及切换后回退仍待演练；生产目前沿用旧账户来源。


## 完整政策身份、生效指针与固定输入重放（影子入口）

v19 增加不可变 `policy_versions` / `policy_evaluations`，扩展既有只追加的 `policy_activations`。完整身份由当前安装的应用 Python 源码及 `uv.lock`、实际生效参数、规则索引/指南规则和全部指南内容共同确定。代码范围包含共享运行时，所以任何应用源码变更都须显式登记新身份；旧身份保留供历史核验，不会随升级静默改写。原 `v0:` 投影 profile 继续用于历史成本投影，不能代替完整 `policy:` 身份。

登记参数须为 `risk_policy.resolve_risk_parameters(...).risk_policy` 的实际解析结果（固定输入包中的 `risk_policy` 同形），包括来源与适用范围，不能直接把未经解析的原 `risk-policy.json` 当成运行参数。保存参数的本地文件须由当前用户持有且无组/其他用户权限；不得把账户参数提交到仓库。

只有本地命令可登记及切换。以私有环境变量 `ASHARE_OWNER_TOKEN` 传 owner Key，`DATA_DIR` 选已迁移的数据库；命令不会创建库或执行迁移：

```sh
python -m a_share_research.policy.registry register --parameters /private/effective-parameters.json
python -m a_share_research.policy.registry current
python -m a_share_research.policy.registry activate policy:IDENTITY \
  --expected-id 0 --request-id unique-owner-decision --reason '明确的切换依据'
```

`register` 不切换指针。`activate` 首次 expected-id 为 0，此后为 current 返回的编号；并发变化拒绝，重复 request-id/内容返回原记录且不会退回旧指针。同一请求标识改内容会冲突。实装哈希不符、参数尚未生效/已到期或账户作用域未确认均拒绝启用。参数的旧历史重放仍按输入包的评估时钟核对。旧无完整身份的指针保存原列和原值，读取明确报缺口，不自动补造身份。

显式账户影子开关下开放四项工具：

- `policy.current`（assistant/owner）：读取生效身份、实际参数、生效时间及 installed 状态；没有指针时返回空，代码变更后 installed=false。
- `policy.guide`（assistant/owner）：省略 path 发现指南列表，传返回的 path 读取已保存的确切版本；不接受任意文件路径。指南是历史政策资料，文中的旧工作流不能覆盖当前宿主授权。
- `policy.replay`（assistant）：提交 identity、request_id、bundle，对完整 JSON 固定输入包执行 v0 `build_monitor_snapshot`，原样保存 monitor-v1 输出、原输入、两者哈希及实际政策身份。若该身份正生效，同时记录对应 activation_id。输入风险参数必须与身份完全一致，运行时不接受 pickle、不取行情、不调用模型。
- `policy.evaluation`（assistant/owner）：按 evaluation_id 读取原输入、结果及不可变回执。

`bundle` 包含 local、quotes、portfolio_value、industry_result、now、risk_policy、denominator_evidence、account_scope_required、research_result；数据类以对应 JSON 字段表示，now 必须带时区，缺失和降级语义沿用 v0。`policy.evaluation.pack` 可把已受信内存输入转为 JSON；它不反序列化可执行内容。重放同请求幂等，参数/输入变化冲突，写入失败整体回滚。

结果明确标记 `mode=supplied_input_replay`：仅证明给定输入的程序结果，不证明输入是真实/当前账户或行情，也不参与收益比较。当前生效指针用于新指南与重放归属，所有者网页 `/policy` 只读展示登记、参数与账户来源；旧生产监控和持仓来源仍沿用切换前入口，所有生产评估入口统一留存与宿主激活尚待接入。镜像与明确绑定账本的采集见下节。切换新指针不改变旧周期已绑定的投影参数，不重新解释历史交易。


## 服务端监控采集（镜像或明确绑定账本，开发入口）

`account.current` 接受空对象，要求 assistant 与账户影子开关，返回 `data.codes`（去重排序的六位公司代码）和 `data.source`。同一只读事务内复用监控的来源核验，镜像读取未关闭持仓，账本读取明确绑定账户的已核验资金投影；来源缺失、投影或证据损坏、不可转换的代码均拒绝，不用空持仓掩盖失败。不查询行情、不依赖生效政策、不修改订阅，供宿主在扫描前和通知前刷新当前在仓集合。

`policy.stop_alert` 同样要求 assistant 与账户影子开关，输入为 request_id、expected_activation_id 和 previous_state（旧通知 `{date,levels}`，也接受空对象）。固定子进程只采集未关闭持仓的 canonical 止损线与当前行情；非 v0 交易时段不取行情。服务复用政策/来源/撤销检查、无网络写锁与幂等追加，保存包含参数、时钟、持仓止损线、行情和旧通知状态的原始输入及纯函数结果。回执 mode 为 `service_stop_alert_capture`，capture_context 版本 3 的 request 还绑定 previous_state；同请求不能换状态或与 monitor/replay 混用。结果固定为 status、exit_code、stdout、stderr、state、alerts；state 是下一通知状态，服务不代发通知或改调用方状态。

止损规则不从新监控结论近似推导：直接复用 v0 的 stop_level/new_stop_alerts，保留同日与绝对 15 分钟行情窗、3% 临近线、逐代码每日升档、部分行情有效时只检查有效部分的既有行为。全部行情无效返回已保存的 unavailable 评估（exit_code=1），与采集器异常、来源变动等未保存成功回执的服务错误区分。可用 `policy.evaluation` 查回原输入/结果/身份；纯函数 `policy.stop_evaluation.evaluate` 可离线复算。无数据库列变更，但旧版本不识别上下文 3，不可作为这些记录的直接回退版本；发布前仍须兼容合并镜像演练。

`policy.daily_brief` 接受 request_id / expected_activation_id，同为 assistant、账户影子开关及评估追加入口。采集沿用监控输入但不加入旧 daily-brief 未读取的研究卡片；另保留每个未关闭持仓的完整生命周期事件、Provider 规范化分红行、来源/获取时间/源日期与安全错误类别、分红检查日和格式化日。成功空行集与失败 null 行集区分；逐持仓身份、代码、名称、股数必须与监控输入一致，遗漏或重复核查项拒绝。新旧入口共用从旧函数原样抽出的 implemented_rows，再由 unrecorded_dividends 和 format_daily_brief 复算，不采用调用方提供的计算结论。

日报回执 mode=`service_daily_brief_capture`、上下文版本 4，原始输入根部仍保存 risk_policy。结果包含 status（ready/silent）、exit_code、stdout、stderr、snapshot 及 dividends.checked/findings/failures；无当日报价时保留 v0 静默行为，失败项仍可查。可由 `policy.daily_evaluation.evaluate` 离线复算，避免旧监控对嵌套 L3 的注解改变原始留存输入。采集仍在固定 25 秒预算内；只有此固定子进程允许从 tools 的环境接收 TUSHARE_TOKEN，其余模型/owner/宿主配置不传入。分红 Provider 异常逐持仓记录安全类别，不回显异常正文；数据库/来源/政策漂移等仍使整体回执失败。未新增数据库列，兼容回退版本必须识别上下文 4。

v20 为 `policy_evaluations` 增加 capture_context 与其哈希，旧重放行原字段不改。`policy.monitor` 要求 assistant 凭据和账户影子开关，参数只有 request_id 与 expected_activation_id（由 `policy.current` 读取）。服务核对当前实现与生效政策及 `account_source`，随后用固定子进程读取对应账户、资产观察和研究卡片，再调用 v0 现有的 Sina 行情与本地行业缓存读取。调用者不能传账户内容、风险参数、命令、路径或环境变量。

mirror 模式保持原逻辑：核对镜像全量哈希及相邻资产原件，来源上下文版本为 1。ledger 模式要求 watermark 严格包含 `schema_version=1`、`account_id`、`account_scope`、`mirror_watermark` 四项；账户范围必须与生效政策一致，不能猜唯一账本。`source_sha256` 与 mirror_watermark 保留末次镜像来源；活动账本另外生成 state_sha256。当前只有该绑定的读取和验证，没有生产切换写入器，私有测试中设置绑定不算完成切换。

ledger 采集校验资金与成本重放、政策期初、编号、活动周期的账户作用域分析及全部关联证据，不能回退旧分析基线。来源摘要覆盖相关账本记录、分析、资产观察、已入账批次、实际政策投影与财务缓存；观察或无成交快照改变时即使账本版本不变也会改变摘要。子进程仅对明确绑定账户启用连接内双投影，前后再次核对来源。

资产分母取最新保存的有总资产观察或已对账快照，验证记录哈希、回执和实际证据文件。跨来源同秒且金额冲突时拒绝猜先后；同金额优先已对账快照，同来源用记录编号确定顺序。缺资产金额就保持未知，绝不读旧 `account-snapshot.json`。明确提供的 portfolio_value_as_of 原样传入分母证据，并核对不晚于记录时间；旧记录或未提供时点时仍为 unknown，观察时间、记录时间均不替代它。来源记录编号和证据哈希保留在上下文的 asset 中。

子进程接收生效身份保存的参数，ledger 时另带已核对的来源上下文，使用单独的空私有配置，不加载宿主 risk-policy.json，不继承模型、owner 或行情令牌。实际资产分母与其账户范围、记录时间和缺失估值时点原样保留；参数范围不匹配或尚未生效时拒绝。未取得行情、行业缓存过期和旧财务缓存等缺口沿用 monitor-v1 的 data_status/逐项缺口，不把采集调用成功等同于证据完整。

采集不持写锁。返回后再次核对当前 Key、生效指针和账户来源，任何漂移都拒绝保存；通过才原子追加评估及来源上下文。正常结果标为 `mode=service_monitor_capture`，context 包括来源哈希/更新时间/水位、请求和采集起止时间；ledger 的 context 版本为 2，另带账户版本、状态摘要及资产来源。旧回执只核验保存的结构和哈希，不要求仍等于当前账户。它表示服务端执行了采集，不保证每项行情都最新；必须继续查看原输入的来源时点及结果缺口。

同一 Key 的同 request_id 返回第一次已提交回执，不重新采集；希望获得新观察须使用新 request_id。并发重复请求可能各自取一次行情，但只保存并返回第一份回执。request_id 与不同操作/生效指针复用会冲突。子进程 25 秒超时后清理自身进程组，失败无成功回执；HTTP/CLI 超时后可用原请求查询重试，不假定已写入。`policy.evaluation` 同样可读取这类结果，`policy.replay` 不会把同请求的服务采集冒充为用户提供输入的重放。
