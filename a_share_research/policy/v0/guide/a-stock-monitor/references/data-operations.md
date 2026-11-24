# 账户对账、入账与结构化状态操作

用于账户差异对账、交易与分红入账、资产观察及周期分析状态更新。

## 一、资产观察更新

收到清晰的同账户资产截图时：
1. 核对总资产与可见账户标识（金额模糊或账户不符时只问具体缺项）。
2. 用 `account_evidence` 留存原图。提交当前资产观察前，先比较截图所反映的时点与账户最新资产观察；已知旧截图仅作凭证，确认时序后才用 `account_observe` 记录当前金额。
3. 截图明确标出资产估值时点时填入 `portfolio_value_as_of`（带时区 ISO 8601）；手机状态栏时间或记录时间归入 `observed_at`，估值时点缺失则留空。

## 二、对账与交易入账

1. **对比差异**：结合本轮监控快照与用户截图/成交说明，经 `account_state`、`account_positions`、`account_events` 核对股数、成本、现金、费用税费与分红差异；成本口径见 [成本对账门](portfolio-risk.md#三成本对账门)。
2. **分红核查**：查看持仓日报（`schedule_run` 的 `daily_brief` 任务），用回执中的 `evaluation_id` 调用 `policy_evaluation` 查阅分红核查明细，或核查已实施分红，与用户核对到账凭证。现金分红按实收金额记 `dividend`，送转按股数变动记 `adjustment`，两类事件可同批提交。核查失败的代码标“未核对”。
3. **补齐要素**：每笔成交及其他事件均采集完整、带时区的发生时间 `occurred_at`。一次列齐缺少的成交价、数量、费用税费（卖出印花税按成交额 × 0.5‰ 计入）及分红到账事实。如成交实际依据某份 Research 报告，附决策时阅读的 `report_id` 并核对报告时间。
4. **确认并提交**：展示拟写入事件与期末券商快照，经 `account_events` 确认未重复且取得用户明确确认后调用 `account_submit`。更正历史记录采用作废原事件加提交替代事件。
5. **写后复核**：检查回执（`data.status == "posted"`）与最新股数/成本，需要评估最新风险时重跑 `policy_monitor`，交付已闭合项、剩余缺口与下次复核条件。

## 三、结构化分析状态（L3 / 论点 / 预警 / Tier）

- **读取**：`account_cycle_analysis_current`、`account_cycle_analysis_history`。
- **写入**：将 `account_cycle_analysis_current` 返回的 `data.version` 填入 `update.expected_version`，附证据哈希与变更原因。用户确认拟议变更后调用 `account_cycle_analysis_update`。
- **论点重写（`kind=thesis`）**：须含 `l1`、`l2`、`rewrite_reason`、覆盖全部活动条件的 `retire_l3_ids` 及非空 `new_l3`。每条新 L3 含 `condition`、`scope`（`aggregate`/`core_driver`/`non_core`/`governance`）、`action`（`review`/`reduce`/`exit`，`non_core` 仅用 `review`）、`materiality_basis`、`temporary_exit_rule`。
- **历史 `unverified/pending` 预警**：逐项复核证据后经用户确认重新分类或解除；证据不足则保持 `pending`。
- **P3 流动性休克延迟**：固定 `reason_code=price_stop2_liquidity_defer`，`evidence` 写入含版本、`defer_started_at`、原第二档价格与原市场快照的 JSON；用户确认后开启 `red / holding_deterioration` 预警，首次 `defer_started_at` 保持不变。
- 建仓期 Tier 路径、估值豁免声明与框架迁移目前无写入入口，需调整时向所有者说明。

## 四、除权成本调整

以事件前的参考成本为基准，`新参考成本 = (原 reference_cost - 每股现金分红) / (1 + 转增比例)`。

- **仅询问计算**：补齐缺失的每股分红或转增比例后，输出新参考成本与重算后的 Tier 1/2/3 目标价。
- **含写入意图**：在 `batch.position_distributions` 中按本批事件下标（字符串键）关联每股除权现金数；同次除权仅关联一次，含送转时关联到 `adjustment`。展示预计参考成本与 Tier 目标价，按上述对账流程确认并提交。

## 五、持久化状态速查

| 状态 | 写入方式 | 消费场景 |
|---|---|---|
| 持仓股数、成本、买入日与主力入场日 | `account_submit` | 监控快照、12 个月复查 |
| 资产观察快照 | `account_observe` | 风险分母 |
| L3 条件与核查结论 | `account_cycle_analysis_update`（`l3`） | 论文破灭止损、步骤 2.5 |
| L1/L2 论点版本与 L3 替换 | `account_cycle_analysis_update`（`thesis`） | 活动论文边界 |
| 预警开启、续期与解除 | `account_cycle_analysis_update`（`alert` / `alert_open`） | 预警升级门槛、P3 延迟 |
| Tier 1/2/3 状态 | `account_cycle_analysis_update`（`tier`） | 步骤 4 分批止盈与补仓 |
| 格档股息率基线、PEG 计算明细 | 核查记录/备注 | 步骤 3.5 基线对比、步骤 4 PEG 复算 |
