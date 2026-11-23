# Hermes 定时脚本

Hermes 以 `no_agent` 模式运行下列脚本，时间均为上海时间。入口文件安装在 `~/.hermes/scripts/cron/`，输出由 Hermes 投递。

| 任务 | Hermes 任务 ID | 脚本入口 | 调度 |
|---|---|---|---|
| 止损提醒 | `ca568f56d9ef` | `a-stock-stop-alert.sh` | 周一至周五 09–15 点每 5 分钟，服务判断交易时段 |
| 持仓日报 | `7ab0ded3121c` | `a-stock-daily-brief.sh` | 周一至周五 15:35 |
| 账本月度对比 | `93c4f5a4d0fc` | `a-stock-benchmark-monthly.sh` | 每月 1 日 09:00 |
| 披露扫描 | `fbb0cbf6ee60` | `a-share-advisor.sh` | 周一至周五 16:37 |
| 顾问周报 | `7845e96f4184` | `a-share-advisor-weekly.sh` | 周日 18:07 |
| 月批发起 | `3d8b860808e4` | `a-share-monthly-submit.sh` | 每月 1 日 20:00 |
| 月批状态 | `112633a18054` | `a-share-monthly-status.sh` | 每月 1–3 日每小时 :30 |
| 判断记分 | `2c0e2f798501` | `a-share-scoring.sh` | 周日 19:07 |
| 里程碑进度 | `6b6ecd1783ea` | `a-share-milestones.sh` | 周一 09:03 |
| 12 月双组月批核验提醒 | `1f1ead8a8a62` | `a-share-remind-monthly.sh` | 2026-12-04 10:00 一次，允许离线后补发 |

核验提醒只输出固定文字，操作以[12 月首跑清单](../../docs/implementation-plan.md#待真实事件的验收)为准，验收完成后清理提醒。里程碑脚本读取仓库 HEAD 的[固定里程碑](../../docs/implementation-plan.md#固定里程碑)、生产 web 镜像标签和只读业务库，核对交付提交并汇总近期到期项。

## 安装与凭据

账户、顾问和月批脚本通过 `ashare_tools.py` 调用 `~/.local/bin/ashare`，使用 `~/.config/ashare/hermes-assistant.json`（0600）。`ASHARE_HERMES_CONFIG` 可指定另一绝对路径。配置使用公网 MCP 地址，本机 cron 通过 `connect` 直连 tools 回环端口。记分任务直接运行 worker 容器内的命令。[CLI 配置与退出码](../clients/README.md#cli-与本机-cron)

先部署服务，再同步同版本 CLI。首次切换前用原凭据和旧脚本查完历史待查请求。提交本目录的脚本改动后，从仓库根目录安装。

```sh
python3 ops/hermes/install.py --apply
python3 ops/hermes/install.py --check
```

`--apply` 在扫描与政策任务空闲且没有待查请求时复制脚本，`--check` 比较内容与可执行位。两者都可用 `--target <目录>` 指定安装位置。应用镜像发布与宿主脚本安装分别执行。

## 止损、日报、月度对比与顾问任务

脚本按上海时间确定时段，用原 assistant 凭据调用 `schedule_run(job, slot)`。服务端按凭据、任务和时段保存运行行。评估记录可按 `evaluation_id` 查询当时的政策与采集证据。重新执行同一时段时，已就绪的行返回原结果，已确认的行静默结束。

| 任务 | 时段 | 查回与输出 |
|---|---|---|
| 止损 `stop_alert` | 当前时间向下取整至 5 分钟 | 服务从同一上海日期最近确认的档位评估。超过 15 分钟的时段静默过期。 |
| 日报 `daily_brief` | 当天日期 | 服务生成持仓与分红核查正文，分红失败可在评估回执的 `dividends.failures` 查到。 |
| 月度对比 `benchmark` | 当月 | 服务沿用首次受理时的上海日期作为截止日，比较两个全收益基准。 |
| 扫描 `advisor_scan` | 当天日期 | 按服务端返回的 `scan_id` 跟踪扫描，在本次运行内轮询同一时段。终态后展示披露及新增建仓机会。 |
| 周报 `advisor_weekly` | ISO 周 | 展示研究状态、披露缺口及换仓复核。扫描与周报各自按最近确认的机会记录去重。 |

回执为 `ready` 时，脚本先将正文写入标准输出，再用 `schedule_ack(run_id)` 确认。顾问在客户端渲染回执中的结构化结果。确认失败时正文已经输出，脚本向标准错误说明原因并以 1 退出。未获确认的止损档位和机会可在后续时段再次提醒，实际投递情况查 Hermes 执行记录。

退出码 21 表示服务暂不可用。脚本每 20 秒用相同时段或运行编号重试，最多 240 秒，耗尽后以 1 退出。采集或评估失败时，运行行保存 `error_code` 和脱敏的 `error`，在同一时段再次调用会重试。日报和扫描在跨日时尚未就绪，会返回 `expired` 和一行说明。

扫描与周报各有任务锁。`advisor_budget.py` 为本轮请求及轮询共留 540 秒，到期终止子进程组。宿主配置 `cron.script_timeout_seconds=600`，排查提前超时时须核对 `HERMES_CRON_SCRIPT_TIMEOUT` 是否覆盖该值。

## 跨激活 pending 转接

回退到旧版政策脚本前，从切换时的备份恢复对应的本地状态，再用原身份、请求编号和预期激活编号查回待查评估。旧版止损、日报和顾问脚本遇到退出 20 时会封存被拒请求，并用新激活编号重新受理一次。旧版月度对比须人工核对。月批按下节的批次回执恢复。

## 月批发起与状态

月批脚本通过 `research_batch_monthly_request` 按上海自然月受理，已有批次直接返回其编号。提交结果未知时先调用 `research_batch_monthly_status` 查当月批次。状态任务对冻结、带缺口完成、待所有者确认、失败及创建超过 48 小时仍未完成分别提示，同一批次的同一事件只记一次。`~/.local/state/a-share-monthly/<月>.receipt` 保存状态通知键，可用 `A_SHARE_MONTHLY_STATE` 指定目录。停用月批自动化时用 `hermes cron pause` 分别暂停发起与状态任务。

## 记分执行预算

V0、V1、H1/H3、A3 四组在 worker 容器内共用 540 秒截止时间，最多两组并行。容器内监督进程负责超时回收，宿主命令另留 10 秒清理。同目录任务锁保护缓存与 `~/.local/state/a-share-scoring/state.json`。只有四组均成功且成熟样本计数变化时才保存新的通知状态并输出摘要；失败或超时保留原状态，按输出的记分组排查。
