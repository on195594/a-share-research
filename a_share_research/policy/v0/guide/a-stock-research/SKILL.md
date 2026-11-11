---
name: a-stock-research
description: "用于 A股首次配置研究、公司公开证据、报告追问与纠错。已有持仓的交易或账户操作用 a-stock-monitor。"
license: Proprietary
compatibility: Requires local command execution, the installed a-stock-agent runtime and a-stock-lib, plus network access for live research.
---

# A股首次投研

## 路由

用户明确只问公司公开证据、报告追问或纠错，不涉及配置、买卖、仓位和账户时，按 [Research 服务](references/research-service.md) 先复用报告、再补关键缺口，直接交付有出处的回答；无需先完成 A—F 评分。以下流程用于完整首次配置研究。

1. 确认股票代码、研究目标；未指定时交付简洁首版。
2. 先只读运行 `a-stock-cache holdings <股票代码> --active-only`。确认已持仓后立即终止本 Skill，改用 `a-stock-monitor`；账本不可用时可继续公开研究，但仅限用户已明确要求的研究任务，不得声称新仓或给账户级动作。用户仅要求核对持仓、且查询结果不完整时，说明缺口并停止澄清，不擅自启动首次研究。
3. 先串行运行本地命令，取得 `check` 结果前不调用 Research 服务：
   ```bash
   a-stock-fetch fetch <股票代码>
   a-stock-cache check <股票代码>
   ```
   `fetch` 失败即停止；不得用网页报价替代。`ANALYSIS_HIT` 输出已包含当日结论正文，直接展示当日缓存并停止，不再单独读取缓存/快照、调用 `get-analysis` 或重新 QA。其他状态只补运行时指出的缺口；已配置 Research 服务时按上述服务资料先复用相关报告与证据，只有新的关键问题才发起研究。
4. 依据最新正式披露确认主营并选择 A—F 框架；无法确定时询问用户。量化框架不适用时只给定性摘要并停止评分。
5. 需要评分时依次调用 `a-stock-cache checklist <股票代码> <框架>` 和 `a-stock-cache score-fundamentals ...`。枚举、阈值、红线、完整性、规则版本与计算结果只服从命令输出和 `a-stock-lib`，不得在 Skill 中复算或覆盖。
6. 把完整机器状态作为 decision-v1 JSON 经 stdin 交给 `a-stock-cache --confirm-write set-analysis`；仅在用户确认具体写入后执行。Markdown 只用于展示，不得反推或充当机器状态。
7. 首次报告完成后，把正文固定为不可变快照再交给独立 `a-stock-qa`；正文改变必须重新 QA。

## 检索预算与停止条件

- 先复用安装态 runtime；Web 只补缓存未覆盖且会改变门禁或动作的事实，优先交易所、监管机构与公司正式披露。
- 每只股票默认最多一批、最多四个独立查询；只对失败、缺失、过期或冲突项做一次有理由的 fallback。同一 PDF 最多两种提取路径。
- runtime 已提供报价或结构化字段时不得探测重复 Provider。批量股票逐股串行执行，最后再比较。
- 达到已校验结论或 fail-closed 结论后停止；不为非阻塞新闻、研报、技术指标或链接巡检延长首版。

## 安全边界

- 结构化状态缺失、过期、冲突或命令失败时 fail-closed：明确缺口，保持 `incomplete/not_formed`，不得补造分数、评级、矩阵或可交易结论。
- 买入动作主干是 Research 有效论点卡立场（现在分批/等价格/回避；旧等证据按现在分批并附加仓检查点），A–F 矩阵只作参考；某项 incomplete 只扣对应项，动作上限降为首批 1/3，不整体默认等待；股数只取 `a-stock-cache position-size`。见 [报告合同](references/report-contract.md) 第 1 节。
- 账户风险政策 `untradable_boards` 所列板块（如科创板）照常研究，但动作写“研究保留：账户无该板块交易权限，不形成买入动作”，不给股数（[2026-11-04 板块权限规格](https://github.com/on195594/a-stock-agent-skills/blob/master/docs/specs/2026-11-04-untradable-boards-spec.md)）。
- 分数、分类、风险门、估值冲突、组合风险、L3、建议动作和来源时效均以 runtime 的结构化合同为准；不得从自然语言标签重建。
- 持仓、分析、预警、L3/Tier 与交易账本写入均属 W1。只有用户确认具体动作后才可追加全局 `--confirm-write`；读操作不得携带该参数。
- 本 Skill 提供首次配置研究和公开证据问答；止损、减仓、补仓、L3 与已有持仓操作由 `a-stock-monitor` 处理。本框架基于公开信息，不构成投资建议。

## 按需资料

- `FUNDAMENTALS_HIT` 或 `FULL_MISS` 需要展开研究时：读取 [完整研究流程](references/research-execution-flow.md)。
- B/C/D 框架需要周期判定时：读取 [周期评估](references/cycle-assessment.md)。
- 需要择时调整时：读取 [择时调整](references/timing-adjustments.md)。
- 展开取证前读取 [报告合同](references/report-contract.md)：先确定会改变结论的关键论据与来源，默认五段首版，不先写长文再补证据；QA复用同一正文快照。
- 催化、产品周期或订单兑现问题：读取 [催化与产品周期](references/catalyst-cycle-analysis.md)。
- 需要详细数据来源与 CLI 说明时：读取 [数据与缓存合同](references/data-cache-contract.md)，但以当前命令帮助和输出为准。
