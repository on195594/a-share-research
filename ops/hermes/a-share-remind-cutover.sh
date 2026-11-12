#!/usr/bin/env bash
# Temporary owner reminder; delivers fixed instructions without running an agent.
set -eu
cat <<'ASHARE_REMINDER'
A股合一重构提醒：账户与客户端切换
计划时间：2026-12-05T09:00:00+08:00

请把下面这段原样交给开发 Agent：

请在 /home/lin/a-share-research 接续合一重构。先读 AGENTS.md、docs/implementation-plan.md 和 docs/consolidation.md，核对当前提交、生产与证据，以最新记录为准；已完成的步骤不要重复执行。按现有授权完成工作并更新实施计划、提交推送，最后报告实际结果与剩余项。
请先核验第 3 段退出条件与 12-01 月批验收，未通过就顺延，不凭日期切换。通过后按方案第 5 节执行账户切换：停旧账户调用任务与宿主入口并等在途结束，取一致快照与水位，末次镜像核对守恒，封锁旧写入，写切换标志，再启用新账本与替代任务。期初用切换时持仓和当时截图现金，不用 11-12 影子期初代替。完成真实入账、重复提交、更正与兼容合并版回退验证；保留通知状态与回执。五宿主真实账户场景开始前，按实施计划向我确认一次账户数据会进入对应模型；AGY 如需重新登录请指出。只执行验收所需账户记账，不进行真实证券交易。

这是提醒，不表示上述操作已执行。若阶段已完成，请让 Agent 核对记录后跳过。
ASHARE_REMINDER
