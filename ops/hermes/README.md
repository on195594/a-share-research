# Hermes 定时脚本
源文件只在本目录维护；宿主安装目录为 `~/.hermes/scripts/cron/`，使用复制，不使用符号链接。当前合一替代脚本仅准备，尚未安装；启用须满足[合一方案](../../docs/consolidation.md)的影子、回退与切换条件。
任务与计划取自[上线后接入](../../docs/implementation-plan.md#当前部署)（上海时间；该节只记录任务 ID，未记录具名任务名称）。
| 脚本（共用入口） | Hermes 任务 | 计划 |
|---|---|---|
| `a-stock-stop-alert.sh` | 止损提醒 `ca568f56d9ef` | 工作日 09–15 点每 5 分钟，命令内判断交易时段 |
| `a-stock-daily-brief.sh` | 持仓日报 `7ab0ded3121c` | 工作日 15:35 |
| `a-stock-benchmark-monthly.sh` | 账本月度对比 `93c4f5a4d0fc` | 每月 1 日 09:00 |
| `a-share-advisor.sh`、`a-share-advisor-weekly.sh`（`a-share-advisor.py`） | 顾问扫描 `fbb0cbf6ee60` / 周报 `7845e96f4184` | 工作日 16:37 / 周日 18:07 |
| `a-share-monthly-submit.sh`、`a-share-monthly-status.sh`（`a-share-monthly.sh`、`a-share-monthly.py`） | 月批发起 `3d8b860808e4` / 状态 `112633a18054` | 每月 1 日 20:00 / 每月 1–3 日每小时 :30 |
| `a-share-scoring.sh`、`a-share-scoring.py` | 记分 `2c0e2f798501` | 周日 19:07 |
| `a-share-milestones.sh`、`a-share-milestones.py` | 里程碑进度 `6b6ecd1783ea` | 周一 09:03 |
凭据：`~/.config/a-share-advisor/runtime.env`。
去重回执：`~/.local/state/a-share-advisor/opportunities.json`、`~/.local/state/a-share-monthly/<月>.receipt`、`~/.local/state/a-share-scoring/state.json`。
核对：`python3 ops/hermes/install.py --check`；安装：`python3 ops/hermes/install.py --apply`（须先提交本目录改动，自动复核）。
可加 `--target <目录>`；安装移除旧 `a-share-k7-progress.py/.sh`，其他非清单文件不动；Hermes 任务入口需同步为新名字。

## 合一替代调用与切换核对

下表按 2026-11-11 本机任务配置与脚本核对；不改变任务时间、`no_agent` 模式或通知目标。三个 `a-stock-*` 包装脚本从宿主原样纳入版本管理，内部仍为旧调用，不能当作服务化已完成。

| 任务 | 调用与替代状态 | 去重状态 | 命令预算与回退边界 |
|---|---|---|---|
| 止损提醒 | `a-stock-cache stop-alert`；服务替代待实现，须保留同日按代码升档提醒 | `~/.local/share/a-stock-agent/stop-alert-state.json`（可由旧 runtime.env 的 A_STOCK_STATE_DIR 改路径） | wrapper 无独立超时；准备期保留已安装脚本/状态，切换后只回兼容新账本的合并版 |
| 持仓日报 | `a-stock-cache daily-brief`；替代须同时覆盖监控与漏记分红检查及其失败提示 | 无独立通知去重 | wrapper 无独立超时；不可只改成 policy.monitor 而遗漏分红检查 |
| 账本月度对比 | `a-stock-cache benchmark-report`；全收益指数与完整事件历史的服务替代待验 | 无独立通知去重 | wrapper 无独立超时；期初后现金流不能冒充完整基准历史 |
| 顾问扫描 | 新脚本用 `ashare … call account.current` 同步及通知前刷新持仓，`policy.current` + `policy.monitor` 取已留存评估；研究扫描 API 不变 | `opportunities.json`，保留旧机会键并增加待查请求 | ashare 子进程 40 秒、HTTP 30 秒、服务采集 25 秒；扫描 HTTP 每次 60 秒，轮询最多 90×20 秒 |
| 顾问周报 | 原研究 weekly API，无账户命令 | 无独立通知去重 | HTTP 60 秒；脚本仍共用 advisor |
| 月批发起 | 原 monthly submit API | `<月>.receipt`，flock 与落盘回执 | 保持原入口；48 小时只用于卡住提示，不是执行超时 |
| 月批状态 | 原 monthly status API | 共用 `<月>.receipt` 事件去重 | 同上 |
| 记分 | 原 scoring 脚本 | `~/.local/state/a-share-scoring/state.json` 原子替换 | Docker 子进程各 1,200 秒 |
| 里程碑 | 原 milestones 脚本 | 无独立通知去重 | 保持原入口 |

宿主配置的 `cron.script_timeout_seconds=600`，调度器可被 `HERMES_CRON_SCRIPT_TIMEOUT` 覆盖，不能仅凭配置断言进程实际预算。扫描轮询、记分子进程与此配置已有不一致；正式任务验收须核对实际进程环境和完成耗时，未在本批修改宿主设置。

顾问脚本需要已安装的标准库 `ashare` 和私有 `~/.config/ashare/hermes-assistant.json`；可用 `ASHARE_HERMES_CONFIG` 指向其他绝对路径文件，格式见[客户端准备](../clients/README.md)。不读取旧账户库、不在服务失败时回落旧命令；没有已核对来源就报告失败，不用空持仓覆盖订阅。`account.current` 与 `policy.monitor` 目前仍要求服务显式账户影子开关，准备期只在私有副本验收。

机会去重键仍为 `code|report_id|stance_action|v2_zone`，只保留当次仍存在的键；机会失效后重现可再次提醒。旧 JSON 数组首次使用时升级为 `{schema_version:1,seen:[…],pending:null}`。调用监控前原子保存 `pending` 中的 request_id / expected_activation_id；超时、损坏响应或不匹配回执保留请求，下次复用，不另造编号。锁防止并行改写；损坏状态不覆盖。前日已成功回执只用于清理待查请求，保留 seen，不作为今日机会发送，下次再采集。持久化通知键仍在 stdout 交给 Hermes 之前，未增加送达确认机制，不能宣称通知恰好一次。

若待查请求被服务拒绝，应先以同一凭据重试原请求并核对政策/权限；不要删状态反复制造评估。无法经重试查回时，由维护者按 created_by/request_id 检查 policy_evaluations，确认不存在成功回执后再解除该 pending。状态升级后旧 advisor 不理解此对象，不能直接覆盖回旧脚本；正式切换前须把此客户端兼容性纳入合并版回退演练。当前宿主脚本与原状态尚未替换。
