# Research 报告与操作建议合同

本页定义默认五段标准评估卡和可选详细审计。写入授权、评分完整性与唯一动作出口仍以主 Skill 为准。

权威机器状态只存在于已校验的 decision-v1 JSON。Markdown/`narrative` 仅供人阅读，不得从标题、动作词、标签或自由文本反推评分、阻断、周期、估值冲突或动作。写入时将完整 decision-v1 JSON 通过 stdin 交给 `a-stock-cache --confirm-write set-analysis`；旧记录没有 `decision_json` 时只能按 `LEGACY_ANALYSIS_HUMAN_ONLY` 展示，不得自动回填。

## 有界首版

用户未明确要求详细审计时，默认输出五段决策卡；每段只保留1—3条决策相关证据。强制门、状态和来源仍须出现，但不得用12个展开章节淹没“今天是否买”的答案。用户明确要求详细审计时，才展开基本信息、格雷厄姆、异动、周期、基本面六维、红线、压力测试、择时、双轨、综合分、防韭菜和操作建议原12模块。

正式 PDF 引用前必须核验首页真实标题、公告日期和发布主体；搜索摘要只用于定位原文。无法核验文件身份时写 `source_identity=incomplete`，不得把投资者活动记录、业绩预告或新闻摘要写成定期报告。

## 默认五段标准评估卡

### 1. 结论语义

首句必须在以下三种动作词中选择一个，并紧接原因：

- `回避`：已确认 P0/P2 blocked、框架红线，或 Research 立场为“回避”；
- `等待`：Research 立场为“等价格”且未到价或已过期时，必须写出程序等待价及有效期；没有有效论点卡时，写清缺什么；
- `买入候选`：Research 立场为“现在分批”（包括旧“等证据”卡），或“等价格”的价格已到且在有效期内。

动作主干是 Research 有效论点卡的 `stance_calc`（[2026-11-04 规格](https://github.com/on195594/a-stock-agent-skills/blob/master/docs/specs/2026-11-04-proactive-decision-spec.md)）。A–F 评分与双轨矩阵只作质地与择时参考，不单独否决。某个门禁或评分项 `incomplete/not_formed` 时，只扣对应项：列出缺口及其影响，动作上限降为“首批 1/3”，不得整体默认等待，也不得写成已确认公司不值得买或已确认高估。卡片待复核或无卡时，先按 Research 服务资料补研究，不用矩阵替代立场。

按 [0.1.37 参考证据规格](https://github.com/on195594/a-stock-agent-skills/blob/master/docs/specs/2026-11-04-reference-evidence-spec.md)，新报告立场只取现在分批/等价格/回避。现在分批的 `stance_calc.add_trigger` 和旧等证据的 `stance_calc.trigger` 都是加仓检查点（参考，不阻止首批），动作上限为 `position-size.tranches[0]`（不足一手为 0，不能移用第二批）。目标期间实际披露达到阈值，且有更新的有效 Research 研究确认后，才按立场加后续批次；未达到列为复核项，复核是否减仓，未披露不等于未达到。不得反复以“首批”名义追加，不以期间日期已到、股价或旧卡自动解锁。无检查点时不补造。阈值按卡片投影原值与财务表单位、累计口径引用，不在 Skills 重算；行业趋势轮动、宏观政策与外部条件复用 Research 已取证判断，不以缺少某类资料自动要求等待。

### 2. FUNDAMENTALS_HIT 最小数据卡

展示股票/代码、当前价/quote as-of/来源、行业与框架、缓存路径。价格注明来自 `a-stock-fetch`/runtime，保留对应字段、来源与 as-of；字段名照实际输出记录，不补造。runtime 未返回有效价格时，正文记录价格缺失及原因，不用网页补价、不输出依赖该价格的确定性结果。另展示：

- `price_change_5d`；
- `pe_static`/`pe_percentile_5y`；真实 `pe_ttm_true`/`net_profit_ttm_yoy`/`peg_ttm`；
- annual PB/BPS 与 latest-report PB/BPS、`valuation_compatibility`；
- `ps_ttm`/`ps_percentile_5y`、`dividend_yield`/`dps`；
- `latest_report_snapshot.report_period`、`fields.revenue_yoy`、`fields.net_profit_yoy`；
- `_decision_meta.scoring_status`、`timing_status`、`data_completeness` 和缺失原因。

`not_evaluated` 表示 scorer 尚未运行，不得写成 complete。“普通字段X/Y”和“决策门X/3”分开展示；gate dict 非空不等于门完整。顶层字段无值时展示 `missing` 和 `null_reasons`，nested字段展示自身 `status`。

### 3. 基本面与估值

展示确定性 scorer 的 subtotal、rule version/hash、关键强弱项与最新报告方向；未运行或被P0/P2阻断时写 `not_formed`，不得人工加总。

在本段列出配置评级和时机评级作为参考；未形成时按下节风险门及缺失原因写 `not_formed`。简版不要求展开综合分或仓位矩阵，不由评级推导第二套动作。

估值只使用适用框架主轴：A看PE5年分位；B看PB10年分位；C/D按各自周期/股息/FCF；E看真实PEG或PE5年分位；F盈利看真实TTM PEG，亏损看PS但在现金跑道/稀释规则落地前只定性观察。不同报告期 BPS 的 `latest_report_update` 不阻断 A/E/F；`pb_percentile_eligible=false` 只阻断依赖PB历史分位的分支。

### 4. P0—P2风险门

按 `regulatory_gate` → `roe_structural_gate` → `cash_flow_gate` 顺序输出 status、reason_code、source、as-of、报告期和action_eligible。

- P0/P2 blocked、incomplete或review_required：`scoring_status=incomplete`、`timing_status=incomplete`，基本面小计、配置评级、时机评级、综合分和矩阵均 `not_formed`；
- P1 blocked/incomplete：保留完整的基本面小计和配置评级，仅时机、综合分和矩阵 `not_formed`；
- “未搜到负面”不得作为 clear；
- 新仓报告的P3只写：`P3：not_applicable（未持仓，仅监控第二档止损适用）`，不展开500/501状态机。

### 5. 唯一动作与复核条件

动作只取第 1 节的唯一动作（Research 立场为主干，矩阵为参考）；综合分不得另建动作。股数只取 `a-stock-cache position-size` 的输出：按单股/组合止损风险预算、框架第二档止损与交易单位，给出 1/3、2/3、全额三批；预算不足一手时如实写 `budget_below_one_lot`，不另设权重上限。缺总资产或已确认的风险快照时，只写动作和分批阶段，不换算资金或股数。闲置资金默认持基准 ETF 515800，买入个股所需资金先卖 ETF，见 Monitor 组合风险资料。

列出最多3个可验证复核节点。技术信号仅参考，不得把当日/近日涨跌或板块联动作为买卖主因。首次研究不输出持仓止损线；止损和L3由建仓后的 `a-stock-monitor` 负责。

## 关键论据与来源

- 只为会改变门禁、评级或动作的关键论据逐项附证据：披露主体/报告期/口径、官方链接及页码或章节（runtime 数据则注明字段与 as-of）。不是给所有背景句增加引用。
- 明确标注披露事实、计算结果和分析假设；推算须列同口径输入与公式，假设不得写成披露结论。摘要未覆盖的分部收入、毛利率等不能共用摘要链接冒充核验。
- 支撑关键结论的原文未取得时，标为待核实并撤回依赖它的确定性结论；仅该缺口的一次有理由 fallback 可继续，不扩大搜索预算。若该论据已进入 scorer 的 `metrics`、覆盖值或主观评估，先撤除未经核验的输入再重跑 `score-fundamentals`，按新 runtime 结果同步 decision-v1 与正文，不得只在正文改动作或手工补分；缓存写入仍须具体 W1 授权。未取得有效重算结果时不沿用旧评级/矩阵。
- 正文保留关键论据的来源字段、核验状态及可定位的原文短摘录（runtime 数据可列字段值与 as-of），供离线QA检查记录完整性和内部一致性。外部来源真实性与原文支持关系由研究阶段核实；流程QA通过不等于事实已验证或策略有效。

## 详细审计附加要求

用户明确要求详细审计时，在五段卡后展开原12模块，并遵守：

- 所有框架的每个 checklist 子项标注「checklist核验」「人工核验推翻简化判定（含实际档位）」「⚠️ 无客观数据支撑，纯人工判断」或「⚠️ 数据缺失，未核验」；
- C/D/B补周期位置，C/D补分红可持续性压力测试；
- 双轨评级、综合分和仓位模块即使被阻断也保留标题并写 `not_formed`；
- C/D操作建议包含建议持有期限和可验证的周期退出触发条件；
- 临时L3核验必须写明已核验条件数，不能笼统称“L3未触发”。

## 操作建议禁止条款

- 禁止把当日/近日涨跌、板块情绪或未经验证的财务数字作为操作主因；
- 禁止混淆品种与公司；
- 禁止把单月/单季边际改善写成已确认长期趋势；
- 引用同比增速时必须同时披露基期绝对量或量级；
- 风险必须进入分数、门禁或动作约束，不能只放在免责声明；
- 加仓/不加仓理由必须是可验证的信息论据，不得使用情绪标签。

本 Skill 基于公开信息，不构成投资建议。数据无法取得时必须明确缺口，不得凭空估计。
