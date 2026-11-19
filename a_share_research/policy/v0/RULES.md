# 政策 v0 规则来源索引

本索引把旧 Skills 的规则、带日期规格与合仓后的实现连接起来，不复制阈值或另建规则源。源码与指南取自 `a-stock-agent-skills` 固定提交 `1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8`；下表的规格链接全部固定到该提交，供后续归档后追溯。当前开发约束以根目录 [AGENTS.md](../../../AGENTS.md) 为准，合仓边界见[合一方案](../../../docs/consolidation.md)。

`guide/` 只剩持仓监控指南 `a-stock-monitor`，保留不等于重新授权旧工作流。旧首次研究指南 `a-stock-research` 与本地 QA 指南 `a-stock-qa` 已于 2026-11-15 退役（去向见文末），表中标“已退役”的资料从该次提交之前的历史查看。本包 [research_client.py](research_client.py) 只提供进程内读取；发起研究与反馈经研究 API，[研究服务指南](guide/a-stock-monitor/references/research-service.md)已据此改写（2026-11-19）。`fetcher.py`、`commands_analysis.py`、`check_regulatory_freshness.py`、`performance.py` 与确定性评分器已退役（`2180beb` 及后续清理），表中提到它们的条目只记录规则当时的实现位置，源码从该提交之前的历史查看；指南里依赖 `a-stock-fetch` 和 `a-stock-cache check / checklist / score-fundamentals / set-analysis / set` 的步骤不可执行。切换前宿主仍使用旧入口；新 tools 服务和统一 Skill 的启用按合一方案验收，不由本索引宣布完成。

下表列的是对应实现或资料入口，不代表规格中的所有建议、历史部署步骤和验收均已在合仓版本完成。历史规格的用户授权、安装路径、版本号、截止日期和逐次确认要求不自动成为新系统规则。规则之间的替代关系见表内说明；改变 v0 的投资语义须用新政策版本和对照证据。

## 规则、规格与当前入口

| 规则主题 | 固定规格 | 当前实现或资料；适用边界 |
|---|---|---|
| 只读命令、行情时效与发布安全 | [08-09 审查整改][audit] | [db.py](db.py) 的 `read_only_scope` / `read_only_db_session`、[market_quotes.py](market_quotes.py)、[risk_budget.py](risk_budget.py)；旧 installer、cron 安装及其发布门禁没有作为 v0 运行代码迁入，新部署按主仓入口。 |
| PB/BPS 同期、周期情景、唯一仓位出口 | [08-09 投资框架修复][framework] | `fetcher.py` 的 `_fetch_pb_pe_data`、旧C 框架（已退役）、[组合风险指南](guide/a-stock-monitor/references/portfolio-risk.md)。规格中矩阵唯一决策出口、incomplete 一律等待已被 11-04 进取决策修订，不能恢复旧否决链。 |
| 产品周期与主题催化的证据分层 | [08-10 催化周期][catalyst] | 旧催化周期指南（已退役）；这是人工/Agent 分析资料，不是自动订单或利润推断器。 |
| 静态 PE 与真实 TTM 区分、5 年分位、QA verdict | [08-11 复盘修复][wuxi] | `fetcher.py` 的 `compute_pe_percentile`、旧A 框架（已退役）、旧QA 指南（已退役）。`pe_ttm` 兼容别名与后增的 `pe_ttm_true` 不同；QA 文本通过不证明事实或收益。 |
| 确定性基本面评分接线 | [08-23 Lib 消费者切换][scoring] | 评分命令 `cmd_score_fundamentals` 与评分器 `framework_scoring.py` 均已退役，本仓不计算 A–F 基本面分。 |
| PS 同口径分位与最新财报方向快照 | [08-23 数据能力与 Skill 拆分][data] | `fetcher.py` 的 `compute_ps_ttm_percentile_5y` / `_build_latest_report_snapshot`、旧缓存合同（已退役）。年报基线与更新季报分别保留，不将累计财报机械年化。 |
| 持仓查询物理只读、缺库与未持仓区分 | [08-27 时延与只读][readonly] | [commands_holdings.py](commands_holdings.py) 的 `cmd_holdings`、[db.py](db.py)、旧Research 指南（已退役）。旧宿主工具次数、搜索预算和安装步骤是当时工作流，不限制主仓研究 AI 的自主取证。 |
| L3 重要性、scope/action、活动论点版本 | [08-27 L3 与论点版本][l3] | [commands_monitor.py](commands_monitor.py) 的 `_l3_view`（`cmd_thesis_rewrite` / `cmd_l3_update` 已于 2026-11-19 删除，写入改由 `account.cycle_analysis.update`）、[L3 指南](guide/a-stock-monitor/references/daily-monitoring-and-l3.md)。历史候选个股载荷不是当前账户指令；D7 的分析状态追加历史另按合一方案实施，不能把旧覆盖式更新称为追加账本。 |
| 有界首版、不可变 QA 输入、外部集中风险标签 | [08-28 执行效率][execution] | 旧执行流程（已退役）、旧A 框架（已退役）、旧QA 指南（已退役）。主要是指南合同，不是运行时自动 QA 执行器。 |
| 复用价格历史计算五交易日涨跌 | [08-30 工具链效率][toolchain] | `fetcher.py` 的 `compute_price_change_5d` / `_load_price_df`；保留来源日期与缺失原因。跨宿主审计路由和旧交付流程不作为本包的可执行能力。 |
| PE/PS 数据能力与按需加载说明 | [09-01 合同对齐][disclosure] | 旧缓存合同（已退役）、[Monitor 指南](guide/a-stock-monitor/SKILL.md)。字段能力不等于当前输入必然有效；5 年与 10 年窗口不可互代。 |
| incomplete 的 QA 语义与 PB 兼容性 | [09-02 QA / 估值兼容][compatibility] | `fetcher.py` 的 `compute_valuation_compatibility`、旧报告合同（已退役）、旧QA rubric（已退役）。跨期 BPS 的早期全局否决已由 09-09 收窄；旧 QA 指南保留不表示已接入新入账门。 |
| P3 流动性休克（P0 监管、P1 ROE、P2 现金流已删除） | [09-07 风控增强][gates] | [risk_gates.py](risk_gates.py) 的 `liquidity_shock_gate`、[commands_monitor.py](commands_monitor.py)、[L3 指南](guide/a-stock-monitor/references/daily-monitoring-and-l3.md)。P0–P2 去向见文末。 |
| monitor-v1 正交状态、升级原因与快照审计 | [09-08 监控治理][monitor] | [commands_monitor.py](commands_monitor.py) 的 `build_monitor_snapshot`、[monitor_contract.py](monitor_contract.py) 的 `validate_monitor_snapshot`、[决策表](guide/a-stock-monitor/references/decision-table.json)。快照合同与文本路由分开；原定宿主调度方式不等于新 tools 服务。 |
| TTM / PEG、财务门接线、PB 跨期更新与框架路由 | [09-09 决策可用性][availability] | `fetcher.py` 的 `_build_risk_gate_inputs` / `_compute_ttm_valuation` / `compute_valuation_compatibility`、`commands_analysis.py` 的 `_decision_meta`、[framework_catalog.py](framework_catalog.py)。基本面/时机资格分开；最终动作还须结合 11-04 修订，不能沿用旧版统一等待。 |
| 风险状态闭环、单事务快照、政策参数与账户业绩 | [09-17 风险闭环与业绩][closure] | [monitor_contract.py](monitor_contract.py)、[commands_monitor.py](commands_monitor.py) 的 `_load_monitor_local_snapshot`、[risk_policy.py](risk_policy.py)、`performance.py`。规格中的 Tracker 实验不在 v0；“三仓保持不变”已被合一方案取代；旧基准不等于新增中证 800 全收益口径已完成。 |
| 策略现金分母、浮盈回吐与本金归因 | [10-22 分母与超限归因][denominator] | [risk_policy.py](risk_policy.py) 的 `resolve_risk_parameters`、[risk_budget.py](risk_budget.py) 的 `assess`、[commands_monitor.py](commands_monitor.py)。超限复核不能凭空生成卖出标的或股数；参数文件仍是外部输入。 |
| 论点卡、待复核升级、分红敏感性换仓比较 | [11-01 研究卡片提醒][cards] | [monitor_research.py](monitor_research.py) 的 `summarize`、[monitor_contract.py](monitor_contract.py)、[research_client.py](research_client.py)。分类与提醒规则保留；读取传输已改为进程内，旧 HTTP 配置与提交路径不适用本包。 |
| 机会成本提示按候选合并与七日时效 | [11-02 换仓提示][digest] | [monitor_research.py](monitor_research.py) 的 `summarize` / `_fresh`、[估值复查指南](guide/a-stock-monitor/references/valuation-and-annual-review.md)。完整比较保留在 `research.opportunity_costs`，提示时效不抹掉比较记录。 |
| Research 立场主导、incomplete 局部降级、分批定仓 | [11-04 进取决策][proactive] | [position_size.py](position_size.py) 的 `size`、[monitor_research.py](monitor_research.py) 的 `_opportunity`、旧报告合同（已退役）。累计阶段不得前移；基准仓建议属于旧政策资料，不表示程序已自动买入 ETF。 |
| 财报检查点作为首批参考、保留后续确认条件 | [11-04 参考证据][reference] | [monitor_contract.py](monitor_contract.py) 的 `stance_add_trigger`、[monitor_research.py](monitor_research.py) 的 `_checkpoint_detail`、旧报告合同（已退役）。旧“等证据”保留兼容，不能以日期到期自动认定后续披露达标。 |
| 治理红线：官方证实占款或违规担保即不新增仓位 | 所有者 2026-11-15 决定保留（原载旧首次研究指南第五步） | [组合风险指南](guide/a-stock-monitor/references/portfolio-risk.md) 的“治理红线”条。指南条款，由执行买入或加仓判断的 Agent 核对官方披露；没有程序门。 |
| 不可交易板块：保留研究、排除建议 | [11-04 板块权限][boards] | [boards.py](boards.py)、[risk_policy.py](risk_policy.py) 的 `untradable_boards`、[position_size.py](position_size.py)、[monitor_research.py](monitor_research.py)、[monitor_contract.py](monitor_contract.py)。权限限制同时作用于定仓、机会与换仓候选，不删除研究。 |

## 合仓时保留与替换的边界

- 账户/政策表结构由主仓迁移拥有，逐表逐列映射见 [account/mirror.py](../../account/mirror.py)；旧 `schema.py` / `schema_ledger.py` 不再是结构所有者。
- 数据库路径与权限由 [paths.py](paths.py) 和 [db.py](db.py) 处理；旧 `CACHE_DB_PATH` / `A_STOCK_STATE_DIR` 不作为本包连接入口。
- A–F 分类（[framework_catalog.py](framework_catalog.py)）只决定止损比例与定仓，作为 v0 基线保留；确定性评分器与各框架评分文件已退役。C/D 分级加仓所需的股息率档位已抄入 [步骤 3.5](guide/a-stock-monitor/references/step3.5-cd-accumulation.md)。
- 旧首次研究流程（2026-11-15 退役）：评分阈值与清零红线、格雷厄姆预筛、择时分、双轨评级矩阵、异动清单、报告模板、缓存与搜索预算编排删除，单一行业 40% 上限按所有者决定不保留；周期分红传导、基本面与股价背离、汇兑同口径剔除、催化证据阶梯四项分析思路写入共享 Wiki 方法卡；股息率交叉验证与 PB 同期检查记为 2027-01-01 月批前的契约候选。评估依据在仓库外证据目录 `legacy-local-flow-eval-20261115/`。
- 旧 QA 的 22 项文本检查（同日退役）去向：框架路由（2）归止损定仓（`holding-framework` 迁移命令已于 2026-11-19 删除）；唯一动作出口与仓位范围（13）归研究立场、`position-size` 与板块权限；P3 门归 [risk_gates.py](risk_gates.py) 与监控快照，P0–P2 门（19–21）随下条删除；来源、期间、估值算术与最新财报核验（1、5、8、12、15、22）由研究服务的机械校验和独立复核处理；其余随评分器删除（3、4、6、9–11、14、16–18），其中股息率交叉验证（7）见上条契约候选。这是去向说明，不是逐项等价证明。
- v0 记账与状态写命令（2026-11-19 删除）：建仓、买卖、分红、公司行动、平仓、回报与复盘补录、预警、L3、论点重写、Tier、框架迁移和缓存清理共 19 个写命令连同 `--confirm-write` 门一起删除。合一后账户与分析写入只走账本工具（`account.submit`、`account.cycle_analysis.update`），这些命令已无生产调用方；R0/R1 只读命令保留，作为新政策等价验证的对照。Tier 建仓声明与框架迁移当前没有写入入口。
- P0 监管、P1 ROE、P2 现金流三道门（2026-11-15 按所有者决定删除）：旧抓取器与 `regulatory-evidence` 补录命令退役后 `stock_fundamentals` 已无写入方，三道门在生产上对每只持仓都只能报过期，补数步骤不可执行。监控快照、持仓检查与指南不再消费它们，决策表 D03P0 随之删除；已证实的监管与诚信事件仍由治理红线和决策表的论点破坏条件覆盖，ROE 与利润连续恶化仍由 D03 覆盖，现金流质量交由研究判断。
- 阶段进度与对照结果只维护在[实施计划](../../../docs/implementation-plan.md)及仓库外交接证据中，避免本索引成为第二份状态台账。

[audit]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-09-audit-remediation-spec.md
[framework]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-09-investment-framework-repair-spec.md
[catalyst]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-10-catalyst-cycle-analysis-migration-spec.md
[wuxi]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-11-wuxi-analysis-retro-repairs-spec.md
[scoring]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-23-lib-060-consumer-cutover-and-scoring-wire-spec.md
[data]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-23-research-data-capability-and-skill-slimming-spec.md
[readonly]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-27-a-stock-research-latency-and-readonly-remediation-spec.md
[l3]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-27-l3-materiality-and-thesis-versioning-repair-spec.md
[execution]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-28-a-stock-research-execution-efficiency-repair-spec.md
[toolchain]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-08-30-a-stock-research-tool-chain-efficiency-spec.md
[disclosure]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-09-01-a-stock-skill-contract-reconciliation-and-slimming-spec.md
[compatibility]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-09-02-a-stock-research-incomplete-qa-valuation-compatibility-spec.md
[gates]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-09-07-investment-framework-and-risk-control-enhancement-spec.md
[monitor]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-09-08-a-stock-monitoring-efficiency-and-safety-governance.md
[availability]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-09-09-research-decision-availability-and-qa-integrity-repair-spec.md
[closure]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-09-17-a-stock-risk-closure-and-performance-validation-spec-v1.1.md
[denominator]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-10-22-risk-denominator-and-breach-attribution-spec.md
[cards]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-11-01-research-card-review-prompts-spec.md
[digest]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-11-02-opportunity-cost-digest-spec.md
[proactive]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-11-04-proactive-decision-spec.md
[reference]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-11-04-reference-evidence-spec.md
[boards]: https://github.com/on195594/a-stock-agent-skills/blob/1f7c5203aad956fe0c83f0e19e272ea39a4ec2c8/docs/specs/2026-11-04-untradable-boards-spec.md
