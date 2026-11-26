# TuShare 生产主源 Provider API

从 `a_share_research.core.providers` 导入下列 Provider。结果统一为 `MarketDataResult`，包含值、状态、来源、抓取时间及可得的来源日期、请求指纹和行数。

## 凭据

TuShare 版本由 `pyproject.toml` 锁定。Token 按构造参数、`TUSHARE_TOKEN`、显式指定的 `env_path` 顺序读取。显式空 Token 禁用访问；缺少凭据时返回 `AUTH_MISSING`。

## 行情与行业

`TushareFundamentalsProvider.fetch_industry_map()` 返回六位公司代码到行业的映射。行业映射来自 `stock_basic`，没有官方业务日期，`source_as_of` 为空。缓存默认位于 `~/.cache/a_stock_lib/tushare_industry_map.json`，有效期 30 天，可用 `cache_path` 和 `ttl_seconds` 指定。缓存年龄按 `fetched_at` 计算。

`read_cached_industry_map()` 只读本地缓存。过期返回 `CACHE_STALE`，缺失、损坏、未来时间戳或读取失败返回 `CACHE_MISSING`。`fetch_industry_map()` 默认优先读有效缓存，缓存不可用时回源，`force_refresh=True` 时直接请求上游。获取成功但缓存写入失败时仍返回映射，状态为 `degraded`，原因为 `CACHE_WRITE_FAILED`。

## 行情价格合同

`TushareMarketDataProvider` 提供以下查询。结果中的 `source_as_of` 取实际最后观察日，`freshness_days` 相对请求截止日计算。股票和指数可分别用 `to_tushare_stock_code`、`to_tushare_index_code` 规范化代码。

接入共享历史行情缓存时，命中与回源都按下列接口的同一口径校验。命中不合格就按原键回源；只有通过校验的响应才写入，缓存保留 TuShare 原始列和单位。

- `fetch_index_bars(symbol, start_date, end_date)` 在闭区间内返回按日期升序排列的 `date/close` 点位，支持 SH、SZ、CSI（含 H00300.CSI 等全收益指数）。非法参数、缺列、非正或非有限点位、重复日期、区间外数据均返回失败。
- `fetch_daily_bars(code, start_date, end_date)` 返回闭区间内的不复权日线，停牌日无行。
- `fetch_suspended_days(code, start_date, end_date)` 仅返回 `suspend_d` 同时满足 `suspend_type=S`、`suspend_timing` 为空的全天停牌；日内暂停不是旧价补齐依据。
- `fetch_trading_calendar(start_date, end_date)` 读取 `trade_cal` 上交所开闭市日历并核对日期连续性；净值以日历核对基准缺口，不用缺失的指数序列反推交易日。
- `fetch_close_price(code, target_date)` 返回目标日及此前 10 个自然日内最近的有效收盘价。目标日命中为 `ok`，取较早交易日为 `degraded`，原因为 `NEAREST_AVAILABLE_PRICE`，无有效价格则失败。

## 估值

`TushareValuationProvider().fetch_valuation_history("603606", "2016-07-18", "2026-07-17")` 成功时结果的 `.value` 是 DataFrame，含 `ts_code`、`trade_date`、`close`、`pe`、`pe_ttm`、`pb`、`ps`、`ps_ttm`、`dv_ratio`、`dv_ttm`、`total_mv`、`circ_mv`。股息率单位为百分比，市值单位为万元；亏损公司的 PE 可为空。[TuShare 每日指标](https://tushare.pro/document/2?doc_id=32)

## 财务

`TushareFinancialProvider` 的 `fetch_indicator_history`、`fetch_income_history`、`fetch_balance_history`、`fetch_cashflow_history` 以公司代码查询指标、利润表、资产负债表和现金流量表。可选 `period`、`start_date` 和 `end_date` 均接受 YYYYMMDD 或 ISO 日期，发往 TuShare 时统一为 YYYYMMDD。

结果保留 `ts_code`、`endpoint`、`ann_date`、`f_ann_date`、`end_date`、`report_type`、`comp_type`、`end_type`、`update_flag`。财务指标上游缺少的实际公告日和报表分类字段留空。三大报表逐行优先取 `f_ann_date`，缺失时用 `ann_date`，最大有效公告日为 `source_as_of`；指标使用 `ann_date`。指标中的 `or_yoy`、`dt_netprofit_yoy` 分别为营业收入与扣非归母净利润同比增长率。[财务指标](https://tushare.pro/document/2?doc_id=79) · [利润表](https://tushare.pro/document/2?doc_id=33) · [资产负债表](https://tushare.pro/document/2?doc_id=36) · [现金流量表](https://tushare.pro/document/2?doc_id=44)

## 分红

`TushareDividendProvider().fetch_dividend_history("600036")` 查询每股分红，`cash_div` 为税后，`cash_div_tax` 为税前。生效资料日期优先用 `imp_ann_date`，缺失时取 `ann_date`。成功查询无记录时返回 `status="ok"` 和空 DataFrame。[TuShare 分红送股](https://tushare.pro/document/2?doc_id=103)

## 限流、重试和错误

Provider 默认共用进程级 180 次/分钟限流器。仅对超时或连接类异常重试一次。其他失败查看 `error_code` 和已移除已知 Token 的 `error_message`。常见错误包括权限或限频（`PERMISSION_DENIED`、`RATE_LIMITED`）、传输失败（`TIMEOUT`、`REMOTE_DISCONNECTED`）、输入或来源数据错误（`INVALID_ARGUMENT`、`EMPTY_RESPONSE`、`MISSING_COLUMNS`、`SCHEMA_CHANGED`、`SOURCE_STALE`）。[结果类型与错误码](market_data.py)

## 测试边界

Provider 的离线用例位于 `tests/core/`，该目录的 fixture 隔离行情、Telegram 与代理环境变量，测试注入客户端。其他验证入口见[测试说明](../../tests/README.md)。
