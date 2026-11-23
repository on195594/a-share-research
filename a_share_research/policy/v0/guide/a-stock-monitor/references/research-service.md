# Research 服务协作指南

研究读取、提交与纠错统一走 MCP 研究工具（`research_search`、`research_cards`、`research_card`、`research_report`、`research_request`、`research_feedback`、`research_status`），详见 [a-share Skill](../../../../../../skill/a-share/SKILL.md)。

## 一、复用监控快照与论点卡

- `policy_monitor` 快照已包含 `research` 与 `review_prompts`，监控流程直接复用。
- 卡片处于「待复核」时，从论点卡的 `pending_events` 读取待复核事件，按卡上的 `report_id` 调 `research_report` 核对原报告的公开证据，暂停解读该卡的估值区间与回报。
- 区分报告形成时边界（`valuation_calc.boundary`）与当前滚动边界（`current.valuation.boundary`）；卡片有效或 `passed=true` 仍须核对并保留 `open_questions` 与“Research 风险提示”要点。
- 研究服务不可用时保留具体错误状态，其余账户监控与止损规则照常执行。

## 二、报告阅读与时效核对

1. 先按 `compact=true` 读报告摘要，需核对原文页或程序财务表时再取 `compact=false`；本会话已取得的报告直接复用。
2. 以证据日期（`evidence.market_date`、`announcement_searches.until` 与财报披露期）而非 `created_at` 判定时效；标注 `historical_not_live_checked` 或处于财报季时，核对官方披露列表确认无更新定期报告。
3. 读接续报告时先看 `continuation.baseline_status`：基线就绪时再对照 `previous_checkpoints`（待核查问题）与 `table_changes`（同口径数据变化）；失败报告先说明 `diagnostics.problems/gaps/next_step`，仅引用已复核事实。
4. 回答中保留报告 ID、来源页码或财务表期间，写清哪些旧判断延续、哪些变化及后续观察节点。

## 三、发起新研究与纠错

- 现有证据充分时直接作答；遇到新披露、关键缺口或实质错误时提交 `research_request`（接续同公司时传 `parent_report_id`）或 `research_feedback`（按实际段落或数组下标填写 `location`）。研究 `question` 与纠错 `text` 只传公开研究问题及证据，股数、成本与账户状态留在账户侧。
- 受理后立即提供 `run_id` 与进度链接，待任务通过复核后再引用新报告。纠错生成关联新报告，保留原稿。

## 四、研究立场与加仓检查点

- 新 Research 立场取「现在分批 / 等价格 / 回避」。
- `stance_add_trigger`（或旧等证据卡的 `trigger`）是参考检查点。首批上限与后续加仓条件见[风险预算定仓](portfolio-risk.md#五基准仓定仓板块权限与治理红线)。未披露时保持观察，披露未达标时复核是否减仓。
- 硬否决、数据缺口、组合风险预算与账户板块权限照常生效。
