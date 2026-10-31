# 测试目录

## 组织

- `test_*.py`：按业务能力保存回归断言，不按某次交付或版本另建重复测试。
- `data/`：真实采集的 JSON（含 `.json.gz`）/PDF 样本，属于回归输入，不是临时文件。采集来源与口径见样本内元数据及使用它的测试。
- `support/reports.py`：茅台年报摘录、财务数字和引文检查用报告构造器。
- `support/continuation.py`：真实生产/K2 报告记录及 SQLite 装载器。
- `support/templates.py`：统一开启自动转义的 Jinja 环境与报告渲染入口。

通用支撑不再从 `test_check.py`、`test_continuation.py` 导入；其他领域专用采集数据/fixture 暂留原测试模块，不为整理目录搬动业务代码。报告构造器只用于检查边界，不能充当真实模型验收结果。不要修改公共样本对象；需要改变输入时复制后再处理。

## 验证范围

从仓库根目录运行 `uv run pytest -q tests/test_相关能力.py`。选择受影响的真实案例及正常案例；同一版本已通过的结果复用，不因提交或部署再跑一轮。

常见入口：

| 能力 | 测试 |
|---|---|
| 公告标题与报告日期 | `test_announcements.py`、`test_report_dates.py`、`test_mdna_pages.py` |
| 申万行业与同业快照 | `test_industry_peers.py`、`test_peer_snapshots.py` |
| 财务序列与报表核验 | `test_financial_series.py`、`test_financial_table.py` |
| 价格参考与技术纪律 | `test_price.py` |
| 图表与可视化几何、投资卡片 | `test_charts.py`、`test_cards.py` |
| 引文、接续报告与首页事项 | `test_check.py`、`test_continuation.py`、`test_attention.py`、`test_feedback_retry.py`、`test_research_methods.py`、`test_home_staleness.py` |
| 精确计算与研究工具 | `test_research_tools.py`、`test_research_repair.py`（真实披露与离线协议输入验证共享补证、额度、来源冲突及跨材料账本，不代表模型语义验收） |
| 候选初筛与批次管理 | `test_screening.py`、`test_screening_entry.py`、`test_screening_preflight.py` |
| 历史回测与离线实验 | `test_backtest.py`、`test_backtest_experiment.py` |
| 公司研究进展 | `test_progress.py`：真实捕获的引用、重复公告反例、幂等、HTTP 与隔离导入；人工绑定的结构互变样本不代表真实模型验收 |
| 页面证据与只读检索 | `test_web_reading.py`、`test_reading_discovery.py` |
| 数据库迁移与调用日志存储 | `test_db.py`、`test_call_log.py`（真实探索轮次的增量与资料块还原、历史压缩、巡检互变） |
| 模型运行时与推理守护 | `test_model_preflight.py`、`test_model_daemon.py`（在 worker 环境下运行，验证 Node 预检、Unix socket 守护进程管理、请求隔离与单次回退） |
| API、会话与部署运维 | `test_service_http.py`、`test_service_delivery.py`、`test_api_keys.py`、`test_login_token.py`、`test_integrity.py`、`test_deploy.py` |

完整离线回归：`uv run pytest -q tests`。仅在确有跨模块影响时使用；这不是模型评估集，也不代替真实浏览器验收。模型或全局成文/复核契约变化的评估要求见 [AGENTS.md](../AGENTS.md)。

## 清理边界

可删除可再生的 `__pycache__/`、`.pytest_cache/` 等缓存；不删除 `data/` 下的真实样本，不把仓库根目录运行数据 `../data/`、`.env*` 或历史评估证据当作测试垃圾。相似测试若覆盖不同数据来源、缺失状态或回归故障，应保留，不按行数或文件数裁剪。
