# 测试目录

## 组织

`test_*.py` 按能力组织。`data/` 保存真实采集的财务、行情、报告、公告等回归输入。`support/` 提供共享构造器和装载器，覆盖报告、立场、接续与跨页数字引文核验（`reports.py`、`stance.py`、`continuation.py`、`delivery.py`、`split_numbers.py`），财务、估值、行情、初筛与扫描（`financials.py`、`valuation.py`、`market_replay.py`、`screening.py`、`scans.py`），账户与持仓（`accounts.py`、`positions.py`、`account_analysis.py`），政策与监控评估（`policy.py`、`policy_replay.py`、`evaluations.py`），完整性与备份（`backup.py`），以及 Web、MCP、Hermes 与模板渲染（`web.py`、`mcp.py`、`hermes.py`、`templates.py`）。修改用例输入前先复制公共样本。

`conftest.py` 隔离数据与缓存，提供本地 Web、HTTP 服务及 pi 环境 fixture。依赖真实 pi 的用例在缺少环境时跳过；付费模型用例另需 `PI_LIVE_TEST=1`。

## 验证范围

在仓库根目录，按受影响的入口选择文件。例如：

```bash
nice -n 15 uv run pytest -x -q tests/test_account_ledger.py
```

| 能力 | 代表入口 |
|---|---|
| 数据源、公告、行情与财务 | `core/`、`test_announcements.py`、`test_financial_series.py`、`test_price.py`、`test_market_boundary.py` |
| 研究工具、引文、成文与复核 | `test_research_tools.py`、`test_check.py`、`test_report.py`、`test_review_evidence.py`、`test_continuation.py` |
| 初筛、月批与判断记分 | `test_screening.py`、`test_monthly_batch.py`、`test_backtest.py`、`test_validation.py`、`test_judgment_contract.py`、`test_stability.py` |
| 账户、政策与监控 | `test_account_ledger.py`、`test_account_reconciliation.py`、`test_account_nav.py`、`test_policy_ledger_monitor.py`、`policy_v0/` |
| Web、MCP 与客户端 | `test_web_reading.py`、`test_web_account_forms.py`、`test_web_oauth.py`、`test_tools_remote_mcp.py`、`test_tools_cli.py` |
| 数据库、完整性、发布与宿主 | `test_db.py`、`test_integrity.py`、`test_deploy.py`、`test_ops_hermes.py` |

`test_policy_samples.py` 重放真实政策评估，只在改动 `a_share_research/policy/` 时运行；样本含真实持仓，存放在私有证据目录 `~/.local/share/a-share-research-evaluations/policy-samples/`（可用 `POLICY_SAMPLES` 指定），不进仓库。

需要跨模块离线回归时运行 `nice -n 15 uv run pytest -x -q tests`。已有项目环境可用 `uv run --no-sync --offline pytest ...`。`agent-runner` 的 TypeScript 测试用 `node --test 'agent-runner/tests/*.test.ts'` 运行。各类变更的真实样本、浏览器与模型验证要求见[验证与交付](../AGENTS.md#验证与交付)。

## 清理边界

可清理 `__pycache__/`、`.pytest_cache/`、`.ruff_cache/`。保留 `tests/data/` 的真实样本、运行数据库、环境文件与仓库外评估证据。
