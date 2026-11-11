# 工具入口

HTTP 与进程内入口默认只读，单文件客户端在 `scripts/ashare.py`；开发实例可显式开启外部研究和账户影子账本。生产服务尚未启用；发布编排、MCP、新材料取证与账户切换按 `docs/consolidation.md` 第 2 段继续实现。

开发验证使用已迁移至 v15 的数据库副本。服务启动不会创建库或执行迁移，固定监听回环地址 8621，最多并发两个调用：

```sh
DATA_DIR=/absolute/private/copy uv run python -m a_share_research.tools.server
```

凭据由现有本地 `api_keys create NAME --role research|assistant|owner` 命令创建，只显示一次明文；开发时同样指定副本的 `DATA_DIR`。通过环境变量 `ASHARE_TOKEN` 提供给客户端，不在命令参数中传令牌：

```sh
python scripts/ashare.py tools
python scripts/ashare.py call research.card --json '{"code":"600519"}'
python scripts/ashare.py call policy.v0.read --json '{"command":"holdings"}'
```

`ASHARE_URL` 缺省为 `http://127.0.0.1:8621`，客户端仅允许回环 IP、禁止代理和重定向，不自动重试。

- `research`：读取研究卡片及报告；看不到也不能调用账户和政策工具。
- `assistant`：研究读取，加成交时可用研究诊断、v0 五项 R0 读取命令。
- `owner`：预留管理身份，当前没有开放的管理工具，也不允许调用旧研究 API。

旧 Key 迁移后仍为 research。角色不可原地更改；更换角色须创建新 Key 并撤销旧 Key。assistant 发起的旧研究 API 请求仍交由隔离 worker 研究；外部草稿提交和独立记分使用下述单独开启的入口。

`GET /tools` 按角色返回注册表及输入/输出 JSON Schema。`POST /tools/{name}` 接收 JSON 对象，成功为 `{ok:true,result:...}`，错误为 `{ok:false,error:{code,message}}` 与对应非 200 状态码。业务载荷保留现行 research-v1 数据结构。进程内入口为 `registry.invoke(Context, name, arguments)`，执行相同角色和参数检查；调用者须提供与该凭据对应的 Context。

政策命令使用固定参数列表的子进程，不接收 shell、任意 argv 或环境参数；超时 30 秒，原 stdout、stderr 与退出码保留在结果中。HTTP 成功表示命令已执行，调用者还须检查 `exit_code`。当前不开放 R1 缓存刷新或任何 W1 写入。只读调用以 SQLite `mode=ro` 打开数据库；显式开启的写工具按请求使用 `mode=rw`。

`/health` 仅表示进程响应，不等价于数据库就绪或发布验收；生产发布接入时还需迁移、在途排空、就绪核对与回退演练。

## 外部研究提交暂存（开发入口，默认关闭）

数据库迁移 v12 新增独立的 `external_research` / `external_submissions`，默认不把草稿送入 worker 队列。测试或开发实例须显式调用 `create_app(database, external_enabled=True)`；命令行服务仍默认关闭，未提供生产启用开关。

开放 `external.start`、`external.material`、`external.status`、`external.submit` 与 `external.review`（后者还需独立队列开关）。start 用 `source_run_id` 引用已完成 worker 研究的服务端材料，不接收客户端上传的材料或账户知情标记。当前只支持复用已有材料，新增受控取证尚待接入。材料以原始 JSON 保存（字段/页面顺序属于既有哈希合同）。

研究归创建 Key 所有；research 标盲研究、assistant 标账户知情；撤销凭据后不能再访问。提交带 `expected_version` 和 `draft`，机械检查逐项返回问题。草稿和材料哈希、检查结果及回执一起原子保存，每个提交版本不可修改或删除。同内容重提返回原回执（即使携带旧版本）；不同内容须匹配当前版本并生成新版本，旧版不覆盖。草稿幂等哈希使用规范 JSON，不冒充后续独立复核的绑定哈希。

`accepted=true` 仅表示通过机械检查，`review_status=not_queued` 明确尚未独立复核，不可当成正式报告或交易依据。暂存阶段不写 `runs`、`reports` 或冻结批次；显式开启独立复核后按下述流程处理。

## 独立复核与发布（尚未生产启用）

开发实例须同时显式设 `create_app(database, external_enabled=True, external_review_enabled=True)`，才能调用 `external.review`，参数为已通过机械检查的本人 `submission_id`。同一提交只生成一个 `kind=external` 任务。worker 需 `EXTERNAL_REVIEW_ENABLED=1` 才领取；2026-11-29 至 12-04 月批保护期内即使配置开启，也不接受新排队或领取外部任务。

复核只复用固定草稿和材料，不重新取证、成文或自动修稿；格式错误沿用现行协议最多修正一次。每版独立复核与发布，旧版不可覆盖。所有者任务优先，其次外部复核，最后批次研究；复核中断恢复为已提交，已记录调用数继续计入上限。报告和终态在同一事务提交，失败保留可核查问题，不展示未通过的正文。`external.status` 返回最新版本状态及各版复核任务/报告编号；报告仍可通过 `research.report` 读取。

发布正文带 `producer`（外部凭据编号、提交编号、账户知情身份）；作者明确为 external，不伪装成 worker 模型，复核模型另行记录。旧研究实验与论点卡默认池保持只选 `kind=research`。`external_scoring.load_forecasts/load_agenda` 要求生产者编号和账户知情身份，先过滤身份与判断契约再去重，复用既有 V1/V5 记分函数，不混合不同生产者。

完整性巡检检查外部材料/草稿哈希、复核任务与报告关联、生产者身份；发布停服指纹包含新表及已有账户表。第三服务暂停/排空、生产 compose 与回退切换仍未完成，不据模块测试提前启用生产。

## 账户影子账本（开发入口，默认关闭）

v14 新增期初与投影、批次回执、事件、作废记录、资产观察五张独立表；不改写旧持仓/成交表，不切换账户事实来源。开发实例须显式设 `create_app(database, account_shadow_enabled=True)`。账户工具仅 assistant 可发现和调用，research/owner 不可访问。

- `account.evidence` 接收 base64 原始证据（解码后最多 20 MiB），返回 SHA-256。文件原子写入数据库同目录下的私有 `account-evidence/`，重传幂等，读取验证哈希并拒绝符号链接路径。
- `account.state` 返回账本版本、持仓股数、现金及显式容差。建立期初目前仅供内部调用，必须提供期初、时点、证据和经确认的容差，不向助手开放猜测期初或修改容差的入口。
- `account.submit` 接收版本号、事件、原事件作废编号、证据哈希和完整期末快照；按业务身份去重，重新计算有效事件，校验股数、现金和总资产后事务入账。更正用作废加替代事件，保留原始内容。报告依据必须为同公司、不晚于成交日、通过复核的正式报告。
- `account.events` 按事件编号分页返回原事件及当前是否有效，可据此取得更正所需编号；`account.receipt` 查询不可变批次回执。
- `account.observe` 将不完整截图保存为资产观察，只记录明确提供的金额，不推断缺失交易；重复提交复用观察编号。

提交结果的 `data.status` 为 `posted`、`missing_material`、`conflict` 或 `service_failure`。HTTP 200 只表示返回了业务回执，必须检查此状态。成功同请求重提复用原回执；失败批次可补齐条件后重试。SQL 入账失败先回滚，数据库仍可写时单独持久化失败回执；数据库不可用时返回 HTTP 503，不声称已存回执。所有失败均不改变交易投影。HTTP 拒绝重复 JSON 键和非有限数字，避免重复持仓键被静默覆盖。

完整性巡检检查事件归属、业务身份、作废关系、回执和投影重放，发布数据库指纹覆盖五张新表。物理证据备份/恢复、旧成本与持仓生命周期投影、分析状态生产接入、完整券商流水对账和生产切换验收仍需完成。


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
