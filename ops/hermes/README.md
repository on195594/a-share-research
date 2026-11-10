# Hermes 定时脚本
源文件只在本目录维护；宿主安装目录为 `~/.hermes/scripts/cron/`，使用复制，不使用符号链接。
任务与计划取自[上线后接入](../../docs/implementation-plan.md#当前部署)（上海时间；该节只记录任务 ID，未记录具名任务名称）。
| 脚本（共用入口） | Hermes 任务 | 计划 |
|---|---|---|
| `a-share-advisor.sh`、`a-share-advisor-weekly.sh`（`a-share-advisor.py`） | 顾问扫描 `fbb0cbf6ee60` / 周报 `7845e96f4184` | 工作日 16:37 / 周日 18:07 |
| `a-share-monthly-submit.sh`、`a-share-monthly-status.sh`（`a-share-monthly.sh`、`a-share-monthly.py`） | 月批发起 `3d8b860808e4` / 状态 `112633a18054` | 每月 1 日 20:00 / 每月 1–3 日每小时 :30 |
| `a-share-scoring.sh`、`a-share-scoring.py` | 记分 `2c0e2f798501` | 周日 19:07 |
| `a-share-milestones.sh`、`a-share-milestones.py` | 里程碑进度 `6b6ecd1783ea` | 周一 09:03 |
凭据：`~/.config/a-share-advisor/runtime.env`。
去重回执：`~/.local/state/a-share-advisor/opportunities.json`、`~/.local/state/a-share-monthly/<月>.receipt`、`~/.local/state/a-share-scoring/state.json`。
核对：`python3 ops/hermes/install.py --check`；安装：`python3 ops/hermes/install.py --apply`（须先提交本目录改动，自动复核）。
可加 `--target <目录>`；安装移除旧 `a-share-k7-progress.py/.sh`，其他非清单文件不动；Hermes 任务入口需同步为新名字。
