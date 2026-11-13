#!/usr/bin/env bash
# Temporary owner reminder; delivers fixed instructions without running an agent.
set -eu
cat <<'ASHARE_REMINDER'
A股合一重构提醒：12 月双组月批核验
计划时间：2026-12-04T10:00:00+08:00

请把下面这段原样交给开发 Agent：

请在 /home/lin/a-share-research 接续合一重构。先读 AGENTS.md、docs/implementation-plan.md 和 docs/consolidation.md，核对当前提交、生产与证据，以最新记录为准；已完成的步骤不要重复执行。按现有授权完成工作并更新实施计划、提交推送，最后报告实际结果与剩余项。
请完成实施计划中的“12 月首跑最小验收”：核对定时提交与唯一批次 ID、screen-v1.3 快照、Q20/S10 分池与报告关联、调用预算与缺失 usage、共同冻结时间及缺口、下一交易日入场/现金席位/基准，以及通知实际送达与去重。核对判断契约和冻结样本未变；不得为了补验收另建重复批次。保护期持续至 12-04，不发布镜像或重启 worker。未完成或失败按现有恢复契约处理，禁止盲目重跑；给出 12 月双组月批首跑验收结论。

这是提醒，不表示上述操作已执行。若阶段已完成，请让 Agent 核对记录后跳过。
ASHARE_REMINDER
