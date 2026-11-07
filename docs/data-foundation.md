# 数据层治理方案（Plan B 彻底版）

更新：2026-11-07。取代 `company-key-schema-migration.md`（原文档的 4 处错误见第 8 节）。本文维护设计、发布顺序与验收口径；状态与部署归[实施计划](implementation-plan.md)。

## 1 目标与归类

一次把数据底层理顺，让[北极星](judgment-validation.md#2-北极星与工程纪律)依赖的链路“候选 → 判断样本 → 记分 → 交易归因”有唯一、可信的数据基础：

| 环节 | 现在的问题 | 治理后 |
|---|---|---|
| 发现候选 | 批次与任务靠 `request_id` 字符串关联，四种拼接变体（`service.py:289-292`） | `runs.batch_id` |
| 形成判断 | 同一公司在各表形态不同，靠 `substr` 匹配 | 五张表统一 `company_key` |
| 记分 | “正式研究”条件散落 14 处，靠人工保持一致 | 样本条件收敛到一处定义，差异显式保留 |
| 复盘归因 | Skills 的 `holding_events.report_id` 指向 `reports.id`，删除最新报告后 id 会复用 | `reports`、`runs` 的 id 永不复用 |

**归类**：维护与基础设施。它让记分口径不漂移、交易归因不串号，但不直接改善任何判断，不计为投资进展，也不声称提高收益。

## 2 现状证据

生产库 v8（2026-11-07）只读盘点，以及发布备份副本上的实测：

| 项目 | 证据 |
|---|---|
| 标识形态混杂 | `reports`、`prices` 全为 9 位；`scan_events`、`subscriptions` 全为 6 位；`runs` 157 条 9 位、3 条 6 位 |
| 大字段拖慢元数据读取 | `runs.material` 共 92 MB、单条最大 2.1 MB，排在 `request`、`replay_of` 之前；`reports.body` 排在 `created_at`、`coverage` 之前；`calls.prompt` 同理。读这些排在后面的小列要走完溢出页链，成本随数据量线性增长 |
| 关系藏在 JSON | SQL 中 `$.parent_report_id` 8 处、`$.feedback.*` 9 处；`body` 里有而 `request` 里没有父报告的历史行为 0 |
| id 可复用 | `runs`、`reports` 没有 `AUTOINCREMENT`，已有删除（`reports` 最大 id 147，共 141 行） |
| 数据本身干净 | 五张表代码全部合规；`reports`、`prices` 与所属任务的代码 0 不一致；外键 0 违规；`request` 无非法 JSON |

## 3 目标模型

### 3.1 公司标识

`company_key` 为 6 位纯数字字符串，是虚拟生成列，加在 `runs`、`reports`、`prices`、`scan_events`、`subscriptions` 上，`NOT NULL`：

```sql
CASE WHEN length(code) IN (6, 9)
      AND substr(code, 1, 6) NOT GLOB '*[^0-9]*'
      AND (length(code) = 6 OR substr(code, 7) IN ('.SH', '.SZ', '.BJ'))
     THEN substr(code, 1, 6) END
```

- `code` 原样保留，只作存证与显示，不再用于判断是否同一公司，也不回写统一。
- `ts_code` 只在数据源适配层由 `stock_codes.infer_ts_code` 推导。
- 查询函数接受 6 位或 9 位入参，进入 SQL 前必须归一化：`company_key = '601899.SH'` 会静默查空。

### 3.2 物理布局

重建 `runs`、`reports`、`calls`，把大字段放到最后。列名和含义不变，旧代码按列名读写不受影响：

| 表 | 大字段顺序（末尾） |
|---|---|
| `runs` | `request`、`identity`、`material` |
| `reports` | `body` |
| `calls` | `response`、`prompt` |

这里选择调整列序而不是拆成 `run_materials`、`report_bodies`：不用改任何 JOIN，旧镜像也能照常运行，效果已实测（第 6 节）。

### 3.3 关系显式化

`runs` 新增三个虚拟生成列：

| 列 | 定义 | 取代 |
|---|---|---|
| `parent_report_id` | `json_extract(request, '$.parent_report_id')`，建部分索引 | 8 处 JSON 查询 |
| `is_feedback` | `json_extract(request, '$.feedback') IS NOT NULL` | `$.feedback` 判空 |
| `batch_id` | `CASE WHEN request_id GLOB 'batch-[0-9]*-*' THEN CAST(substr(request_id, 7, instr(substr(request_id, 7), '-') - 1) AS INTEGER) END`，建部分索引 | `LIKE 'batch-%'` 与四变体拼接 |

`batch_id` 已对生产数据只读验证：批次 1、2 各 19 条，其余 122 条为空，`batchB-refresh-*` 不会误匹配。

### 3.4 稳定 id

`runs`、`reports` 改为 `AUTOINCREMENT`，序列从现有最大 id 继续（实测 176、147）。

### 3.5 记分样本口径

沿用 `db.COVERAGE` 的做法，在 `db.py` 定义 SQL 片段常量，不建视图：

- `RESEARCH_ORIGINAL`：`u.kind = 'research' AND u.replay_of IS NULL AND NOT u.is_feedback`
- `FORMAL_REPORT`：`RESEARCH_ORIGINAL AND r.passed = 1 AND u.status = 'done'`

现有 14 处条件并不完全相同，能切换的切换，确有差异的就地保留并写明原因。**冻结记分口径不因此改变**，验收以样本 id 差分为空为准。

记分侧有两处定义与其他地方不同，当前数据上结果一致，本次都不改：

- **纠错**：`validation.source()` 在 Python 里按 `request.get("feedback")` 排除，别处用 SQL 判空或 `json_type = 'object'`。生产上有纠错的只有 2 行，都是 `object`。
- **批次**：`validation.source()` 按 `question == screening.QUESTION` 判定，页面和 `batch_id` 按 `request_id` 判定。生产上 38 份批次报告两种判法完全一致。

`source()` 属于已冻结的记分规则，保持原样；R2 差分时核对一次两种判法仍然一致。要统一只能走新记分版本。

## 4 迁移 v9

```python
def _migrate_v9(conn: sqlite3.Connection) -> None:
    # Payload columns stay last so metadata reads never walk their overflow pages.
    for table, columns, payload in REBUILT:
        existing = list(conn.execute(f"PRAGMA table_xinfo({table})"))
        if existing[-1]["name"] == payload:  # already rebuilt; a re-run must be a no-op
            continue
        names = ",".join(row["name"] for row in existing)
        attached = [row["sql"] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger')"
            " AND sql IS NOT NULL", (table,))]
        conn.execute(f"CREATE TABLE {table}_v9 ({columns})")
        conn.execute(f"INSERT INTO {table}_v9 ({names}) SELECT {names} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_v9 RENAME TO {table}")
        for sql in attached:
            conn.execute(sql)
    for table in ("prices", "scan_events", "subscriptions"):
        if "company_key" not in {row["name"] for row in conn.execute(f"PRAGMA table_xinfo({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {COMPANY_KEY}")
    for table in ("runs", "reports", "prices", "scan_events"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_by_company_id ON {table}(company_key, id)")
    # plus the partial indexes on runs.parent_report_id and runs.batch_id
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("foreign key violations after rebuild")
```

要点：

- **外键开关**：表重建不能在外键开启时进行，而事务内改不了这个开关。`connect()` 把 `PRAGMA foreign_keys=ON` 移到迁移循环之后，迁移末尾用 `foreign_key_check` 兜底。
- **`SCHEMA` 不动**：新列和索引只放迁移，否则存量库启动时会报 `no such column`。
- **列检测用 `table_xinfo`**：`table_info` 看不到生成列。
- **生成列写在大字段之前**：虚拟列不占存储，这样每张表最后一列都是大字段，可作为重入判断。
- **WAL**：迁移产生约 268 MB WAL。`connect()` 在本次确实执行过迁移时，跑一次 `wal_checkpoint(TRUNCATE)`。
- **不跑 `VACUUM`**：文件增长约 93 MB，都是可复用空闲页，磁盘余量 24 GB，不值得多一步生产操作。
- **`integrity.py` 必须兼容 v8**：发布工具用新代码巡检迁移前的备份，新增检查要先用 `table_xinfo` 判断列是否存在。

## 5 发布与工作分解

日期固定。11-29 起月批保护期不发布；到 11-28 仍未发布的部分顺延到 12-05，并在实施计划公开标记。K7b（11-20）优先级不变，与之冲突时先合 K7b。

### P0 清理基线（11-07，例行）

- 修复 HEAD 上已失败的测试：`test_attention::test_failed_report_hides_judgment_but_keeps_evidence_gaps`、`test_review_evidence::test_interrupted_repair_keeps_unresolved_opinion_and_safe_stage_in_api_and_page`。原因是直接渲染模板时没传 `retry`；生产路径 `web.py:399,468` 都传了，属测试缺陷。
- `deploy.idle` 增加进行中的 `research_progress` 检查（已知缺口）。

### R1 结构迁移（11-08 20:00，**需所有者当次授权**）

只改结构，业务查询不动，因此行为应完全不变。空闲检查不通过就中止，11-09 20:00 再试一次。

- `db.py`：`_migrate_v9`、`connect()` 调整。
- `integrity.py`：增加 `reports` 与所属任务 `company_key` 一致性检查（列不存在时跳过）。
- 测试：
  - `test_db.py:43,173,176`、`test_batch_c.py:59`、`test_scans.py:216,222` 更新版本号；
  - `test_feedback_retry.py:39` 回插前去掉生成列；
  - `test_scans.py:217` 重写：列序变化后 `iterdump` 的 `INSERT` 行不再可比，改为按列比较行数据。
- `README.md` 部署节与 `deploy.py:180` 注释：现行规则是“非加法迁移不走发布工具”，而表重建不算加法。工具的应用回退实际只依赖旧镜像能跑在新结构上（已验证），所以把准入条件改为这一条。**这是规则变更，随 R1 一并由所有者授权**；不同意则另写一次性迁移步骤。

### R2 查询切换（11-11，例行）

| 位置 | 改动 |
|---|---|
| `reading.py` | `latest_reports`（`codes`、`code_prefix`、`GROUP BY`）、研究库搜索、`:467`、`latest_price`、`api_reports`、`stock_reports`、`:249`、`:741`、`:805-806`，以及 `$.parent_report_id`、`LIKE 'batch-%'` |
| `service.py` | `company_card`、`submit_price`、`:289-292` 改 `batch_id` + `company_key` |
| `scans.py`、`hypotheses.py`、`validation.py`、`evolution.py`、`research.py`、`screening.py`、`zones.py` | `company_key` 与第 3.5 节常量 |
| 全部 `SELECT *` / `r.*`（`runs`、`reports` 共 9 处） | 改为显式列，既不把新列带进 API，也不再无谓读取大字段 |

### R3 代码层收口（11-13，例行）

- 约 35 处公司代码的 `[:6]`：有行数据的直接用 `company_key`，入参用 `stock_codes.normalize_key`。`backtest.py`、`industry.py` 的 `d[:6]` 是日期，不动。
- 7 处重复的代码正则（`service.py:26,79,226`、`web.py:587`、`reading.py:81,370`、`research_tools.py:347`）统一到 `stock_codes`，错误文案与状态码不变。
- 新增 `tests/test_data_foundation.py` 钉住不变量：
  - 五张表都有 `company_key`，三张重建表最后一列是大字段；
  - 五张表都拒绝非法代码；
  - 删除最新报告后 id 不复用；
  - 6 位和 9 位入参结果相同；
  - 源码扫描：SQL 中无 `substr(...code`，无 `LIKE 'batch-%'`。
- `AGENTS.md` 工程约束增加第 7 节规则的一行摘要。

## 6 已验证

全部在发布备份的私有副本上完成，未触碰生产库：

| 项目 | 结果 |
|---|---|
| 迁移耗时 | 0.93 s（远低于发布工具 30 s 就绪等待和 SQLite 锁等待） |
| 迁移后状态 | `integrity_check` ok，外键 0 违规，4 个触发器保留，序列正确 |
| 重入 / 新库 | 重入 0.00 s 无报错；新库直接到 v9 |
| 旧代码跑在新结构上 | 读写、触发器正常，完整性巡检 0 问题（1323 次调用、141 份报告、348 个 PDF） |
| 非法代码 | `603606.SS`、`60360`、全角数字、`603606.sh`、尾随空格全部拒绝 |
| 现有测试 | 48 个接触数据库的测试文件（每个 `-x`）：45 个通过；2 个是 HEAD 上已有的失败（见 P0）；1 个是 `test_scans.py:217`（见 R1） |

同一份旧代码在重建前后的耗时（141 份报告、160 个任务）：

| 操作 | 之前 | 之后 |
|---|---|---|
| `pending_reviews` | 16.98 ms | 0.12 ms |
| 任务元数据 + `request` 全表 | 23.01 ms | 0.29 ms |
| 报告元数据全表 | 7.54 ms | 0.17 ms |
| 调用元数据全表 | 16.69 ms | 3.64 ms |
| `latest_reports` | 53.46 ms | 33.58 ms |

绝对值目前不大。价值在于这类查询不再随大字段总量线性变慢。`latest_reports` 剩余耗时主要来自读 `body` 里的检查点，本次不处理。

**尚未验证**：第 3.3 节三列中的 `batch_id` 未进入原型（只验证了表达式）；生成列放在大字段之前的最终 DDL；R2、R3 的查询改动；未被上述 48 个文件覆盖的测试。这些在实施时验证。

## 7 验收与治理规则

**主验收：新旧差分。** R2、R3 各跑一次：同一数据库副本，新旧代码分别输出下列结果，除新增列外必须完全一致。

- 41 家公司各自用 6 位和 9 位入参调用 `latest_reports`、`stock_reports`、`latest_price`、`api_reports`、`company_card`；
- 首页、研究库搜索、`pending_reviews`、批次视图的查询结果；
- 研究 API 的 JSON 响应（Skills 依赖，字段不得增减）；
- `validation.py`、`hypotheses.py` 选出的样本 id（不调用 TuShare，只比样本）。

差分脚本与结果放 `~/.local/share/a-share-research-evaluations/`，不进仓库。

**其他验证**：

- 每个 R：接触数据库的测试文件逐个 `-x`；Python 编译与 `git diff --check`。不无差别跑全量。
- R3：互变验证，去掉归一化后不变量测试必须失败。
- R2 后：在 8610 开发实例用手机尺寸走一遍首页、`/stock/603606`、报告页、研究库搜索。

**回退**：

- 应用问题：两服务 `docker tag a-share-research-{web,worker}:rollback-<提交> a-share-research-{web,worker}:latest`，再 `docker compose up -d --no-build --no-deps web worker`。数据库不动，也不改 `user_version`（`connect()` 不拒绝更高版本，旧代码已验证可运行）。
- 数据问题：停服后从 `backup-pre-<提交>-<时间>.db` 恢复，会丢失备份之后的写入，须所有者决定。

**长期规则**（R3 后生效）：

1. 带公司代码的新表必须有 `company_key`；跨表比较只用它。
2. 大字段放表尾。向这些表 `ALTER` 追加的列会排在大字段之后，读取成本高：高频读的列要配索引，否则重建。
3. 表间关系用列表达，不新增靠 JSON 路径或字符串前缀的关联。
4. 记分样本条件用第 3.5 节常量；已冻结的口径不改写，新口径走新版本。
5. 迁移必须可重入，并验证旧镜像能跑在新结构上；做不到的要单独设计回退。

## 8 不做及理由

| 项目 | 理由 |
|---|---|
| 改写报告、预测、快照中的 JSON，统一存量 `code` | 已冻结且有哈希校验（AGENTS 规定不事后改写） |
| 重命名 `code` 列 | 旧镜像会失效，失去只换镜像的回退能力 |
| 拆出大字段表 | 调整列序已解决读取问题；将来要归档 `material` 时再拆 |
| 报告与材料去重 | `report.table` 5848 行与材料 100% 相同，共 12.6 MB，约占库 6%；但受 `body_sha256` 约束，不值得 |
| `companies` 维度表 | 同一公司名称无冲突，报告里的名称本就是历史快照 |
| 记分结果落表 | V0 明确不建表；成熟读数归 K7d |
| `replay_of`、`coverage` 触发器改生成列 | 运行正常；`coverage` 含子查询，生成列不支持 |
| Skills 账本改造 | 归 `a-stock-agent-skills`，消费者不直连 SQLite；本方案只保证 id 稳定、API 不变 |

**原文档已修正的错误**：

1. `company_key IN (...)` 没有归一化，`web.py:313,446`、`reading.py:476` 传 9 位时会静默查空。
2. 回滚命令 `docker compose down && up -d` 重启的仍是新镜像；`PRAGMA user_version = 8` 不需要。
3. “不再依赖 `substr`”在只改两张表时做不到，现改为五张表全覆盖。
4. 测试数量（实际 1241，289 是相关子集）、备份文件名、`test_scans:217` 的描述和 `< 0.1s` 门槛有误。
