# 测试目录

## 组织

- `test_*.py`：按业务能力保存回归断言，不按某次交付或版本另建重复测试。
- `data/`：真实采集的 JSON（含 `.json.gz`）/PDF 样本，属于回归输入，不是临时文件。采集来源与口径见样本内元数据及使用它的测试。
- `support/reports.py`：茅台年报摘录、财务数字和引文检查用报告构造器。
- `support/continuation.py`：真实生产/K2 报告记录及 SQLite 装载器。
- `support/templates.py`：统一开启自动转义的 Jinja 环境与报告渲染入口。
- `support/financials.py`：财务序列、图表与研究工具共用的真实财务样本及隔离副本构造器。
- `support/valuation.py`、`support/split_numbers.py`：估值案例与跨页数字采集样本的领域支撑。
- `conftest.py`：缓存隔离、共享 Pi 运行环境和 HTTP 服务 fixture；模型测试未配置运行环境时跳过。

通用支撑不再从 `test_check.py`、`test_continuation.py`、`test_financial_series.py` 导入；其他领域专用采集数据/fixture 暂留原测试模块，不为整理目录搬动业务代码。报告构造器只用于检查边界，不能充当真实模型验收结果。不要修改公共样本对象；需要改变输入时复制后再处理。

## 验证范围

从仓库根目录运行 `nice -n 15 uv run pytest -x -q tests/test_相关能力.py`。选择受影响的真实案例及正常案例；同一版本已通过的结果复用，不因提交或部署再跑一轮。

常见入口：

| 能力 | 测试 |
|---|---|
| 公告标题与报告日期 | `test_announcements.py`、`test_mdna_pages.py` |
| 申万行业与同业快照 | `test_industry.py`、`test_peer_snapshots.py` |
| 财务序列与报表核验 | `test_financial_series.py` |
| 价格参考与技术纪律 | `test_price.py` |
| 图表与可视化几何、投资卡片 | `test_charts.py`、`test_cards.py`、`test_company_card.py` |
| 引文、复核账本、接续报告与公司法定事项 | `test_check.py`、`test_review_evidence.py`、`test_open_questions.py`、`test_continuation.py`、`test_attention.py`、`test_feedback_retry.py`、`test_research_methods.py`、`test_companies.py`、`test_company_identity.py`、`test_stock_codes.py` |
| 精确计算、报告载荷与研究工具 | `test_research_tools.py`、`test_calculation_assumptions.py`、`test_conditions.py`、`test_report.py`、`test_report_language.py`、`test_valuation.py`、`test_research_repair.py`、`test_repair.py`（真实披露与离线协议输入验证假设链传播、长期补充回报、分情景引用、共享补证、修订调用储备、来源冲突、写作契约接线及跨材料账本；不代表真实模型语义验收或研究通过率） |
| 候选初筛与批次管理 | `test_screening.py`、`test_screening_entry.py`、`test_screening_preflight.py`、`test_batch_c.py`、`test_monthly_batch.py` |
| 历史回测 | `test_backtest.py` |
| 判断记分、投资立场与估值诊断（V0–V5、A1、A3、A4、契约面锁定） | `test_validation.py`、`test_forecast_review.py`、`test_judgment_contract.py`、`test_agenda.py`、`test_stance.py`、`test_hypotheses.py`、`test_valuation.py`、`test_valuation_ui.py`、`test_zones.py`、`test_scans.py`、`test_advisor_sources.py`、`test_r2_queries.py` |
| 账户账本、净值与对账（`account/`） | `test_account.py`、`test_account_ledger.py`、`test_account_reconciliation.py`、`test_account_nav.py`、`test_account_nav_core.py`、`test_account_evidence.py`、`test_account_analysis.py`、`test_account_audit.py`、`test_account_current.py`、`test_account_valuation_time.py` |
| 投资政策与监控（`policy/`、`policy/v0/`） | `test_policy_identity.py`、`test_policy_registry.py`、`test_policy_replay.py`、`test_policy_monitor.py`、`test_policy_derived.py`、`test_policy_stop_evaluation.py`、`test_policy_daily_evaluation.py`、`test_benchmark_evaluation.py`、`test_web_policy.py`、`policy_v0/` |
| 受控工具、CLI/MCP 与外部入账（`tools/`） | `test_tools_service.py`、`test_tools_auth.py`、`test_tools_control.py`、`test_tools_external.py`、`test_tools_lifecycle.py`、`test_tools_cli.py`、`test_tools_mcp.py`、`test_tools_account.py`、`test_external_research.py`、`test_external_review.py`、`test_external_scoring.py` |
| 公共数据与计算合同（`core/`） | `core/` |
| 页面证据与只读检索 | `test_web_reading.py`、`test_reading_discovery.py`、`test_content_search.py` |
| 数据库迁移、治理基底与历史隔离 | `test_db.py`、`test_data_foundation.py`、`test_diagnostic_isolation.py`、`test_call_log.py`（真实探索轮次的增量与资料块还原、巡检互变） |
| TuShare 缓存与任务恢复 | `test_tushare_cache.py`、`test_market_boundary.py`、`test_worker_recovery.py`、`test_material_cache.py`（真实财报响应、SQLite 恢复事务和材料快照；`test_integrity.py` 核对 PDF 文本缓存复用与重建） |
| 模型运行时与运行上限保护 | `test_model_preflight.py`、`test_model_complete.py`、`test_limits.py`（在 worker 环境下运行，验证 Node 预检、单次推理的错误、运行身份与分层预算上限；付费实测需 `PI_LIVE_TEST=1`） |
| API、会话、分层隔离与部署运维 | `test_service_http.py`、`test_service_delivery.py`、`test_layering.py`、`test_api_keys.py`、`test_login_token.py`、`test_integrity.py`、`test_deploy.py`、`test_ops_hermes.py`、`test_web_connection.py` |

完整离线回归：`nice -n 15 uv run pytest -x -q tests`。仅在确有跨模块影响时使用；这不是模型评估集，也不代替真实浏览器验收。模型或全局成文/复核契约变化的评估要求见 [AGENTS.md](../AGENTS.md)。已有 `.venv` 但离线缓存缺失时，可用 `uv run --no-sync --offline pytest ...` 复用已安装依赖；这不验证锁文件安装。

## 实际 Wiki 方法卡

默认测试只保证包内 fixture 的回归行为。验收 Wiki 的方法卡改动时，显式指定实际目录；不设置变量时，这一跨库测试会明确跳过，不算实际卡已验证：

```bash
WIKI_METHOD_CARDS_TEST_DIR=/path/to/wiki/concepts/company-analysis \
  nice -n 15 uv run pytest -x -q -s tests/test_research_methods.py tests/test_judgment_contract.py
```

`test_actual_wiki_cards_when_explicitly_requested` 的当前验收版本固定为卡片目录最后一次改动的 Wiki 提交 `72d1d86bee689dff5b782cd6ae7a83ad89c5863c`（2026-11-16 起；此前为 `e123b1a`），并要求该目录在工作树中无未提交修改，因此 Wiki 其他目录的提交不会让测试失败：五张 stable 卡，逐项核对标题、生命周期、完整来源列表、原文件哈希与关键词，并输出原文件哈希。不以运行时实际值生成预期；Wiki 升版须明确审查并更新版本/预期。此前三 stable、两 draft 的验收事实仍保留在[方法库文档](../docs/research-method-library.md)，不代表当前生命周期。

同一入口在临时数据库载入真实纠错样本，按 `method_catalog` 生效替换后两条历史方法加实际五卡，验证四组人工诊断的溢出稳定前置、Relations 不作明确优先依据、未溢出/纯历史/批次行为、返回记录去重及冻结哈希不变。默认单卡 fixture 的 draft 排除测试继续保留；不复制整套卡片正文入库、不回退真实卡状态来过测、不读取生产材料或调用研究。来源链接核对仅覆盖文本与读取器往返，不证明网络可达或已读原文。全文仍为大小写敏感子串 OR，已检查 `Dechow–Dichev` / `Dechow-Dichev` 两种写法；仅溢出时按现有关键词整项/完整标题前置，不声称解决通用同义词。空结果负例用已确认不存在的 `应收保理从未出现_9a7e`；`no matching research method` 因 `no` 命中关联链接中的 `normalization`，混合目录下不是空结果负例，此宽匹配局限本轮不修。

2026-11-15 最终验收实际使用上面两文件加 `tests/test_research_tools.py tests/test_web_reading.py`，其余命令选项为 `nice -n 15 uv run --no-sync --offline pytest -x -q`，显式目录 `/home/lin/wiki/concepts/company-analysis`：**62 passed / 0 failed / 0 skipped**。`ruff check a_share_research/research_tools.py a_share_research/contract.py tests/test_research_methods.py tests/test_judgment_contract.py` 通过；复用本次结果，不因 commit/push 再跑同代码。

## 清理边界

可删除可再生的 `__pycache__/`、`.pytest_cache/`、`.ruff_cache/` 等缓存；不删除 `data/` 下的真实样本，不把仓库根目录运行数据 `../data/`、`.env*` 或历史评估证据当作测试垃圾。相似测试若覆盖不同数据来源、缺失状态或回归故障，应保留，不按行数或文件数裁剪。重复准备步骤可以共享，但异常测试须证明实际重查或拒绝，不能只凭相同返回值声称覆盖。
