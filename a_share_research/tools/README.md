# 工具入口

HTTP 与进程内入口默认只读，单文件客户端在 `scripts/ashare.py`；开发实例可显式开启外部研究和账户影子账本。生产服务尚未启用；发布编排、MCP、新材料取证与账户切换按 `docs/consolidation.md` 第 2 段继续实现。

开发验证使用已迁移至 v14 的数据库副本。服务启动不会创建库或执行迁移，固定监听回环地址 8621，最多并发两个调用：

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

完整性巡检检查事件归属、业务身份、作废关系、回执和投影重放，发布数据库指纹覆盖五张新表。物理证据备份/恢复、旧成本与持仓生命周期投影、分析状态追加、完整券商流水对账和生产切换验收仍需完成。
