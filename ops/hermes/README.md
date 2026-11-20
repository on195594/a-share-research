# Hermes 定时脚本
源文件只在本目录维护；宿主安装目录为 `~/.hermes/scripts/cron/`，使用复制，不使用符号链接。合一替代脚本已安装，账户任务通过统一服务读取新账本；止损与日报实际入口已通过运行检查。当前调度、正式送达与剩余验收统一见[实施计划](../../docs/implementation-plan.md)。后续同步使用下文安装入口；它会整包更新止损、日报、月度对比与顾问脚本，不能把生成准备包当作已安装。
任务与计划取自[上线后接入](../../docs/implementation-plan.md#当前部署)（上海时间；该节只记录任务 ID，未记录具名任务名称）。
| 脚本（共用入口） | Hermes 任务 | 计划 |
|---|---|---|
| `a-stock-stop-alert.sh`（`a-share-stop-alert.py`） | 止损提醒 `ca568f56d9ef` | 工作日 09–15 点每 5 分钟，服务内判断交易时段 |
| `a-stock-daily-brief.sh`（`a-share-daily-brief.py`） | 持仓日报 `7ab0ded3121c` | 工作日 15:35 |
| `a-stock-benchmark-monthly.sh`（`a-share-benchmark.py`） | 账本月度对比 `93c4f5a4d0fc` | 每月 1 日 09:00 |
| `a-share-advisor.sh`、`a-share-advisor-weekly.sh`（`a-share-advisor.py`） | 顾问扫描 `fbb0cbf6ee60` / 周报 `7845e96f4184` | 工作日 16:37 / 周日 18:07 |
| `a-share-monthly-submit.sh`、`a-share-monthly-status.sh`（`a-share-monthly.sh`、`a-share-monthly.py`） | 月批发起 `3d8b860808e4` / 状态 `112633a18054` | 每月 1 日 20:00 / 每月 1–3 日每小时 :30 |
| `a-share-scoring.sh`、`a-share-scoring.py` | 记分 `2c0e2f798501` | 周日 19:07 |
| `a-share-milestones.sh`、`a-share-milestones.py` | 里程碑进度 `6b6ecd1783ea` | 周一 09:03 |
Hermes 业务脚本经 `ashare` 调用 MCP。唯一凭据文件为 0600 的 `~/.config/ashare/hermes-assistant.json`，含 `url` 和 `token`。
去重回执：`~/.local/state/a-share-advisor/opportunities.json`、`~/.local/state/a-share-monthly/<月>.receipt`、`~/.local/state/a-share-scoring/state.json`。
核对：`python3 ops/hermes/install.py --check`；安装：`python3 ops/hermes/install.py --apply`（须先提交本目录改动，自动复核）。
可加 `--target <目录>`；安装移除已退役的 `a-share-k7-progress.*`、第 3 段临时对照 `a-share-shadow-diff.sh` 及已完成提醒脚本，其他非清单文件不动；Hermes 任务入口需同步为新名字。

## 临时接续提醒

`a-share-remind-monthly.sh`（任务 `1f1ead8a8a62`，计划 2026-12-04 10:00）只输出交给开发 Agent 的固定提示词，不调用模型或执行运维操作。第 3 段临时对照任务 `85806e8e4ea1` 及已完成的发布、切换、收尾三条提醒（`ba792c50aef5`、`4c5639ca558e`、`ba87fe98944e`）与对应脚本已于 2026-11-13 清理退役，仅保留待执行月批核验提醒；计划与维护记录见[实施计划](../../docs/implementation-plan.md#合一重构收尾)。

## 合一替代调用与切换核对

下表记录本目录脚本的调用、去重与预算合同；任务时间、`no_agent` 模式和通知目标沿用原配置。三个 `a-stock-*` 包装脚本保留原入口名，内部调用统一服务。实际耗时与定时送达状态见[实施计划](../../docs/implementation-plan.md)。

| 任务 | 调用与替代状态 | 去重状态 | 命令预算与回退边界 |
|---|---|---|---|
| 止损提醒 | 新脚本 `policy_current` → `policy_stop_alert`；服务保存原始行情、止损线、旧通知状态及评估 | 新 `~/.local/state/ashare-stop-alert/state.json`，切换时从已确认的旧 stop-alert-state.json 导入 | ashare 子进程 40 秒，HTTP 30 秒，服务采集 25 秒；切换后只回兼容新账本及此回执/状态的合并版 |
| 持仓日报 | 新脚本 `policy_current` → `policy_daily_brief`；监控、逐持仓完整事件与 Provider 分红响应/失败共同留存 | `~/.local/state/ashare-daily-brief/state.json` 仅记录待查请求，无新增每日通知去重 | ashare 子进程 40 秒、HTTP 30 秒、服务整体采集 25 秒；真实账户日报脚本已成功运行；正式定时送达待观察 |
| 账本月度对比 | 新脚本 `policy_current` → `policy_benchmark`；完整历史、双全收益指数及估值报价或失败留存 | `~/.local/state/ashare-benchmark/state.json` 仅记录待查请求，无新增月度通知去重 | ashare 子进程 40 秒、HTTP 30 秒、采集 25 秒；公开双基准完整响应已验证，真实账户完整采集、预算与正式送达仍待验收 |
| 顾问扫描 | `research_scan_request` → `research_scan_read`；通知前用 `account_current` 刷新持仓；`policy_current` + `policy_monitor` 取已留存评估 | 独立 `scan.json` 保存待查扫描；原 `opportunities.json` 保留机会键及待查政策请求 | 整轮 540 秒，共享 MCP 调用、轮询与机会检查预算；超时保留待查请求，恢复须匹配原服务、客户端和凭据 |
| 顾问周报 | `research_scan_weekly(compact=false)`，另汇总 `policy_current` + `policy_monitor` 的 `opportunity_cost` 换仓机会成本复核提示（不更改 `policy_daily_brief`） | 独立 `weekly-opportunities.json` 保存待查政策请求、独立运行锁，不混用每日 `opportunities.json` | 同一 advisor 进程监督与 540 秒总预算；换仓复核不可用时仍输出已取得的周报主体及失败说明 |
| 月批发起 | `research_batch_monthly_request(month)` | `<月>.receipt`，flock 与落盘回执 | 保持原入口；48 小时只用于卡住提示，不是执行超时 |
| 月批状态 | `research_batch_monthly_status(month, compact=false)` | 共用 `<月>.receipt` 事件去重 | 同上 |
| 记分 | 新 scoring 执行器，原四组记分算法与通知格式 | 原 `~/.local/state/a-share-scoring/state.json`，增加同目录任务锁 | 四组共用 540 秒截止、最多两路；容器内回收子进程，宿主命令最多再留 10 秒清理 |
| 里程碑 | 原 milestones 脚本 | 无独立通知去重 | 保持原入口 |

宿主配置的 `cron.script_timeout_seconds=600`，调度器可被 `HERMES_CRON_SCRIPT_TIMEOUT` 覆盖。2026-11-11 只读核对运行中 gateway 未设置该覆盖变量，当前源码正常路径据配置解析为 600 秒；未触发真实任务验证到时终止。新版记分和顾问源码均以 540 秒给任务清理留出余量。启用时须确认有效宿主时限、完成耗时与送达；上述 11-11 只读核对当时未修改宿主设置或已安装脚本，后续安装与运行状态见实施计划。

扫描工具复用 v21 的持久请求记录。服务端提交与查询语义见[顾问闭环](../../README.md#顾问闭环批次-a)。

## 顾问请求恢复与执行预算

`A_SHARE_ADVISOR_STATE`（默认 `~/.local/state/a-share-advisor`）下的 `scan.json` 为 0600。版本 2 保存 `pending` 的 `request_id`、`binding`、`created_at` 与 `scan_id`。绑定值是 MCP 地址与令牌的联合摘要。扫描锁由监督进程持有至清理完成，周报使用独立的 `weekly.lock`。

在非扫描时段切换。版本 1 如有 pending，保留原文件，用旧脚本、原研究凭据完成该请求；确认 pending 为空后再换发。政策待查回执也须先用原 assistant 身份查回。发送 MCP 请求前才写入版本 2；流程完成后恢复为两版客户端均可读取的版本 1 空记录。切回旧客户端也应先完成 MCP 待查请求。旧 `runtime.env` 中的 research key 在切换验证后由所有者按退役流程撤销。

扫描前将 UUID 和身份绑定原子落盘，同一身份调用 `research_scan_request` 提交或找回原扫描，取得编号后以 `research_scan_read(compact=false)` 查询。跨日查回的终态回执注明原观察时间。有通知时才读当前持仓；读取失败时提示具体类别及“持仓状态未知”。机会检查独立报告失败，保留已取得的扫描通知。

披露扫描的持仓范围由服务端核实，政策监控独立读取持仓卡。切换时按[双入口方案](../../docs/web-mcp-platform.md#43-打通研究账户与政策数据)核对并一次性清理 Hermes 以前写入的 external 持仓镜像行。订阅表仅有 origin，没有写入者字段；须先核对要保留的显式关注，再替换列表。这项清理由所有者切换流程执行。

`advisor_budget.py` 用 Linux 子进程和单调时钟限制整轮执行，涵盖 ashare 子进程、轮询睡眠和机会检查。超时或 TERM/INT 后终止进程组，约 4 秒内升级 KILL 并回收后代；正常结束也清理后代，子进程 stdout/stderr 不直接投递，成功返回的文本才交 Hermes。监督进程本身被 SIGKILL 时无法收尾，工具子进程不得另开会话脱离组；正式宿主时限须允许 540 秒预算后的清理。状态写入与 stdout 之间仍无送达 ACK，不能保证恰好一次通知。

## 记分执行预算

四组任务仍为 V0、V1、H1/H3、A3，共用一次开始时确定的 540 秒截止时间，最多同时运行两组；排队任务不重新获得预算。同目录 `.lock` 避免并行任务重复使用缓存或推进通知状态，已有任务运行时静默退出。

每组在容器内启动标准库监督进程，检查绝对截止时间后用单调时钟等待评分子进程；到期或收到终止信号时先发 TERM，随后 KILL，并回收其进程组和被收养的后代。即使宿主 Docker 客户端被杀，容器内监督仍有自己的截止时间。宿主命令仅增加 10 秒清理余量，给 Hermes 的 600 秒上限留出空间。每组继续复用原 `DATA_DIR` 缓存；JSON 输出使用独立临时文件，同一次执行成功后才读取，退出时清理，不再用另一次 `cat` 读取可能过期的结果。

四组全部解析并通过原签名/摘要校验后才原子更新 V0、V1、H1、H3、A3 五个通知键；任一失败或超时保留旧状态，给出失败提示。通知格式和成熟样本去重口径不变，仍不提供送达 ACK。隔离 worker 已验证超时回收、宿主断开清理，以及真实研究全量输入的冷缓存两路执行；耗时与覆盖限制见[实施计划](../../docs/implementation-plan.md)。这不替代正式调度与送达验收，当前结果见实施计划。

## 顾问替代入口

顾问脚本需要已安装的标准库 `ashare` 和私有 `~/.config/ashare/hermes-assistant.json`；可用 `ASHARE_HERMES_CONFIG` 指向其他绝对路径文件，格式见[客户端准备](../clients/README.md)。持仓来源无法核实时，通知会标明缺口。`account_current` 与 `policy_monitor` 要求服务显式账户开关；开关沿用 `account_shadow_enabled` 命名，实际数据来源以服务的 `account_source` 为准。

机会去重键仍为 `code|report_id|stance_action|v2_zone`，只保留当次仍存在的键；机会失效后重现可再次提醒。旧 JSON 数组首次使用时升级为 `{schema_version:1,seen:[…],pending:null}`。调用监控前原子保存 `pending` 中的 request_id / expected_activation_id；超时、损坏响应或不匹配回执保留请求，下次复用，不另造编号。锁防止并行改写；损坏状态不覆盖。前日已成功回执只用于清理待查请求，保留 seen，不作为今日机会发送，下次再采集。持久化通知键仍在 stdout 交给 Hermes 之前，未增加送达确认机制，不能宣称通知恰好一次。

若待查请求被服务不确定拒绝，应复用原请求并核对政策/权限，不删状态反复制造评估。唯一可自动转接的确定拒绝见下节；除此之外的人工处理须核对 created_by/request_id 的原回执，不由 deploy 删除客户端状态。状态升级后旧 advisor 不理解此对象，不能直接覆盖回旧脚本；应用回退须保持此客户端兼容性。当前安装与状态交接结果见实施计划。

## 跨激活 pending 转接

止损、日报、月度对比和顾问机会检查在调用前绑定 MCP 身份，同一身份、请求编号和参数才能恢复评估。CLI 退出码 **20** 表示政策已变化、确定未写入；止损、日报和顾问机会检查可据此转接。**21** 表示发布暂停或服务繁忙，确定未受理，可原样重试。止损首次记录暂停时间，持续超过 10 分钟才告警。**22** 表示超时或结果未知，只能用原身份和原请求查回。其他非零退出码为确定失败，通知提示核对凭据、权限与参数。原始错误体不进入通知。

止损、日报与顾问收到退出码 20 后，先将原请求和原因封存到状态文件同目录 `<文件名>.rejected-<原 request_id 的 SHA256>.json`（保留供核对），保持止损 state/previous_state 与机会 seen；再发现 installed=true 的新 activation，原子保存新 UUID 与新指针后才重采集。每轮最多转接一次，第二次变化保留新 pending 退出。发现失败、不适用的新指针、写状态失败也不擅自清理原请求。503 paused/busy、超时、坏响应、普通 409 一律保留原请求，旧 CLI/服务不支持专用码时同样安全失败。

过期行为不变：止损超过 15 分钟的成功告警不补发且不推进档位；日报/机会跨日回执不冒充今日，机会 seen 保持。没有通用补跑、通知 ACK 或恰好一次送达保证，不保证自动补发发布窗口错过的定时任务。不能把暂停或政策失效写成“无信号/无事件”。

安装顺序为服务端 → 同版本 MCP CLI → 确认 pending 已结清、切换配置 → 同步本目录脚本 → `install.py --check`。应用发布与 Hermes 宿主安装分开进行，当前安装与正式送达情况见[实施计划](../../docs/implementation-plan.md#合一重构收尾)。

## 止损提醒替代入口

保留任务 ID 和原 shell 入口；新版 wrapper 执行同目录 `a-share-stop-alert.py`，与顾问共用 `ashare_tools.py` 的标准库客户端调用与原子状态写入。使用同一 Hermes assistant 配置，不加载旧 Skills 或旧 runtime.env。`ASHARE_STOP_ALERT_STATE` 可指定新状态文件；默认路径见上表。

止损去重状态已在 2026-11-12 账户切换时从旧提醒状态逐字节交接；客户端接受旧 `{date,levels}` 或空对象，首次调用时包成 `{schema_version:1,state,pending}`。

顾问 `opportunities.json`、月批 `<月>.receipt` 和记分 `state.json` 继续使用原路径，保留原通知键及回执，不迁移或清空。上线前保存状态副本，按前一级合并版客户端验证升级后的格式；不能回退到不识别新对象的旧 Skills 客户端。实际任务停用、安装、启用及送达结果由实施计划维护。

服务沿用 v0 的时段判断、同日且绝对时间差不超过 15 分钟的行情、3% 临近线及同日升档去重；午休/非时段、无持仓或无新档位静默，全部行情无效时返回失败，不标成已提醒。服务不发送通知、不写客户端状态；输入中的 previous_state 是调用者提供的通知历史，不是账户事实。输出正文、档位与旧实现一致，客户端只将复核入口文案改为统一 a-share。

客户端先持久化请求编号、启用编号和旧状态，再发起评估；不明结果重试原请求，锁防并发。有效回执到达后原子推进状态再交 stdout 给 Hermes；没有送达确认，仍不能保证恰好一次通知。超过 15 分钟才取回的告警不作为实时提醒，也不推进档位，只清理已完成的待查请求、提示下次重新采集；全部行情无效的已保存回执也会结束本次请求，让下一轮重新采集。坏状态/坏回执保留待查记录并失败；解除待查遵守上面的服务回执核对规则。升级后的本地状态和服务上下文版本 3 都须由回退版本识别，不能回旧客户端或旧 Skills。

## 持仓日报替代入口

原 shell 入口执行同目录 `a-share-daily-brief.py`，使用相同的 Hermes assistant 私有配置。`ASHARE_DAILY_BRIEF_STATE` 可指定待查请求文件；记录原子写入并加锁，响应不明时只重试原 request_id / expected_activation_id。确认回执后清理待查请求，再交给 Hermes 发送；没有新增每日去重，也没有送达 ACK。其他日期的回执只用于结束待查，不作为今日日报发送；坏状态或回执不清理。

日报读取新 `policy_daily_brief` 完整结果，保留漏记分红及分红获取失败提示。Provider 成功空表表示未发现实施记录，失败的 null 表表示尚未核对，两者不会混用。服务正文沿用 v0，客户端仅把“运行 dividend-check”提示改为通过统一 a-share 核对券商流水，不自动入账。无当日报价时原格式静默，分红失败仍在评估回执的 dividends.failures 中可查。

成功分红采集需要 tools 专用 `.env.tools` 中已有的 TUSHARE_TOKEN；日报子进程仅额外传递这一行情令牌，不读取旧 runtime.env，也不传模型或 owner 凭据。服务未配置令牌时保留 AUTH_MISSING 失败项。离线历史行情、无令牌失败支路和既有分红历史样本已验证；生产日报脚本已成功运行，定时送达继续观察。上下文版本 4 和待查文件格式须由回退合并版本支持。安装及真实运行结果见实施计划。

## 账本月度对比替代入口

原 shell 入口执行同目录 `a-share-benchmark.py`，使用相同的 Hermes assistant 配置。`ASHARE_BENCHMARK_STATE` 可指定待查文件。调用前原子保存 request_id、expected_activation_id 与上海当前日期 end，锁防并发，响应不明时复用原请求和截止日。有效回执结束待查，再输出报告或明确失败；其他日期的已完成请求只清理待查，不当作当日报告发送。坏状态/坏回执不覆盖，不增加月度通知去重或送达 ACK。

完整交易历史包含期初前事件与已关闭持仓，服务按原现金流算法比较沪深 300 全收益和中证红利全收益。成功才加原“📊 A股账本月度对比”前缀，报价降级警告保留；无交易、指数/股票价格缺失时退出 1 并提示失败，不显示伪造收益。tools 专用行情令牌传递规则同日报；无令牌的真实 Provider 失败会保存。上下文版本 5 与待查格式需要兼容合并版回退。局部指数缓存和结构算例不计作真实完整双基准验收；实际覆盖见实施计划。
