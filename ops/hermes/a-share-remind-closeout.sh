#!/usr/bin/env bash
# Temporary owner reminder; delivers fixed instructions without running an agent.
set -eu
cat <<'ASHARE_REMINDER'
A股合一重构提醒：切换后首个交易日观察与重构收尾
计划时间：2026-11-13T17:00:00+08:00

请把下面这段原样交给开发 Agent：

请在 /home/lin/a-share-research 接续合一重构。先读 AGENTS.md、docs/implementation-plan.md 和 docs/consolidation.md，核对当前提交、生产与证据，以最新记录为准；已完成的步骤不要重复执行。按现有授权完成工作并更新实施计划、提交推送，最后报告实际结果与剩余项。
请核查账户切换是否完成；若未完成，先报告缺口和顺延安排，不归档仍在使用的旧系统。若已完成，核对切换后交易日的止损、日报、顾问等任务实际执行、耗时、送达和去重，确认回退保持新账本唯一事实来源。按合一方案完成 Lib/Skills 两仓归档、旧入口退役与文档收敛，保留历史数据和备份；删除非本任务创建的数据仍须单独授权。清理已完成的第 3 段临时对照任务及这组提醒，未到自然执行日的月度任务送达记为非阻塞观察。对照退出条件报告实际完成与未完成项，不以日期到期代替验收。

这是提醒，不表示上述操作已执行。若阶段已完成，请让 Agent 核对记录后跳过。
ASHARE_REMINDER
