# TuShare 生产主源 Provider API

> 本文件随 a-stock-lib 0.10.2（`7bda9cd`）并入 `a_share_research/core/`，仍是 Provider 的现行合同；下文“安装”与发版相关段落描述并入前的 wheel 流程，已不适用。

本文描述当前源码合同；源码版本以 `pyproject.toml` 为准。生产消费者的已发布版本以各自锁文件和部署记录为准，源码修复不代表已发布或激活。

## 安装

本地候选验证应安装本提交构建并校验过哈希的 wheel；生产安装必须使用已发布的不可变 Release wheel 及固定 SHA-256，流程见 [发版清单](RELEASE_CHECKLIST.md)。

本版本固定 `tushare==1.4.29`。Token 优先级：构造参数 → `TUSHARE_TOKEN` → 显式 `env_path` 指向的文件。默认不搜索相邻项目或主目录下的 `.env`；未配置时返回 `AUTH_MISSING`，显式空 Token 仍表示禁用。旧调用若依赖隐式文件发现，须改为环境变量或显式路径。禁止在代码中硬编码 Token。

## 行情与行业

`TushareMarketDataProvider` 与其他 TuShare Provider 共用 `TushareProviderBase` 的 token、client、限流、typed retry、错误分类和请求指纹。成功行情以实际最大交易日作为 `source_as_of`；标量价格完整继承 bars 的 `source_as_of`、`freshness_days`、`request_fingerprint` 和 `row_count`。

`TushareFundamentalsProvider.fetch_industry_map()` 的远端与缓存命中结果都返回缓存年龄、确定性请求指纹和映射行数。行业接口没有官方业务日期，因此 `source_as_of` 保持 `None`；抓取时点只由 `fetched_at` 表示，不伪装成财报期或交易日。

- 缓存默认位于 `~/.cache/a_stock_lib/tushare_industry_map.json`，TTL 30 天，可由 `cache_path` / `ttl_seconds` 注入；写入为临时文件 + fsync + 原子替换。
- `read_cached_industry_map()` 只读缓存、不联网、不写入；过期返回 `CACHE_STALE`，缺失、损坏、结构错误、未来时间戳或读失败统一返回 `CACHE_MISSING`，状态均为 `failed`。缓存年龄以 `fetched_at` 为准；旧缓存仍可读取，新写入不再保存重复的 `fetched_at_epoch`。
- `fetch_industry_map()` 缓存不可用时回源；远端成功但写缓存失败返回 `degraded` + `CACHE_WRITE_FAILED`，数据仍可用。

## 实时行情时效校验

`validate_quote_observation(observation, now, trading_dates)` 是无 I/O 纯函数：校验 `QuoteObservation` 的正有限价格、来源与日期时间，并按上海时区判定交易时段。`now` 必须带时区；盘中要求报价为当日且年龄不超过 `max_intraday_age_seconds`（默认 120 秒，容忍 30 秒时钟超前），盘前要求上一交易日，非交易时段要求最近交易日。合法返回 `None`，否则返回原因字符串。交易日历由调用方提供（例如 `fetch_trade_calendar`）。

## 行情价格合同

- `fetch_index_bars(symbol, start_date, end_date)`：闭区间请求，支持 `.SH` / `.SZ` / `.CSI`（包括 `H00300.CSI`、`H00922.CSI`）；只返回升序的 `date/close` 指数点位，不捏造 OHLC、成交量或复权含义。非法代码/日期返回 `INVALID_ARGUMENT`，缺列、非正/非有限点位、重复日期、区间外数据均 fail-closed。`source_as_of` 是实际最后观察日，`freshness_days` 相对请求截止日。
- `fetch_close_price(code, target_date)`：查询 `[target_date - 10 天, target_date]`，返回最近有效收盘价。当天为 `ok`，之前交易日为 `degraded` + `NEAREST_AVAILABLE_PRICE`；保留实际日期、天数、来源、请求指纹。长停牌超过窗口或无行情返回失败，不无限回溯。
- `fetch_trade_calendar` 返回 SSE 交易日期表，不将计划交易日标作行情观察日。
- `to_tushare_stock_code` / `to_tushare_index_code` 是公开的代码规范化函数。

### 接口变动记录

- **0.10.x**：稳定公共表面以 `a_share_research.core.providers.__all__` 与 `a_share_research.core.market_data.__all__` 为准，包含 `py.typed`；`MarketDataResult.fetched_at` 统一为带 `+08:00` 的上海时间 ISO 字符串；移除无消费者的 `fetch_daily_bars_range` 与 `MarketSession` 顶层导出。
- **0.9.0**：移除旧 Tracker 遗留 `MarketDataProvider`、`fetch_l3_bars` 与十年月度分位算法；`fetch_score_price` / `fetch_outcome_price` 合并为 `fetch_close_price`；`fetch_index_bars` 要求显式起止日期并支持全收益指数。详细过程见 [CHANGELOG.md](../CHANGELOG.md)。

## 估值

```python
from a_share_research.core.providers import TushareValuationProvider

provider = TushareValuationProvider()
history = provider.fetch_valuation_history(
    "603606",
    "2016-07-18",
    "2026-07-17",
)
```

返回 `MarketDataResult[pandas.DataFrame]`，字段包括：

```text
ts_code, trade_date, close,
pe, pe_ttm, pb, ps, ps_ttm,
dv_ratio, dv_ttm, total_mv, circ_mv
```

单位与空值遵循官方 `daily_basic`：

- `dv_ratio/dv_ttm`：百分比；
- `total_mv/circ_mv`：万元；
- 亏损公司的 `pe/pe_ttm` 为空是合法业务状态。

官方文档：[每日指标数据](https://tushare.pro/document/2?doc_id=32)。

## 财务

```python
from a_share_research.core.providers import TushareFinancialProvider

provider = TushareFinancialProvider()
indicator = provider.fetch_indicator_history("600036")
income = provider.fetch_income_history("600036")
balance = provider.fetch_balance_history("600036")
cashflow = provider.fetch_cashflow_history("600036")
```

四个历史方法的可选 `period` 参数遵循 TuShare 官方报告期格式 `YYYYMMDD`；`start_date/end_date` 也接受 ISO 日期并在请求前转换为 `YYYYMMDD`。

统一输出增加 `endpoint`，并提供以下可空公共键：

```text
ts_code, endpoint, ann_date, f_ann_date, end_date,
report_type, comp_type, end_type, update_flag
```

`fina_indicator` 官方不提供 `f_ann_date/report_type/comp_type/end_type`，这些字段保持 `NULL`，不得伪造。三大报表保留官方 PIT 键和更正标识。

`fina_indicator` 的选择字段包含 `or_yoy`（营业收入同比增长率）与 `dt_netprofit_yoy`（扣除非经常损益后的归母净利润同比增长率），供消费者做最新报告方向核验；它们不是自动评分或单季年化字段。

财务结果的 `source_as_of` 使用有效公告日：三大报表优先 `f_ann_date`、回退 `ann_date`；`fina_indicator` 使用 `ann_date`。

官方文档：

- [财务指标](https://tushare.pro/document/2?doc_id=79)
- [利润表](https://tushare.pro/document/2?doc_id=33)
- [资产负债表](https://tushare.pro/document/2?doc_id=36)
- [现金流量表](https://tushare.pro/document/2?doc_id=44)

## 分红

```python
from a_share_research.core.providers import TushareDividendProvider

provider = TushareDividendProvider()
result = provider.fetch_dividend_history("600036")
```

无分红记录返回 `status="ok"` 的空 DataFrame，不作为远端失败。

分红结果的 `source_as_of` 优先使用 `imp_ann_date`，缺失时回退 `ann_date`。

官方字段口径：

- `cash_div`：每股分红，税后；
- `cash_div_tax`：每股分红，税前。

官方文档：[分红送股](https://tushare.pro/document/2?doc_id=103)。

## 送转

`a_share_research.core.fetcher_utils.detect_split_ratio(df, latest_report_year, *, as_of=None)` 计算报告期后、`as_of`（默认上海当日）及之前已实施送转的累计摊薄系数。需要可复现结果时请显式传 `as_of`。同时接受 AKShare `stock_fhps_detail_em` 列（每 10 股）与 `TushareDividendProvider` 原生列（`stk_bo_rate + stk_co_rate`，缺失时 `stk_div`，每股；`div_proc == "实施"`）。

## 限流、重试和错误

- 真实 TuShare Provider 默认共享进程级 180 次/分钟限流器；
- 仅 typed timeout/connection 异常重试一次；
- 权限、积分、认证、参数、schema 和频次错误不重试；
- SDK 1.4.29 抛出 API 错误时只保留消息，无法可靠取得服务端数字 code；
- 错误消息在进入 `MarketDataResult` 前移除已知 Token。

常用错误码：

```text
AUTH_MISSING
PERMISSION_DENIED
RATE_LIMITED
TIMEOUT
REMOTE_DISCONNECTED
EMPTY_RESPONSE
MISSING_COLUMNS
SCHEMA_CHANGED
INVALID_ARGUMENT
SOURCE_STALE
UNKNOWN_ERROR
```

行业缓存另有 `CACHE_*` 错误码，见“行情与行业”。

## 测试边界

默认 pytest 通过 autouse fixture 清除 TuShare、Telegram 和代理凭据。单元测试只注入 fake client，不访问网络。真实 smoke test必须单独显式运行，不属于默认测试套件。
