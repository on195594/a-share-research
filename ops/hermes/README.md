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

先部署服务，再同步同版本 CLI。更换配置前用原身份查回扫描与政策待查请求。提交本目录的脚本改动后，从仓库根目录安装：

```sh
python3 ops/hermes/install.py --apply
python3 ops/hermes/install.py --check
```

`--apply` 在扫描与政策任务空闲且没有待查请求时复制脚本，`--check` 比较内容与可执行位。两者都可用 `--target <目录>` 指定安装位置。应用镜像发布与宿主脚本安装分别执行。

## 跨激活 pending 转接

政策评估前，脚本在本地加锁并保存服务、凭据的绑定摘要、请求编号和预期激活编号。响应损坏、超时或 CLI 退出 22 时，保留待查请求，用原身份和原参数找回回执。凭据或绑定不符时，先恢复原配置并核对回执，再换发凭据。

- **退出 20** 表示政策变化且原请求确定未写入。止损、日报和顾问会把原请求封存到状态文件旁的 `<文件名>.rejected-<原请求编号的 SHA256>.json`，读取新激活编号，持久化新请求后再采集。每轮最多转接一次。月度对比保留原请求，需人工核对。
- **退出 21** 表示暂未受理，保留待查请求并在恢复后原样重试。止损脚本对连续不可用计时，满 10 分钟才提示失败。一般冲突、坏状态或坏回执须核对原身份与请求编号，不能清空状态重跑。
- **过期回执**　超过 15 分钟的止损告警不投递、不推进档位。日报、顾问机会和月度对比查回其他日期的回执时，保留原日期，只结束这笔待查请求。本地去重记录在交付输出前落盘，核实实际送达须查看 Hermes 投递记录。

| 任务 | 调用与排错 | 默认状态文件（相对 `~/.local/state/`） |
|---|---|---|
| 止损 | `policy_stop_alert` 读取有效时段的行情，全部无效时提示检查失败。按同日升档去重。 | `ashare-stop-alert/state.json`，可用 `ASHARE_STOP_ALERT_STATE` 指定 |
| 日报 | `policy_daily_brief` 提供持仓与分红核对。分红失败查回执中的 `dividends.failures`；行情令牌由 tools 的 `.env.tools` 提供。 | `ashare-daily-brief/state.json`，可用 `ASHARE_DAILY_BRIEF_STATE` 指定 |
| 月度对比 | `policy_benchmark` 按保存的截止日比较两个全收益基准。无交易、缺指数或股票报价时，查看失败回执。 | `ashare-benchmark/state.json`，可用 `ASHARE_BENCHMARK_STATE` 指定 |

## 顾问请求恢复与执行预算

顾问状态目录为 `~/.local/state/a-share-advisor/`，可由 `A_SHARE_ADVISOR_STATE` 指定。扫描在 `scan.json` 保存绑定和请求编号，用 `research_scan_request` 提交或找回扫描，再用 `research_scan_read` 查终态。跨日找回时显示原扫描时间。通知需要持仓信息时才读 `account_current`，读取失败则注明持仓状态未知。

扫描后的机会检查通过 `policy_monitor` 取评估，`opportunities.json` 按代码、报告、立场和估值区间去重。周报读取 `research_scan_weekly`，用独立的 `weekly-opportunities.json` 保存换仓复核请求。机会检查失败时仍输出取得的扫描或周报正文。

扫描与周报各有任务锁。`advisor_budget.py` 为每轮调用、轮询及机会检查共留 540 秒，到期终止子进程组。宿主配置 `cron.script_timeout_seconds=600`，排查提前超时时须核对 `HERMES_CRON_SCRIPT_TIMEOUT` 是否覆盖该值。

## 月批发起与状态

月批脚本通过 `research_batch_monthly_request` 按上海自然月受理，已有批次直接返回其编号。提交结果未知时先调用 `research_batch_monthly_status` 查当月批次。状态任务对冻结、带缺口完成、待所有者确认、失败及创建超过 48 小时仍未完成分别提示，同一批次的同一事件只记一次。`~/.local/state/a-share-monthly/<月>.receipt` 保存状态通知键，可用 `A_SHARE_MONTHLY_STATE` 指定目录。停用月批自动化时用 `hermes cron pause` 分别暂停发起与状态任务。

## 记分执行预算

V0、V1、H1/H3、A3 四组在 worker 容器内共用 540 秒截止时间，最多两组并行。容器内监督进程负责超时回收，宿主命令另留 10 秒清理。同目录任务锁保护缓存与 `~/.local/state/a-share-scoring/state.json`。只有四组均成功且成熟样本计数变化时才保存新的通知状态并输出摘要；失败或超时保留原状态，按输出的记分组排查。
