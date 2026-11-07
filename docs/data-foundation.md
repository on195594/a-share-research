# 数据层治理方案（Plan B 彻底版）

更新：2026-11-07（已按工程架构审查修订，见第 9 节）。取代 `company-key-schema-migration.md`（原文档的 4 处错误见第 8 节）。本文维护设计、发布顺序与验收口径；状态与部署归[实施计划](implementation-plan.md)。

## 1 目标与归类

一次把数据底层理顺，让[北极星](judgment-validation.md#2-北极星与工程纪律)依赖的链路“候选 → 判断样本 → 记分 → 交易归因”有唯一、可信的数据基础：

| 环节 | 现在的问题 | 治理后 |
|---|---|---|
| 发现候选 | 批次与任务靠 `request_id` 字符串关联，四种拼接变体（`service.py:289-292`） | `runs.batch_id` |
| 形成判断 | 同一公司在各表形态不同，靠 `substr` 匹配 | 五张表统一 `company_key` |
| 记分 | “正式研究”条件散落 14 处，靠人工保持一致 | 样本条件收敛到一处定义，差异显式保留 |
| 复盘归因 | Skills 的 `holding_events.report_id` 指向 `reports.id`，删除最新报告后 id 会复用 | 删除后 `reports`、`runs` 的 id 不复用；从备份恢复时按第 7 节保住序列 |

**归类**：维护与基础设施。它让记分口径不漂移、交易归因不串号，但不直接改善任何判断，不计为投资进展，也不声称提高收益。

## 2 现状证据

生产库 v8（2026-11-07）只读盘点，以及发布备份副本上的实测：

| 项目 | 证据 |
|---|---|
| 标识形态混杂 | `reports`、`prices` 全为 9 位；`scan_events`、`subscriptions` 全为 6 位；`runs` 157 条 9 位、3 条 6 位 |
| 大字段拖慢元数据读取 | `runs.material` 共 92 MB、单条最大 2.1 MB，排在 `request`、`replay_of` 之前；`reports.body` 排在 `created_at`、`coverage` 之前；`calls.prompt` 同理。读这些排在后面的小列要走完溢出页链，成本随数据量线性增长 |
| 关系藏在 JSON | SQL 中 `$.parent_report_id` 8 处、`$.feedback.*` 9 处；`body` 里有而 `request` 里没有父报告的历史行为 0 |
| id 可复用 | `runs`、`reports` 没有 `AUTOINCREMENT`，已有删除（`reports` 最大 id 147，共 141 行） |
| 代码与外键干净 | 五张表代码全部合规；`reports`、`prices` 与所属任务的代码 0 不一致；外键 0 违规；`request` 无非法 JSON |
| 父报告关系有历史断链 | 外键管不到 JSON 里的父报告：任务 16、66 指向已不存在的报告 1、45，两个任务都已完成，分别产生报告 10、49；存在的父报告与子任务公司 0 不一致 |
| 交易归因尚未受影响 | `holding_events` 37 条事件，`report_id` 全部为空（11-07 只读核对），现在建立序列没有串号存量 |

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
- 内部查询函数接受 6 位或 9 位入参，进入 SQL 前必须归一化：`company_key = '601899.SH'` 会静默查空。无法归一化时沿用 `stock_reports` 的写法返回空，不抛 500。
- HTTP 入参契约不变：`/api` 的公司报告与公司卡片仍只收 6 位（`web.py:587`、`service.py:226`）。
- 存储表达式只接受规范形式；`stock_codes.parse_code` 对首尾空白的容错只属于入参处理。写入前必须是规范代码，`NOT NULL` 只作最后防线，不作为用户输入校验。

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
| `batch_id` | 见下方表达式，建部分索引 | `LIKE 'batch-%'` 与四变体拼接 |

```sql
CASE WHEN request_id GLOB 'batch-[1-9]*-*'
      AND substr(request_id, 7, instr(substr(request_id, 7), '-') - 1) NOT GLOB '*[^0-9]*'
      AND substr(request_id, 7 + instr(substr(request_id, 7), '-')) IN (
            substr(code, 1, 6), substr(code, 1, 6) || '.SH',
            substr(code, 1, 6) || '.SZ', substr(code, 1, 6) || '.BJ')
     THEN CAST(substr(request_id, 7, instr(substr(request_id, 7), '-') - 1) AS INTEGER) END
```

外部 `request_id` 只限制长度（`service.py:27`），而批次归属决定月批调用上限（`service.py:296`）和中断后的处理（`worker.py:20`），所以表达式必须完整校验编号、分隔符和公司段。

`batch_id` 只表示编号形态上属于哪个批次。批次是否存在、是否月批，仍要 `JOIN batches`，生成列不能跨表。

与旧四变体关联的差分：生产 160 条任务相同（批次 1、2 各 19 条）；`batch-1oops-603606.SH`、公司错配、`batch-01-*`、`.SS` 后缀、尾部多余字符、`batch-1-` 都不归属；`batchB-refresh-*` 不匹配；批次不存在时 JOIN 后同样不归属。

`LIKE 'batch-%'` 切换后有一处有意变化：`batch-abc` 这类畸形外部编号不再算批次任务，按普通任务排队和计数。正常编号不受影响。

### 3.4 稳定 id

`runs`、`reports` 改为 `AUTOINCREMENT`，序列从现有最大 id 继续（实测 176、147）。

这一机制保证删除后不复用，不保证恢复旧备份后不复用：旧备份里的序列也是旧的，恢复后同一个 id 会再次发出。恢复时的处理见第 7 节“回退”。

### 3.4.1 关系完整性

外键和生成列都不校验 JSON 里的父报告，`integrity.py` 要显式检查：

| 关系 | 处理 |
|---|---|
| 父报告存在，但与子任务公司不同 | 算完整性问题，阻断发布（现有 0 条） |
| 父报告不存在 | 记入 `counts["dangling_parent_reports"]`，不阻断：手工删报告就会产生这种情况。R1 验收要求恰好是任务 16、66；此后数量变化要查明原因 |
| `batch_id` 指向不存在的批次 | 记入 `counts["orphan_batch_runs"]`，不阻断（现有 0 条） |

任务 16、66 保持原样，冻结 JSON 不改指向。读取侧已有处理：`reading.py:543` 返回 `baseline_status='missing'`。

### 3.5 记分样本口径

沿用 `db.COVERAGE` 的做法，在 `db.py` 定义 SQL 片段常量，不建视图：

- `RESEARCH_ORIGINAL`：`u.kind = 'research' AND u.replay_of IS NULL AND NOT u.is_feedback`
- `FORMAL_REPORT`：`RESEARCH_ORIGINAL AND r.passed = 1 AND u.status = 'done'`

现有 14 处条件并不相同，逐处归类如下：

| 处理 | 位置 | 说明 |
|---|---|---|
| 切到 `FORMAL_REPORT` | `service.py:232`、`hypotheses.py:126`、`reading.py:382`、`scans.py:301`、`research.py:364`、`evolution.py:51` | 时间窗、`material` 等附加条件保留。后两处原用 `json_type = 'object'` 判纠错，写入路径只产生 object，切换后结果相同 |
| 切到 `RESEARCH_ORIGINAL` | `reading.py:311` | 不要求通过和完成 |
| 保留，不切换 | `validation.py:190,233,256`、`zones.py:199` | **没有 `status = 'done'`**，纠错在 Python 里判断 |
| 保留，不切换 | `screening.py:228` | 按 `$.prior` 排除接续研究，SQL 不判纠错 |
| 保留，不切换 | `reading.py:250` | 查的是纠错任务本身 |
| 保留，不切换 | `reading.py:737` | 队列状态，不是样本口径 |

**冻结记分口径不因此改变。** 当前没有“已通过但未完成”的报告，所以把 `validation.py` 错换成 `FORMAL_REPORT` 也能通过样本 id 零差分。样本零差分因此不足以证明规则未变，第 7 节增加状态变体。

记分侧还有两处定义与其他地方不同，当前数据上结果一致，本次都不改：

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

只有先后顺序是硬依赖：R2、R3 要用 R1 加的列。三段分开发是为了便于定位和回退，不需要等待期，相邻两段间隔 1 天。

| 段 | 日期 | 条件 |
|---|---|---|
| P0 | 与 R1 同日完成 | 例行 |
| R1 | 所有者授权当天，目标 11-07 | 第 7 节 R1 门禁全过。门禁未过或空闲检查不通过就停止，第二天 20:00 再试，不绕过 |
| R2 | R1 次日，目标 11-09 | R1 后跑通一份真实研究；这只是补充证据，不替代差分 |
| R3 | R2 次日，目标 11-10 | R2 差分通过 |

R1 推迟时，R2、R3 同步后移。最晚 11-28；11-29 起月批保护期不发布，到时仍未发布的部分顺延到 12-05，并在实施计划公开标记。K7b（11-20）优先级不变，与之冲突时先合 K7b。

### P0 清理基线（例行）

修复 HEAD 上已失败的测试：`test_attention::test_failed_report_hides_judgment_but_keeps_evidence_gaps`、`test_review_evidence::test_interrupted_repair_keeps_unresolved_opinion_and_safe_stage_in_api_and_page`。原因是直接渲染模板时没传 `retry`；生产路径 `web.py:399,468` 都传了，属测试缺陷。

### R1 结构迁移（**需所有者当次授权**）

只改结构和直接相关的写入校验，业务查询不动。除下面的入参校验外，行为应完全不变。

- `db.py`：`_migrate_v9`、`connect()` 调整。
- `service.py:26,79`：`\d` 改为 `[0-9]`。Python 的 `\d` 接受全角数字，新约束下 `６０３６０６` 会从“能提交”变成裸 `IntegrityError`，必须改为返回既有的 `RequestError`。其余写入入口已核对：`scans.py:132` 已是 `[0-9]{6}`，`reports`、`prices`、`scan_events` 的代码来自已校验的任务或订阅。
- `integrity.py`：增加 `reports` 与所属任务 `company_key` 一致性检查（列不存在时跳过），以及第 3.4.1 节的关系检查。
- 测试：
  - `test_db.py:43,173,176`、`test_batch_c.py:59`、`test_scans.py:216,222` 更新版本号；
  - `test_feedback_retry.py:39` 回插前去掉生成列；
  - `test_scans.py:217` 重写：列序变化后 `iterdump` 的 `INSERT` 行不再可比，改为按列比较行数据；
  - 新增：全角代码提交返回 `RequestError`，生成列与第 3.3 节 `batch_id` 变体的约束测试。这些是 R1 的发布条件，不留到 R3。
- `README.md` 部署节与 `deploy.py:180` 注释：现行规则是“非加法迁移不走发布工具”，而表重建不算加法。工具的应用回退实际只依赖旧镜像能跑在新结构上，所以把准入条件改为这一条，并以最终 DDL 和实际回退镜像的读写证据为门槛。**这是规则变更，随 R1 一并由所有者授权**；不同意则另写一次性迁移步骤。

### R2 查询切换（例行）

每处查询切到 `company_key` 时，它的入参归一化要在同一次发布交付，不留到 R3。

| 位置 | 改动 |
|---|---|
| `reading.py` | `latest_reports`（`codes`、`code_prefix`、`GROUP BY`）、研究库搜索、`:467`、`latest_price`、`api_reports`、`stock_reports`、`:249`、`:741`、`:805-806`，以及 `$.parent_report_id`、`:332,339,738,783,785` 的 `LIKE 'batch-%'` |
| `service.py` | `company_card`、`submit_price`、`:289-292` 改 `batch_id` + `company_key`（`JOIN batches` 保留）；`:135` 的 `startswith('batch-')` 改用 `batch_id` |
| `worker.py:34` | 排队顺序改用 `batch_id IS NOT NULL` |
| `scans.py`、`hypotheses.py`、`evolution.py`、`research.py` | `company_key` 与第 3.5 节常量 |
| `validation.py`、`zones.py`、`screening.py` | 只在涉及公司比较的地方用 `company_key`，**选样谓词不动**（第 3.5 节） |
| 全部 `SELECT *` / `r.*`（`runs`、`reports` 共 9 处） | 改为显式列，既不把新列带进 API，也不再无谓读取大字段 |

### R3 代码层收口（例行）

- 约 35 处公司代码的 `[:6]`：有行数据的直接用 `company_key`，入参用 `stock_codes.normalize_key`。`backtest.py`、`industry.py` 的 `d[:6]` 是日期，不动。
- 其余重复的代码正则（`service.py:226`、`web.py:587`、`reading.py:81,370`、`research_tools.py:347`）统一到 `stock_codes`，错误文案与状态码不变。有意变化：全角数字从 `web.py:587` 通过后查空，改为返回既有的校验错误。
- 新增 `tests/test_data_foundation.py` 钉住不变量：
  - 五张表都有 `company_key`，三张重建表最后一列是大字段；
  - 五张表都拒绝非法代码；
  - 同一组输入：规范形式下 `company_key` 表达式与 `stock_codes.normalize_key` 结论一致；带首尾空白时入参函数接受、存储拒绝；
  - 删除最新报告后 id 不复用；
  - 内部查询函数 6 位和 9 位入参结果相同；
  - 源码扫描：SQL 中无 `substr(...code`，无 `LIKE 'batch-%'`；`replay_of IS NULL` 只出现在 `db.py` 常量和第 3.5 节列出的保留文件中，防止样本口径重新分叉。
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
| 异常回滚 | 在 `DROP TABLE runs` 后注入异常：结构完全回滚、版本仍为 8；重入结构一致（架构审查复现，原型范围） |
| SQLite 版本 | 当前生产镜像 3.46.1，满足生成列（≥ 3.31） |
| `batch_id` 严格表达式 | 与旧四变体在 160 条真实任务及 15 个变体上结果相同（内存表，见第 3.3 节） |

同一份旧代码在重建前后的耗时（141 份报告、160 个任务）：

| 操作 | 之前 | 之后 |
|---|---|---|
| `pending_reviews` | 16.98 ms | 0.12 ms |
| 任务元数据 + `request` 全表 | 23.01 ms | 0.29 ms |
| 报告元数据全表 | 7.54 ms | 0.17 ms |
| 调用元数据全表 | 16.69 ms | 3.64 ms |
| `latest_reports` | 53.46 ms | 33.58 ms |

绝对值目前不大。价值在于这类查询不再随大字段总量线性变慢。`latest_reports` 剩余耗时主要来自读 `body` 里的检查点，本次不处理。

**尚未验证**：原型没有 `batch_id`，生成列位置也与最终设计不同，上表的迁移结果不能代替最终 DDL 的验收；进程崩溃和双进程同时启动未在最终版本上实测；R2、R3 的查询改动；未被上述 48 个文件覆盖的测试。前两项是 R1 门禁，其余在实施时验证。

## 7 验收与治理规则

**R1 门禁**（全部通过才发布）。除最后一项外都在发布备份的同一份私有副本上用最终 DDL 完成：

- 迁移结果：`integrity_check`、外键、触发器、序列、重入、新库建表；
- 异常与并发：`DROP` 后注入异常必须完全回滚；两个进程同时 `connect()` 只有一个执行迁移，另一个正常启动；
- 数据守恒：迁移前后按列名逐行比较全部非生成列（含 `body_sha256` 和冻结 JSON），必须完全相同。发布工具的两次指纹比较都在迁移之前（`deploy.py:143-159`），证明不了这一点；
- 回退兼容：用实际的回退镜像跑在 v9 副本上，读、写、触发器和完整性巡检都正常；
- 关系检查：`dangling_parent_reports` 恰好是任务 16、66；
- 发布后：对生产库只读复查一次数据守恒，只比较备份中已有的行。不一致就停止并报告所有者。这是发布后的确认，不阻止启动，应用回退也不会还原数据。

**主验收：新旧差分。** R2、R3 各跑一次，新旧代码用同一数据库副本：

- 合法 6 位入参：41 家公司调用 `latest_reports`、`stock_reports`、`latest_price`、`api_reports`、`company_card`，新旧完全一致；
- 9 位入参：内部查询函数的新版结果与旧版对应 6 位结果一致（旧版查空是要修复的问题，不作基线）；`company_card` 和 HTTP 接口仍拒绝 9 位，错误文案和状态码不变；
- 首页、研究库搜索、`pending_reviews`、批次视图的查询结果一致；
- 研究 API 的 JSON 响应一致，字段集合单独严格比较（Skills 依赖，不得增减）；
- `validation.py`、`hypotheses.py` 选出的样本 id 严格零差分（不调用 TuShare，只比样本）；
- 冻结语义：在副本上对真实行做最小变体（把已通过报告的任务改为 `failed`、`running`，加纠错），新旧选样结果必须相同。变体只用于本项，不进入研究或收益评估；
- 批次归属：第 3.3 节的畸形编号变体与 `is_monthly_research`、`worker.recover` 的旧行为一致。

差分脚本与结果放 `~/.local/share/a-share-research-evaluations/`，不进仓库。

**其他验证**：

- 每个 R：接触数据库的测试文件逐个 `-x`；Python 编译与 `git diff --check`。不无差别跑全量。
- R1 后的真实研究只证明主流程能跑，不替代批次、价格、纠错、id 和回退路径的验证。
- R3：互变验证，去掉归一化后不变量测试必须失败。
- R2 后：在 8610 开发实例用手机尺寸走一遍首页、`/stock/603606`、报告页、研究库搜索。

**回退**：

- 应用问题：两服务 `docker tag a-share-research-{web,worker}:rollback-<提交> a-share-research-{web,worker}:latest`，再 `docker compose up -d --no-build --no-deps web worker`。数据库不动，也不改 `user_version`（`connect()` 不拒绝更高版本，旧代码已验证可运行）。
- 数据问题：从 `backup-pre-<提交>-<时间>.db` 恢复，会丢失备份之后的写入，须所有者决定。旧备份的序列也是旧的，步骤必须是：
  1. 停服后先记录 id 高水位：被替换的库中 `sqlite_sequence` 的 `runs`、`reports`，以及 Skills 账本的 `max(holding_events.report_id)`；
  2. 恢复备份。如果备份是 v8，用目标镜像的一次性容器完成迁移；
  3. 开放写入前，把两个序列抬到不低于高水位，并核对账本引用的报告是否都存在；
  4. 高水位取不到可信值时，先完成归因核对，不得宣称恢复后 id 仍不复用。留在 v8 旧镜像上没有 `AUTOINCREMENT`，同样不能保证。

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

## 9 架构审查修订（2026-11-07）

Codex 只读审查，结论逐条对照源码和发布备份核实后修入本文，未发现已证实的 Critical。原文在 `~/.local/share/a-share-research-evaluations/data-foundation-20261107/codex-architecture-review.md`。

| 问题 | 修订 |
|---|---|
| `batch_id` 弱解析会把 `batch-1oops-*`、公司错配归入月批 | 第 3.3 节改用严格表达式，差分验证 |
| 恢复旧备份后 id 仍会复用 | 第 3.4 节写明保证范围，第 7 节增加恢复步骤 |
| 父报告断链（任务 16、66）未纳入治理 | 第 2 节列证据，新增第 3.4.1 节和巡检 |
| 选样缺 `status` 条件未列出，样本零差分可能掩盖规则变化 | 第 3.5 节逐处归类，第 7 节加变体，第 5 节加源码扫描 |
| 差分要求新旧 9 位结果一致，与归一化目标矛盾 | 第 7 节分开比较，第 3.1 节区分内部函数与 HTTP 契约 |
| P0 要恢复已退役的 `research_progress` 检查（11-03 主动移除） | 删除 |
| 全角代码在 R1 后变成裸数据库异常 | 校验提前到 R1 |
| 发布工具的指纹比较都在迁移之前，不能证明数据守恒 | 第 7 节新增 R1 门禁 |

核实时补充：R2 范围漏了 `worker.py:34`、`service.py:135` 和 `reading.py` 的 5 处 `batch-%`，已补入。审查没有发现需要等待数天才能成立的验证条件，排期据此压缩。
