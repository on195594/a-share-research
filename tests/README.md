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
| 引文、复核账本、接续报告与公司法定事项 | `test_check.py`、`test_review_evidence.py`、`test_open_questions.py`、`test_continuation.py`、`test_attention.py`、`test_feedback_retry.py`、`test_research_methods.py`、`test_companies.py` |
| 精确计算与研究工具 | `test_research_tools.py`、`test_calculation_assumptions.py`、`test_valuation.py`、`test_research_repair.py`、`test_repair.py`（真实披露与离线协议输入验证假设链传播、长期补充回报、分情景引用、共享补证、修订调用储备、来源冲突及跨材料账本；不代表真实模型语义验收或研究通过率） |
| 候选初筛与批次管理 | `test_screening.py`、`test_screening_entry.py`、`test_screening_preflight.py`、`test_batch_c.py`、`test_monthly_batch.py` |
| 研究改进与方法实验 | `test_evolution.py`（冻结来源、实验判据，以及空闲发现缓存的自身／外部写入、旧哈希、事务回滚、异常与版本失效） |
| 历史回测 | `test_backtest.py` |
| 判断记分、投资立场与估值诊断（V0–V3、V5、A1、A3、A4） | `test_validation.py`、`test_agenda.py`、`test_stance.py`、`test_hypotheses.py`、`test_valuation.py`、`test_valuation_ui.py`、`test_zones.py`、`test_scans.py`、`test_advisor_sources.py`、`test_r2_queries.py` |
| 页面证据与只读检索 | `test_web_reading.py`、`test_reading_discovery.py`、`test_content_search.py` |
| 数据库迁移与调用日志存储 | `test_db.py`、`test_call_log.py`（真实探索轮次的增量与资料块还原、巡检互变） |
| TuShare 缓存与任务恢复 | `test_tushare_cache.py`、`test_worker_recovery.py`、`test_material_cache.py`（真实财报响应、SQLite 恢复事务和材料快照；`test_integrity.py` 核对 PDF 文本缓存复用与重建） |
| 模型运行时 | `test_model_preflight.py`、`test_model_complete.py`（在 worker 环境下运行，验证 Node 预检、单次推理的错误与运行身份；付费实测需 `PI_LIVE_TEST=1`） |
| API、会话与部署运维 | `test_service_http.py`、`test_service_delivery.py`、`test_api_keys.py`、`test_login_token.py`、`test_integrity.py`、`test_deploy.py`、`test_web_connection.py` |

完整离线回归：`nice -n 15 uv run pytest -x -q tests`。仅在确有跨模块影响时使用；这不是模型评估集，也不代替真实浏览器验收。模型或全局成文/复核契约变化的评估要求见 [AGENTS.md](../AGENTS.md)。已有 `.venv` 但离线缓存缺失时，可用 `uv run --no-sync --offline pytest ...` 复用已安装依赖；这不验证锁文件安装。

## 清理边界

可删除可再生的 `__pycache__/`、`.pytest_cache/` 等缓存；不删除 `data/` 下的真实样本，不把仓库根目录运行数据 `../data/`、`.env*` 或历史评估证据当作测试垃圾。相似测试若覆盖不同数据来源、缺失状态或回归故障，应保留，不按行数或文件数裁剪。
