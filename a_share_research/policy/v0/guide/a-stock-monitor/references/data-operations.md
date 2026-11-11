## 数据操作附录

本节用于账户差异、交易记账、状态更新和公司行动；无这些事项的日常监控不加载。

### 账户资产快照

2026-11-06 用户授权：仓位与风险预算默认复用系统已存总资产，后续日常截图由系统更新，不每次索要。

- 资产观察存于 `CACHE_DB_PATH` 同目录的 `account-snapshot.json`，仓库外、user-owned、0600；不写入风险政策，也不迁移持仓数据库。
- 收到清晰的同账户截图后，核对总资产与可见账户标识，备份原记录后更新并读回核对；这是已授权的资产观察更新，不代表成交、持仓或风险限额修改，不重复征询。账户不同、金额不可辨或互相冲突时只问具体缺项。
- 保存 `schema_version=1`、`currency=CNY`、`account_scope`、`portfolio_value`、`portfolio_value_as_of`（无明确资产估值时点则为 null）、`recorded_at`（本次记录时间）、`source=user_screenshot`、`source_ref`（截图路径或消息出处）；可另存明确可见的 `stock_market_value`、`available_cash`，缺失不填 0。
- 手机状态栏时间和记录时间不冒充资产估值时间；收到无时点截图仍更新已辨明金额，日常监控保留未知标记，不反复催问。已知旧截图不覆盖更新的资产观察。
- 后续只读监控自动使用最新保存值；显式 `--portfolio-value` 仅覆盖本次，不回写、不混用旧范围/时点。资产文件不存在时才补一次初值，非法记录或账户冲突不得静默回退成已确认预算。

### 对账先闭环，不重复研究

1. 用本轮快照对比用户最新截图/已成交说明，列出股数、账户范围、成交、费用税费及分红差异；先复用已提供材料，不重跑取价。成本口径与交易限制见 [对账门](portfolio-risk.md#券商成本与规则参考成本对账门)。
2. 将“用户确认、仅供本次分析的持仓”与“尚未同步的正式账本”分开。无法恢复历史材料时只请求具体缺项；未闭合时不引用旧风险汇总为当前值，不用临时持仓手算风险或推导交易动作，不编造成交价、日期或零费用，不把卖出回款重复加到截图现金。
3. 一次列齐最小补充项及其用途：成交日期/价格/数量/费用税费、分红实际到账；用户只说“已卖出”不是完整记账参数，也不自动授权写入。公开公告由系统核实，到账事实由用户确认。
4. 参数齐全后展示拟记事件，取得具体写入授权再调用下列 W1 命令；复用已有持仓快照，必要时只读 `holdings <代码>`，用 `position-return <代码> <快照现价>` 核对回款/分红合计（无有效快照价时不省略价格触发重新取价）。当前 CLI 没有逐笔事件列表，合计一致不能证明事件未重复；须结合既有记账回执核对，无法确认是否已记则请用户补充确认依据并暂缓该笔写入，避免重复成交或重复分红。建议、授权、申报、成交、到账事实分开。
5. 写后复核股数、成本来源、事件与风险，交付“已闭合/仍缺什么/影响哪项判断/下次复核条件”。仅确认本次补录，不声称历史全部闭合；未写入则明确待办，不能用覆盖当前成本替代重建。

以下示例在执行命令的同一 shell 中先设置 `CACHE=a-stock-cache`；该变量仅是已安装 CLI 的缩写，不包含授权参数。每次新 shell 都需重新设置，也可直接将 `$CACHE` 替换为 `a-stock-cache`。

### 交易账本命令（禁止直接 SQL 改持仓）

本项目采用**单行持仓 + 不可变交易事件**：`holdings` 保存当前状态，
`holding_events` 保存每次买入、卖出、费用、税费和现金分红。任何交易都必须通过
下列命令写入，禁止手工 UPDATE 成本、股数或平仓状态。

```bash
# 全新股票第一次建仓；会固化持仓 framework、initial_shares 和最初 buy_date
$CACHE --confirm-write add-holding <代码> <成交价> <股数> [--notes 备注] \
  [--fee <金额>] [--date YYYY-MM-DD] [--report-id <报告编号>]

# 已有持仓加仓；自动计算含买入费用的加权成本、重算框架止损线，
# 保留最初 buy_date，并维护结构化 main_entry_date
$CACHE --confirm-write buy-holding <代码> <买入价> <股数> [--fee <金额>] [--date YYYY-MM-DD] \
  [--report-id <报告编号>]

# 部分或全部卖出；校验交易所整手/零股申报约束，FIFO分配到lot，
# 同步剩余股数并记录费用、印花税和已实现盈亏
$CACHE --confirm-write sell-holding <代码> <卖出价> <股数|all> \
  [--fee <金额>] [--tax <金额>] [--date YYYY-MM-DD] [--report-id <报告编号>]

# 记录实际到账的现金分红总额
$CACHE --confirm-write record-dividend <代码> <现金总额> [YYYY-MM-DD]

# 只读：按登记日账本持股找出已实施但未记账的分红，并给出 corporate-action 命令
$CACHE dividend-check [代码]

# 用全部买卖现金流、费用税费、分红和剩余市值计算总回报及真实持有天数
$CACHE position-return <代码> [当前价]

# 只读：关联报告的每笔买卖，从交易日收盘到期末收盘含分红送转的收益，相对沪深300全收益的差，及未关联笔数
# 起点或期末没有当日收盘（停牌、行情未更新）时只说明原因，不作比较
$CACHE report-trades [--end YYYY-MM-DD]
```

这笔交易依据了某份 Research 报告时，带 `--report-id`：用决策时实际读到的那份报告。
决策后才录入且卡片已更新时，不用新卡片的编号，用 `research reports <代码>` 按时间找，不确定就问用户。
没有依据报告（如止损、再平衡）就不带；不事后猜测，旧记录不回填。
程序写入前向 Research 核对：报告须属于这只股票、已通过复核、不晚于交易日，否则拒绝且不记录；
Research 不可用时同样不记录，稍后重试（事件不可改，关联不能事后补）。程序分不清同股票的多份报告，
也看不出同一天内报告是否晚于成交，须核对输出里的报告时间。

成本对账或用户提供账户截图时先运行 `dividend-check`：命中项向用户确认是否到账后按其给出的命令记账
（登记日后股数已变时它改给按应收总额的 `record-dividend`，含转增则只提示人工核对），
不让用户自己查分红；数据获取失败的代码报告“未核对”，不推定无分红。卖出时印花税按成交额×0.5‰计入
`--tax`，不因回单未列明而记 0。

### P0 监管门证据补录

`regulatory_gate` 为 incomplete 时，每个年报期补一次官方证据；同一年报期内 `fetch` 刷新会沿用，
出现新年报期后须重补。只用交易所、证监会、巨潮公司公告与年报原文（审计报告、内控审计报告、
“处罚及整改/立案调查”一节、母公司报表），不用媒体或数据商转载；“未搜到负面”不能填 clear。
状态由 runtime 计算（只传事实字段，不传 `listing_risk` 等预设子门对象，否则被拒），用户确认写入后执行：

```bash
echo '<P0事实JSON>' | $CACHE --confirm-write regulatory-evidence <代码>
```

事实 JSON 字段：`report_period`（须等于缓存 `data_period`，如 `2025年报`）、`exchange`/`board`、
带时区 `as_of`、`sources`（逐项写明文件名与发布方）、`listing_status`（normal/st/*st/delisting）、
`financial_opinion` 与 `internal_control_opinion`（unqualified/qualified/adverse/disclaimer）、
`investigation_status`（none/company/related_person，none 须有年报“不适用”等正式陈述）、
`latest_annual_attributable_profit`、`parent_unallocated_profit_at_year_end`（深市另需
`consolidated_unallocated_profit_at_year_end`）、`three_year_cash_dividend`、`three_year_annual_profits`。
缺项就留空，runtime 会判 incomplete；年报期内新出现风险警示或立案时，重补 blocked 证据。

`corporate-action` 已自动生成对应现金分红事件；同一次分红不要再调用
`record-dividend`，否则会重复计算回报。后者只用于不伴随规则参考成本调整的独立补录。

`close-holding` 与 `update-return` 仅保留给旧数据兼容，不再作为本 skill 的交易写入路径。
每次写入后执行 `$CACHE holdings`，确认股数、成本与 buy_date；再按 [有效政策与分母证据](portfolio-risk.md#有效政策与分母证据) 默认运行 `$CACHE portfolio-risk`，复用已存总资产、账户范围和时点，沿用本轮 `--policy-file`（如有），不重新索要截图或把旧金额当作显式覆盖传入。仅本次明确覆盖总资产时，才同传 `--portfolio-value` 与同源已确认的 `--account-scope`、可选 `--portfolio-value-as-of`；不得只给旧存值补一个新时点。政策或账户证据不齐时报告风险复核未闭合，不回退预算、不否认已成功记账；这项写后复核不属于只读监控的重复取价。

### 结构化状态命令

```bash
# L3
$CACHE --confirm-write l3-add <代码> <original|recovered|new_monitoring> "<条件>" ["临时出场规则"]
$CACHE --confirm-write l3-update <条件id> <pending|not_triggered|watch|triggered> \
  <YYYY-MM-DD> "<证据>" [下次复核日期]
$CACHE l3-list <代码>
$CACHE l3-list <代码> --all

# 从 stdin 读取一个 JSON 对象，原子版本化 L1/L2、退役完整旧L3并创建新L3
$CACHE --confirm-write thesis-rewrite <代码> < thesis-rewrite.json

# Tier（豁免声明只能在建仓当日写入）
$CACHE --confirm-write tier-config <代码> <A|B|C|none> <目标涨幅%|none> <E|F|none>
$CACHE --confirm-write tier-update <代码> <tier1|tier2|tier3> <状态>
# Tier1 实际补回已卖出份额并完成 buy-holding 后，显式开启新的 Tier1 周期；Tier2/3 不变
$CACHE --confirm-write tier-update <代码> tier1 pending

# 显式迁移既有持仓框架并按 reference_cost 重算止损线
$CACHE --confirm-write holding-framework <代码> <A|B|C|D|E|F>

# 预警；reason_code 是稳定去重键
$CACHE --confirm-write alert-open <代码> <yellow|red> \
  <holding_deterioration|entry_valuation|unverified> \
  <reason_code> <复核日期|none> "<原因>" ["证据"]
$CACHE --confirm-write alert-pending <代码> <reason_code> "<仍缺的数据/待核实说明>"
$CACHE --confirm-write alert-resolve <代码> <reason_code> "<解除证据>"
$CACHE alerts <代码>
```

`thesis-rewrite` payload 必须含 `l1`、`l2`、`rewrite_reason`、完整的
`retire_l3_ids`，以及非空 `new_l3`。每条新L3必须含 `condition`、`scope`、`action`、
`materiality_basis` 和 `temporary_exit_rule`；scope为
`aggregate/core_driver/non_core/governance`，action为 `review/reduce/exit`，其中
`non_core` 只能使用 `review`。`core_driver/aggregate + exit` 的重要性与确认期约束、
`governance + exit` 的集团级确定性证据必须写入相应文本字段。校验失败、跨持仓ID、遗漏活动L3或任一写入失败都会回滚全部
修改。TTM现金流公式及口径写入条件/重要性/临时规则文本，运行时不负责代算财务指标。

未出现活动论文版本时，`l3-list` 显示 `legacy contract` 并保留旧行为；一旦重写，只有绑定
当前活动论文且未退役的合法L3可参与动作。`l3-update` 拒绝退役行，历史记录只通过
`--all` 查看。L3、Tier、预警和主力入场日期不得再以自由文本 notes 作为唯一事实源；
notes 只保留无法结构化的补充说明。

### 历史预警查询

使用 `$CACHE alerts <代码>`。首次迁移会把旧版 `analysis_results.flags` 原样转成
`unverified + pending` 预警，保留证据但不计入恶化项门槛；复核后必须重新分类或解除，
不再直接按历史 JSON 条数累计风险。`legacy` 或 `pending` 本身不是解除依据；不得为减少噪音批量清除。证据不足时保留待核实状态，重新分类或解除仍须用户确认具体 W1 动作。

### 除权成本调整授权规则

```
新成本 = (建仓成本 - 每股现金分红) / (1 + 转增比例)
触发：确认除权日或收到分红入账
操作边界：
  - 用户只问"怎么算"时：若 DPS 或转增比例未提供，先追问完整参数再计算；只输出新成本与
    Tier 1/2/3 目标价，附提醒「如需更新缓存，请明确说更新」；不得写入运行时状态。
  - 用户含写入意图时（包括：明确说「更新持仓/cache/成本」、同一句话里「算完再更新」、
    「更新为…」等表达）：先给出拟写入的新成本与重算后的 Tier 目标价，请求用户确认。
    有效确认词：「确认」「是，写入」「好，更新」；单纯认可计算结果（「嗯」「算对了」）
    不视为写入授权。
  - 用户明确确认后执行：
    ```bash
    $CACHE --confirm-write corporate-action <代码> <每股现金分红> <转增比例> [YYYY-MM-DD]
    ```
    该命令把经济成本与规则参考成本分开：现金分红作为回报事件记录，不从经济投入中
    重复扣除；Tier/止损参考成本按除息送转公式调整。不得手工 SQL 或删行重建。
  - 除权成本调整不触发卖出记录，不调用 update-return（该命令仅用于实际卖出后）。
```

### 结构化状态字段参考表

| 字段 | 写入时点 | 适用范围 | 消费方 |
|---|---|---|---|
| `buy_date`（holdings 结构化列，非notes文本） | 首次建仓（INSERT） | 全部持仓 | 步骤5年度复查计时 |
| 出场路径 / 目标 | 建仓当日通过 `tier-config` | A/E/F框架轻仓试探 | `holding_tier_state` |
| Tier1估值豁免声明 | 建仓当日通过 `tier-config` | E/F正式仓位 | `holding_tier_state` |
| 主力入场日期 | `buy-holding` 自动维护 | 全部持仓 | `holdings.main_entry_date` |
| 压力测试后前瞻股息率数值+来源+核查日期 | 每次步骤3.5核查（无论是否加仓） | C/D框架轻仓试探持仓 | 步骤3.5「首次核查」基线比较 |
| 格档加仓依据（证据+日期、加仓前/后累计增持量、原建仓股数基准） | 步骤3.5"格档小额试探加仓"触发时 | 同上 | 步骤3.5阶段3仓位计算的累计上限判断 |
| Tier1/2/3状态 | `tier-update` | A/E/F | `holding_tier_state` |
| PEG计算记录（数据源、as-of日期、NP0、NP2、`G_pct`、PEG结果） | 每次涉及E框架PEG判定时（Tier1/Tier2-3/Tier1后补仓均适用） | E框架持仓 | 步骤4各处PEG判定 |
| L3核查结论 | `l3-add/l3-update` | 全部持仓 | `holding_l3_conditions` |
| L1/L2论文版本与L3替换 | `thesis-rewrite` | 新建或重写论文的持仓 | `holding_thesis_versions` + `holding_l3_conditions` |
| 预警及消除依据 | `alert-open/alert-resolve` | 全部持仓 | `holding_alerts` |

本表汇总各步骤的持久化状态。除 PEG 计算明细和格档核查证据仍可作为补充 notes 外，
交易、L3、Tier、预警与主力入场日期均以结构化列/表为准。

P3 延迟复核复用 `holding_alerts`：固定 `reason_code=price_stop2_liquidity_defer`，`evidence` 为包含版本、`defer_started_at`、原第二档价格和原市场快照的 JSON，`review_due` 可为带时区 ISO 8601。check-holdings 只输出候选；用户确认后才执行：
```bash
a-stock-cache --confirm-write alert-open <代码> red holding_deterioration price_stop2_liquidity_defer <review_due> "系统性流动性休克观察中" '<evidence-json>'
```
首次起点不得因重复运行或持续 501 家跌停而重置，停牌/封板不可解除或伪造成交。
