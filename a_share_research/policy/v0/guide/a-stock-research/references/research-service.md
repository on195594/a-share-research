# Research 服务：先复用，再补证据

通过 `a-stock-cache research` 使用 `research-v1`。凭据仅由 CLI 从环境变量或所选私有配置读取，不进入提示词、日志或命令参数。未配置时说明不可用，继续原有已授权公开取证；服务错误必须保留，不把失败当作没有报告。

只读论点卡命令：`a-stock-cache research cards` 读取扫描范围内的卡片与无卡代码，`a-stock-cache research card <六位代码>` 读取单家公司卡片；二者不会发起研究。持仓监控已在一次 `monitor-snapshot` 中读取卡片，直接复用 `research` 与 `review_prompts`，不重复调用；卡片待复核时按其 `report_id` 读取相关报告并核对 `pending_events`，区间与回报暂停解读。`--no-research` 可跳过快照内的卡片读取，服务不可用不替代或改变账户门禁。

卡片与报告的 `open_questions` 是复核明确留下的未答部分（含 `prior_status[i]`）；有效或 `passed=true` 不代表问题已经全部回答。卡片 `valuation_calc.boundary` 是报告形成时的边界，`current.valuation.boundary` 是当前滚动边界，两者不得互相覆盖。原样保留提醒 `detail` 中带代码/报告 ID 的“Research 风险提示”及具体未答问题；组合复盘即使无新提醒，也从现有嵌套卡片保留这些限制。不在 Skill 重算边界。旧卡缺字段或 null 均不能当作无风险，false 不代表立场已证明稳定；空问题数组仅表示没有记录的未答问题。警示不改变机会条件、有效状态、首批上限、账户风险门或 W1 权限，不默认等待、不自动付费补研。

HTTP 401 时停止重试，交由 owner 修复凭据及环境覆盖；恢复后沿用原 request-id。日常研究不签发或轮换 Key，不取得网页登录或账户写权限；配置和轮换由项目运维文档维护。

1. 先运行 `a-stock-cache research reports <六位代码>`，再按问题读取相关的 `research report <报告ID> --compact`。需核对关键原文或程序财务表时，才读取同一报告的完整 `research report <报告ID>`；精简视图不等于引文已核验。已在会话提供完整报告与证据时直接复用，不重复查询。
2. 检查 `status`、`passed`、`question`、`coverage.periods/missing/limits`、`evidence.market_date` 和各来源披露/取得时间。认定报告为最新前，将 `announcement_searches` 的 `until` 和报告生成时间与当前日期比较；财报季还须查官方披露列表，确认没有更新的定期报告。`created_at` 不是证据日期，历史结果不自动更新。同时核对 `continuation` 的财报期、生成时间、行情日期与公告检索截止日；`historical_not_live_checked` 不是“最新”。先看 `continuation.baseline_status`：非 `ready` 时，空的 `previous_checkpoints` 不代表没有旧检查点，须解释基线缺失、失败、未就绪或冲突；只有可用基线才进行旧疑点对照。`previous_checkpoints` 仅为待核查问题，`table_changes` 仅为同口径数据变化，不等于投资理由改变。失败报告先解释 `diagnostics.problems/gaps/next_step`，只用已复核 `facts` 并保留 `problems`，不把预算提示当作已确定的失败根因，不自动付费重跑。
3. 有 `checkpoints` 时，复用其中的下期观察条件并核对本期证据，不把观察条件当作已发生事实；旧报告缺此字段不补造。主动研究同公司且未指定父报告时，服务会按自身规则接续最近合格报告的风险与检查点，Skill 不另建历史摘要或接续状态。已有证据足够就回答；新披露、冲突或关键缺口才发起任务。新任务前用 `research budget` 读取调用上限；这是循环保护而非金额预算，费用未知不得写成免费。用户有硬金额上限而服务无法保证时停止澄清。引用旧报告用 `--parent-report-id`。把最值得补的一项问题写清楚：
   ```bash
   a-stock-cache research start <代码> --question '<待解决的问题>' --request-id '<本次唯一编号>'
   # 追问已有报告时再加 --parent-report-id <报告ID>
   a-stock-cache research status <任务ID>
   a-stock-cache research report <返回的report_id> --compact
   ```
   同一问题二选一：提交研究后等待结果（轮询间隔至少 30 秒，20 分钟仍未完成则停止等待并说明任务号和超时），或自行阅读官方披露；不得同时提交研究又自行读披露抢先下结论。提交只返回任务号；超时后只能交代证据未齐，后续接着查同一任务。提交超时先用 `a-stock-cache research resume --request-id <原编号>` 只读找回任务，不重提、不恢复执行失败任务。404 也不证明原提交失败；不换编号重建，必要的原样重试须沿用同一 request-id。改问题另需确认新任务与成本。服务尚不支持新读取接口时明确兼容性缺口，不因此重开研究。
4. 发现具体错误时：`a-stock-cache research feedback <报告ID> --location 'risks[0]' --text '<可核查的纠错说明>' --request-id '<唯一编号>'`。位置用报告实际 `summary` 或 `answer/risks/business/financial/methods/checkpoints` 数组下标。反馈产生关联的新报告，不覆盖旧文；先查新结果，不能把反馈本身当成已验证结论。`methods` 是带出处和适用边界的方法线索，跨公司使用仍需核查；针对 `methods[i]` 的成功核查会替换该条历史方法，空列表可撤下失效方法，失败不生效。
5. 聊天渠道在证据齐备后只交付一次合并答案；进度说明不得包含结论。回答保留报告 ID、源文档页码/引文或表格期间，说明哪些旧判断仍成立、哪些被新证据推翻、尚缺什么以及下一项取证的时间或触发条件。没有可比基线或没有增量就明确说明，不另建持仓状态、不自动更新 L3。不把旧报告当成原始证据，不因 `passed=true` 宣称投资结论正确。

研究方法、提示词和 A—F 框架均可提出反证或替代假设；公开证据问题不以完成评分为前提。服务不授予投资政策变更、交易或 W1 权限，也不替代 decision-v1/monitor-v1。完整首次配置研究仍按原评分和 QA 合同；服务报告不冒充该机器状态，且不提交给只支持 Skill 文本 rubric 的 QA。

新 Research 立场只取现在分批/等价格/回避。`research` 行的 `stance_add_trigger` 投影新卡 `stance_calc.add_trigger` 或旧等证据卡 `stance_calc.trigger`，不迁移旧卡。两者均按“现在分批 + 参考加仓检查点”解读，不阻止首批，动作上限为 `position-size.tranches[0]`；不得反复以首批名义追加。目标期间披露达标且更新的有效研究确认后，才按立场加后续批次；未达到列复核项，复核是否减仓，未披露不当作未达到。不自行重算行业/宏观/商品数据。硬否决、incomplete 对应缺口、组合风险预算与账户板块权限照常适用。
