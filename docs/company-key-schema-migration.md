# A 股标的标识归一化与 Schema 生成列加固方案（Option 2 / Migration v9）

更新：2026-11-07。前序实现：`fe0fd5b`（输入侧归一化与应用层双向兼容）。状态：**设计完备待实施**。  
关联文档：[项目契约](project-contract.md)、[实施计划](implementation-plan.md)。

---

## 1. 背景与定位

在提交 `fe0fd5b` 中，系统通过纯应用层容错（`stock_codes.py` 与 `reading`/`service` 查询前缀兼容）彻底修复了 `/stock/603606` 404、价格参考提交 404 及 Worker 潜在异常等真实生产故障。当前所有 141 篇生产报告和 16 条价格记录均在零 DDL、零数据风险的前提下稳定运行。

**方案 B（本方案）的定位**：  
属于**数据库底层防御加固与 Schema 规范演进**。通过在 SQLite 表结构中引入虚拟生成列（Virtual Generated Column）与专用 B-Tree 索引，从数据库底层建立“A 股标的业务标识”的强约束与高效检索体系，不再依赖应用层零散的 `substr(code, 1, 6)` 计算。

---

## 2. 标的标识三层架构规范

为避免历史表示、外部接口与内部关联职责混杂，系统严格确立三层标识模型：

| 标识层级 | 字段/变量命名 | 格式规范与示例 | 独占职责与边界 |
|---|---|---|---|
| **业务关联键** | `company_key: str` | 严格 6 位纯数字，如 `"603606"`、`"000333"`（必须为字符串，保留前导零） | 聚合分组（`GROUP BY`）、跨表关联、字典索引、URL 路由、报告前后对照识别。由数据库自动生成或通过 `stock_codes.normalize_key` 校验推导。 |
| **存储记录码** | `code: str` | 6 位或 9 位，如 `"603606.SH"` 或 `"603606"` | 原样保留写入时形态，仅作为历史存证与界面默认显示，**不再承担跨记录身份判定**。 |
| **数据源接口码** | `ts_code: str` | 必须带合法交易所后缀，如 `"603606.SH"`、`"000333.SZ"` | TuShare、行情获取接口调用；由专用解析器从 `code` 提取或按前缀规则推导。 |

---

## 3. 数据库模式升级与 Migration v9 设计

基于 Claude 针对生产备份库的实机原型审查，本方案解决了 SQLite 生成列迁移的三个关键陷阱：

### 3.1 生成列定义与表范围
仅针对具有检索和关联需求的 `reports` 和 `prices` 增加生成列，不对 `runs` 表增加生成列，以最小化任务写入时的冲击面。

**生成列 SQL 表达式**（严格使用 `[0-9]` 逻辑，避免 Unicode 非 ASCII 字符）：
```sql
CASE
    WHEN length(code) IN (6, 9)
     AND substr(code, 1, 6) NOT GLOB '*[^0-9]*'
     AND (length(code) = 6 OR substr(code, 7) IN ('.SH', '.SZ', '.BJ'))
    THEN substr(code, 1, 6)
END
```

### 3.2 避免启动死锁（SCHEMA 保持纯净）
`a_share_research/db.py:connect()` 的执行顺序是：**先跑 `conn.executescript(SCHEMA)`，再遍历执行 `MIGRATIONS`**。  
如果直接将 `company_key` 索引写入 `SCHEMA` 字符串，存量 v8 数据库在启动时会立即抛出 `no such column: company_key`，导致甚至无法进入迁移流程。  
**规程**：`SCHEMA` 保持原样，生成列的添加与索引建立**全部由 `_migrate_v9` 独立承担**（与 `replay_of`、`coverage` 的历史加列模式严格保持一致）。

### 3.3 迁移幂等与生成列识别（采用 `table_xinfo`）
SQLite 的标准 `PRAGMA table_info` 无法识别虚拟生成列（Virtual Generated Column），若使用 `table_info` 会导致重复执行时误报 `duplicate column name`。  
**规程**：必须使用 `PRAGMA table_xinfo` 进行列存在性检测。

### 3.4 迁移实现函数 (`a_share_research/db.py`)
```python
def _migrate_v9(conn: sqlite3.Connection) -> None:
    expr = """
    CASE
        WHEN length(code) IN (6, 9)
         AND substr(code, 1, 6) NOT GLOB '*[^0-9]*'
         AND (length(code) = 6 OR substr(code, 7) IN ('.SH', '.SZ', '.BJ'))
        THEN substr(code, 1, 6)
    END
    """
    for table in ("reports", "prices"):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_xinfo({table})")}
        if "company_key" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN company_key TEXT GENERATED ALWAYS AS ({expr}) VIRTUAL NOT NULL")
        conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_by_company_id ON {table}(company_key, id)")
```
在 `db.py` 中更新迁移元组：
```python
MIGRATIONS = (_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7, _migrate_v8, _migrate_v9)
```

---

## 4. 既有测试用例改造清单（关键阻断项处理）

SQLite 规定：**虚拟生成列绝对禁止显式执行 `INSERT`**，否则抛出 `OperationalError: cannot INSERT into generated column`。因此在引入生成列前，必须先适配修改以下测试：

| 测试文件与行号 | 原有模式与冲突 | 适配改造方案 |
|---|---|---|
| `tests/test_feedback_retry.py:39, 65` | `SELECT *` 获取整行后按字典键直接执行回插，包含了 `company_key` 导致抛错 | 在回插前显式过滤生成列：`cols = [k for k in row.keys() if k != 'company_key']` |
| `tests/test_scans.py:217` | 对比测试库与基准 Schema 的 `iterdump`，表结构变化导致断言失败 | 更新测试内的 `CREATE TABLE` iterdump 基准字符串，体现生成列与索引 |
| `tests/test_db.py:43` | 断言 `conn.execute("PRAGMA user_version").fetchone()[0] == 8` | 断言值更新为 `9` |
| `tests/test_db.py:173, 176` | 将版本 `9` 作为模拟的“未来版本”进行测试 | 将模拟的未来版本号更新为 `10` |
| `tests/test_batch_c.py:59` | 写死检查 `user_version == 8` | 断言值更新为 `9` |
| `tests/test_scans.py:216, 222` | 写死检查 `user_version == 8` | 断言值更新为 `9` |

---

## 5. 业务模块改造清单

在数据库模式升级后，业务查询可直接切换至高效索引，无需在 SQL 内部调用 `substr` 计算：

1. **`a_share_research/reading.py`**：
   - `latest_reports`：
     - CTE 改为：`WITH latest AS (SELECT p.company_key, MAX(p.id) AS id, ... FROM reports p JOIN runs u ON u.id = p.run_id WHERE ... GROUP BY p.company_key ...)`；
     - `codes` 过滤参数：`p.company_key IN (...)`（命中 `reports_by_company_id` 索引）；
     - `code_prefix` 过滤参数：`p.company_key = ?`；
     - 主查询 `SELECT` 列表中显式包含 `r.company_key`。
   - `stock_reports`：
     - 查询优化为：`WHERE r.company_key = ? AND u.kind = 'research'`。
   - `latest_price`：
     - 查询优化为：`WHERE company_key = ? ORDER BY id DESC LIMIT 1`。

2. **`a_share_research/service.py`**：
   - `submit_price`：
     - 查询最新报告优化为：`SELECT code, name FROM reports WHERE company_key = ? ORDER BY id DESC LIMIT 1`；
     - 保持安全代码注入逻辑不变（写入 `runs.code` 的必须是合法的带后缀 `ts_code`）。
   - `company_card`：
     - 查询改为：`WHERE r.company_key = ?`。

---

## 6. 验证矩阵与发布回滚预案

### 6.1 离线验证矩阵
实施阶段必须通过以下五维验证：
1. **存量升级测试**：以生产实际 `app.db`（141 篇报告副本）执行迁移，耗时要求 `< 0.1s`，`PRAGMA integrity_check` 必须为 `ok`；
2. **重入测试**：手动将 `user_version` 修改回 8 后再次触发连接，系统必须平稳重新升至 9 且不抛重复列异常；
3. **脏行拦截测试**：插入非法代码（如 `603606.SS`、`60360`、非 ASCII 字符），必须被 SQLite 判定约束失败而回滚；
4. **回退兼容测试**：当前生产代码（`fe0fd5b`）连接 v9 数据库时，读取、价格计算及完整性检查均可无缝兼容；
5. **全量回归测试**：运行全部 289+ 项测试，全绿通过。

### 6.2 生产发布与回滚步骤
- **前置门禁**：按 `AGENTS.md:36` 确认本次包含 DDL 变更，发布前通过 `deploy.py` 自动执行空闲检查与全量备份（`backup-pre-v9-*.db`）；
- **回滚操作**：若发生未预料异常，回滚仅需执行：
  ```bash
  # 恢复镜像
  docker compose down && docker compose up -d
  # 数据库若需降级，恢复备份副本或执行：
  PRAGMA user_version = 8;
  ```
  无需冗长的数据清洗，5 分钟内可快速恢复。
