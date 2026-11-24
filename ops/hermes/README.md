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

先部署服务，再同步同版本 CLI。提交本目录的脚本改动后，从仓库根目录安装。

```sh
python3 ops/hermes/install.py --apply
python3 ops/hermes/install.py --check
```

`--apply` 检查任务锁和旧版待查记录，空闲时复制脚本。`--check` 比较内容与可执行位。两者都可用 `--target <目录>` 指定安装位置。应用镜像发布与宿主脚本安装分别执行。

## 定时运行与确认

定时脚本按上海时间计算时段，以原 assistant 凭据调用 `schedule_run(job, slot)`。服务端按凭据、任务和时段保存运行行与最终正文。评估记录可按 `evaluation_id` 查询当时的政策与采集证据。

| 任务 | 时段 | 查回与输出 |
|---|---|---|
| 止损 `stop_alert` | 当前时间向下取整至 5 分钟 | 同一上海日期按标的继承已确认的最高档位。超过 15 分钟的时段静默过期。 |
| 日报 `daily_brief` | 当天日期 | 输出持仓与分红核查，分红失败项见评估回执的 `dividends.failures`。跨日尚未就绪时返回 `expired` 和一行说明。 |
| 月度对比 `benchmark` | 当月 | 截止日取首次受理日与请求月末的较早者，比较两个全收益基准。 |
| 扫描 `advisor_scan` | 当天日期 | 按 `scan_id` 跟踪扫描。跨日先补回待送披露，补回失败时保存部分披露和原因，再处理当天扫描及新增建仓机会。 |
| 周报 `advisor_weekly` | ISO 周 | 展示研究状态、披露缺口及换仓复核。重试复用本周摘要。扫描与周报分别按最近确认的机会去重。 |
| 月批发起 `monthly_submit` | 当月 `YYYY-MM` | 沿用按上海月份受理的批次。已有批次返回原编号，失败批次由所有者决定是否重开。 |
| 月批状态 `monthly_status` | 上海整点 `YYYY-MM-DDTHH` | 失败、待确认、冻结完成、带缺口完成，以及创建超过 48 小时仍未完成时提示。同月已确认的批次事件键由下一次运行继承。 |

脚本读取服务端返回的 `text`，将非空正文写入标准输出，随后用 `schedule_ack(run_id)` 确认 `ready` 行。重查已确认行时静默结束。未确认的止损档位、机会或月批事件可能重推。顾问正文中的研究链接取自 tools 的 `ASHARE_PUBLIC_MCP_URL`。停用月批时用 `hermes cron pause` 分别暂停发起与状态任务。

本轮输出业务正文后遇到错误，脚本在正文末尾追加失败说明，以 0 退出，让 Hermes 发送全文。尚未输出业务正文就失败时以 1 退出，由 Hermes 发送失败摘要。运行行保存 `error_code` 和脱敏的 `error`。`acked` 表示脚本已处理回执，最终投递查 Hermes 执行记录。

退出码 21 表示暂未受理，22、本地调用超时或 CLI 被信号中断表示结果未知。脚本每 20 秒按原时段或运行编号查回，每个请求的重试窗口最多 240 秒。顾问整轮限 540 秒，其他五类定时运行限 270 秒。单次 CLI 调用最多 40 秒，调用、重试和轮询共用本轮剩余时间。止损还按当前五分钟时段裁剪预算，留出释放锁的时间。宿主配置 `cron.script_timeout_seconds=600`，排查提前超时时核对 `HERMES_CRON_SCRIPT_TIMEOUT`。

## 定时任务状态段 3 回退

若需回退，先暂停受影响的 Hermes 任务，等本轮退出。在 v26 账本的只读连接上将 `:grant_id` 绑定为正在使用的 Hermes grant 编号，查回所有未确认运行，包括尚未写入 `scan_id` 的扫描。

```sql
SELECT id,job,slot,status,scan_id FROM scheduled_runs
WHERE principal_key_id=:grant_id AND status NOT IN ('acked','expired')
ORDER BY job,slot;
```

用段 3 客户端和原凭据查回、输出并确认待交付正文，再次查询核实交接。回退越过段 3 时，先按仓库部署流程恢复含旧工具的段 3 前镜像，确认服务可用，再在 `9e77a07` 的干净检出中运行上面的 `install.py --apply` 和 `--check`，最后恢复 Hermes 调度。若当前镜像已具备旧工具，可直接装回这版脚本。该版本沿用 v26 账本，月批恢复宿主收据，已由段 3 确认的月批事件可能再次提示。v26 之前已无可用的发布前备份，不再回退到 v25。[部署回退入口](../../README.md#回退) · [当前部署](../../docs/implementation-plan.md#当前部署)

## 记分执行预算

V0、V1、H1/H3、A3 四组在 worker 容器内共用 540 秒截止时间，最多两组并行。容器内监督进程负责超时回收，宿主命令另留 10 秒清理。同目录任务锁保护缓存与 `~/.local/state/a-share-scoring/state.json`。只有四组均成功且成熟样本计数变化时才保存新的通知状态并输出摘要；失败或超时保留原状态，按输出的记分组排查。
