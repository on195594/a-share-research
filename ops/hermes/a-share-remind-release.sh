#!/usr/bin/env bash
# Temporary owner reminder; delivers fixed instructions without running an agent.
set -eu
cat <<'ASHARE_REMINDER'
A股合一重构提醒：第 3 段验收与月批前最后一次发布
计划时间：2026-11-27T17:30:00+08:00

请把下面这段原样交给开发 Agent：

请在 /home/lin/a-share-research 接续合一重构。先读 AGENTS.md、docs/implementation-plan.md 和 docs/consolidation.md，核对当前提交、生产与证据，以最新记录为准；已完成的步骤不要重复执行。按现有授权完成工作并更新实施计划、提交推送，最后报告实际结果与剩余项。
请核对 11-23～11-27 五个交易日影子对照及边界样本，检查每次真实采集、差分结果和调度回执；缺失或有差异须定位，不用预演替代。复用 stage3/screenshot-20261112-close 的带时点截图验收。满足条件后，在 11-28 前经 README 固定发布入口完成最后一次发布，启用生产账户影子开关，外部研究排队保持关闭。按最终运行镜像刷新账户镜像，核对实际政策参数，登记并启用政策 v0，完成一次 policy.monitor 生产服务端采集并保存输入、身份与回执。不得整包安装 Hermes 替代脚本或切换真实账户写入。11-29 起进入发布冻结；若收到本提醒时已进入冻结期，不补做发布，记录顺延。

这是提醒，不表示上述操作已执行。若阶段已完成，请让 Agent 核对记录后跳过。
ASHARE_REMINDER
