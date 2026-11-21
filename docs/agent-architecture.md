# a-share-research：面向模型演进的架构重构方案

2026-11-21 · v6。v5 经 Codex 四轮审查后，又交 agy（Gemini 4 Argon High）做过度工程审查，所有者按审查结论改定（Telegram msg 4565）。审查记录与 v1–v5 存放在证据目录 `~/.local/share/a-share-research-evaluations/architecture-plan-20261121/`。

## 所有者决定（2026-11-21）

1. 按 agy 的审查结论简化。不为兼容旧逻辑和旧数据做兼容层；发布一律从简；允许试错，不写防御性测试，也不做防御性编程。系统边界（用户输入、外部 API、模型输出解析）照常校验。
2. L3 用 TypeScript 加 pi-agent-core，研究工具走 MCP（pi-mcp 客户端对接 worker 的 mcp SDK 服务端）。所需的 pi 包都从宿主的 pi 安装加载，随宿主升级，不在仓库锁版本。不用 pi-durable（msg 4573、4577）。agy 复审（`agy-argon-review2.md`）指出的 API 坑与遗漏已按所有者要求补进 2.1 节和第 6 节的实施要点（msg 4583）。
3. 性能不设门禁。评估集每次运行输出调用数和耗时报表，只用来看，不拦截发布。
4. 一次做彻底：切换后不保留新旧两条路径，也不保留旧编排开关。出问题前向修复，或者回到上一个提交。

## 0. 要解决的问题与成功判据

要解决的不是单次研究快几十秒，而是四件反复发生的事：重构后又得治理一轮（政策重新激活、部署记录、冻结窗口协调），性能悄悄退化，模型行为一变适配代码就落进业务代码，代码、测试和文档持续膨胀。

| 判据 | 现状 | 目标 |
|---|---|---|
| 行为不变的代码改动触发政策重新激活 | 触及 policy 依赖链就触发，已激活 30 次 | 0 次 |
| 性能退化能被看见 | 靠下一轮全仓库审查才发现 | 评估集每次运行输出每家的调用数、各阶段耗时和 token，与上一次对比 |
| 适配新模型行为要改的 Python 业务代码 | 适配补丁写在 `research_tools`、`limits`、`research` 里 | 0 行，只改 `agent-runner/` 与模型配置档 |
| Python 中的研究编排代码 | `Acquisition` 循环、review/repair 往返、`model.py`、`pi_infer.mjs`、`limits.py` | 全部删除 |

## 1. 诊断

- **时间花在模型上**：11-10 以来 25 次成功研究平均 323 秒，其中模型占 305 秒（94%）。成文 152 秒，工具轮约 6 轮共 73 秒，复核 62 秒，修补 17 秒。
- **政策身份绑定字节**：`policy/identity.py` 对 policy 包整条 import 依赖链和 `uv.lock` 的运行时部分算哈希，所以共享代码做纯重构也会产生新身份。
- **测试钉实现**：tests 共 14.4 万行，相当一部分断言的是内部路径。
- **模型适配混在业务里**：探索循环、停止判据、上限、格式纠正与复核往返全由 Python 手写，research 核心四个文件 10 月以来改了 103 次。账本规则已经集中在 `ReviewLedger`，`merge_repair_patch` 也是纯函数，L0 规则本身没有问题。

## 2. 目标结构

```
L3 agent-runner（TypeScript + 宿主 pi-agent-core/pi-mcp）  研究员/复核员循环、停止、上限、格式纠正、单轮任务
      │ MCP（子进程 stdin/stdout）
L1+L2 worker（Python）                       受理、领取、材料冻结、工具执行、机械检查、ReviewLedger、发布门禁、审计、记分
L0 事实与规则                                材料与证据、核验、calculate/valuation、补丁合并、判断账本、账户、政策
```

### 2.1 agent-runner
- 新目录 `agent-runner/`，用 Node 内置的类型剥离直接运行 `.ts`（镜像内 Node 22.23 默认开启），不加构建步骤，也不加 npm 依赖。runner 是 worker 的子进程，**每个 run 启动一次**，单轮任务每批启动一次。启动时按宿主 pi 安装的 `current-version` 加载 `pi-agent-core`、`pi-ai`、`pi-mcp`，以及 `pi-coding-agent` 里负责凭据与模型注册的 `ModelRuntime`。加载方式沿用 `pi_infer.mjs`，挂载方式不变。宿主升级 pi 后，下一个 run 自动用上新版本；`calls` 照常记录 `pi_version`。
- **选型**：采用 `pi-agent-core`。它是建在 pi-ai 之上的 Agent 循环，pi CLI 本身就基于它，自带工具循环、`prepareRequest`/`finishTurn` 钩子、`terminate`、`reset()` 与 `abort()`。不用 pi-durable：它独有的崩溃续跑对约 5 分钟的研究没有必要，而且要单独锁版本，和“随宿主升级”冲突。也不在 pi-ai 上手写循环，因为那等于继续自己维护适配代码。
- **研究工具走 MCP**：worker 用现有的 `mcp` SDK 低层 `Server`，按当前 run 注册研究工具，包括取证、`calculate`、`valuation`、`submit_draft`、`review_decide`、`submit_patch`，以及只给 runner 用、不给模型看的 `record_call`。工具 schema 只在 Python 定义一次。runner 用 `pi-mcp` 当客户端，从 `tools/list` 拿到工具后包装成 `AgentTool`，工具名与 Python 定义一致，结果透传 `structuredContent`。runner 自己不写工具定义，只按角色分配子集，不做内部鉴权。
- **传输**：MCP 的 stdio 帧格式（逐行 JSON-RPC）跑在 runner 子进程的 stdin/stdout 上。runner 一侧用 `PipeTransport` 接自己的标准输入输出，worker 一侧把 SDK 的 stdio server 接到子进程的管道上。这样不需要端口、线程或令牌，worker 仍是单线程，一次只跑一个 run，材料状态不需要加锁。两端握手已用最小样例验证（见第 6 节前置检查），不再保留 Streamable HTTP 备选。
- 同一个研究服务端可以单独以 stdio 启动，读取指定 run 的冻结材料，供 Claude Code、Codex 等 MCP 客户端在开发时挂上去做对照或排查。
- **流程映射**（研究员、复核员、修补各一个 `Agent`；工具一律顺序执行，`toolExecution: "sequential"`）：
  - **切换工具集**：`prompt()` 开始时会拷贝一份 `state.tools`，运行中给它赋新数组对当次循环无效。runner 用 `prepareRequest` 在每次请求前把 `context.tools` 换成该角色当前的工具集。
  - **取证**：在 `finishTurn` 里按整轮判断：本轮各工具回执中由 L0 算出的增量计数都为 0，才记一轮无增量（同一轮调多个工具只算一轮）。连续两轮没有增量，就把研究员的工具集换成只剩 `submit_draft`。
  - **成文与复核**：worker 对 `submit_draft` 做机械检查。检查通过后，runner 在这个工具的执行函数里先 `reviewer.reset()`，再 await 复核员的 `prompt()`，然后把问题列表连同按 `section[i]` 索引的原稿和 `program_valuation` 作为工具结果返回给研究员。复核员通过 `review_decide` 一次提交整轮决议，由 worker 内存中的 `ReviewLedger` 校验，有效时返回 `terminate: true` 结束复核员循环；格式无效时，当轮可纠正一次。终稿通过或复核轮数到上限时，`submit_draft` 返回 `terminate: true`，研究员循环随即结束，不再多发一次模型调用。
  - **修补**：新建修补 `Agent`，和现有 `repair()` 一样重开一段干净对话，输入索引化原稿、刷新后的资料块和问题列表，不在研究员的长对话里续写（那样会一直带着 6–15 轮工具回执和旧稿全文）。工具为现有的受限补证工具和 `submit_patch`；补丁非法时把 `submit_draft` 放回工具集，走现有的整稿重写路径，次数有上限。
  - **预算与审计**：`streamFn` 包装 `ModelRuntime.streamSimple`，在 `await stream.result()` 拿到 `usage` 和 `stopReason` 之后，先 await `record_call` 写入 `calls`，再把终态事件交给 `Agent`，这样最后一次记账一定发生在关闭客户端之前。runner 原样上报实际请求的 `messages` 与 `tools`；worker 的 `log_call` 照旧尝试增量与共享块压缩，对不上就存原文。TS 侧不拼旧切片格式。`step`、`identity.actual.model` 的口径不变。超时用 `abort()`，worker 另设整体截止时间，到期结束子进程。进程崩溃时，正在进行的那一次调用的记录会丢失，这一点接受。
  - **致命错误**：`EvidenceError`（来源身份或已读页冲突）这类现有设计中会终止整个 run 的错误，不作为普通 `isError` 工具结果返回给模型。worker 记下失败分类（`source_input`）后直接结束 runner 子进程，run 按现有规则失败。
- **中断**：沿用 `worker.recover()`：普通研究整单重跑一次，自动月批中的研究记为失败。runner 不保存任何持久状态。
- **历史 `--replay`**：worker 只注册只读工具，不允许新取证，并验收最终材料哈希不变，语义与 `research.py` 现有做法相同。
- **单轮任务**：Q20 选股、强制排序和 S10 独立比较改为调用 runner 的单轮无工具模式。各自的格式修正、调用明细和冻结规则不变，上限仍是“三项比较或排序各最多 2 次、整批最多 636 次”。双角色预检改为调用 runner 的 `--check`。
- runner 不提供文件、命令或 bash 工具，研究 AI 的三条硬边界不变。

### 2.2 worker
可交付条件和发布门禁仍由 Python 判定：已复核终稿中，定位合规的具名 `question` 可以交付，其余未解决问题一律阻断。估值、立场冻结、事件证据、报告与 run 终态在同一事务内写入。受理、去重、扫描接续、月批、记分等入口不变，只把模型调用改为经由 runner。

### 2.3 模型配置档
用 JSON 配置档取代现有的 `MODEL_*`、`MODEL_REVIEW_*` 环境变量，配置档入库，放在 `a_share_research/` 包内，随基础镜像供 worker、web 与 tools 共用（`service.py` 的 `research_call_limit`、`run_budget` 改从这里读调用上限）。每个角色写明 provider、model、reasoning、max_tokens、各项上限与预留、复核轮数上限。配置档的哈希和提交号写进每个 run 的 identity，`stability.py` 和记分读取器按配置档分组；旧记录标为“未记录”。上限数值保留一行来源说明，沿用 `limits.py` 注释的写法。换模型（E 类）就是换一份新配置档，然后跑评估集。

### 2.4 判断契约
只保留 `contract.py` 的 `JUDGMENT_CONTRACT` 整数。判断口径有实质变化时手动加 1。删除 `SURFACE_SHA256` 以及 `test_judgment_contract.py` 对源码哈希的锁定。记分按契约号过滤、同月去重的业务测试保留。切换到 runner 时契约号加 1。

## 3. 政策身份

- 身份 = digest(规则文件 + 指南 + 参数)。代码文件和 `uv.lock` 不计入身份，同时删除运行时的代码字节比对（`check_installed`）。
- 不做 v1/v2 兼容。现行政策按新算法重新登记一次，规则、指南和参数与激活 30 完全相同，由发布工具按常设授权自动激活。旧版本行作为历史留在库里，不再验签；`registry.inspect` 只校验当前生效的激活。
- 评估回执照常记录政策身份与提交号，用于溯源。
- `policy/` 代码改动的保障：从真实评估中抽约 20 条典型样本，写成普通 pytest，只在改动 `policy/` 时运行。

## 4. 发布

- 发布工具只保留：空闲检查与暂停、发布前数据库备份（保留最近 3 份）、切换镜像、`/ready` 就绪检查、政策身份变化时自动激活、清理旧镜像。
- 删除：回退探针（`rollback.py`、`rollback_probe.py`）、首次发布路径（`initial_release.py`、`--initial-recovery`、`--with-tools`）、`--complete-held`、源码 tar 与挂载的反射核对。
- 回退：用上一个提交的镜像重新发布。发布带库迁移时，先恢复发布前备份，期间新增的写入自行补回。本方案两步都不需要库迁移。

## 5. 测试与文档

- 随旧编排退役的测试整文件删除，断言内部路径的测试也一并删除。
- 保留：L0 不变量（账本重建、冻结不改写、引文核验的真实样本与篡改变体）、政策样本测试、评估集。
- 切换的同一提交里，按实际结构改写 `ARCHITECTURE.md` 和 `docs/proposal.md` 第 5 节。

## 6. 实施步骤

冻结窗口：11-29 至 12-04 保护首个自动月批，12-30 至 01-04 保护 1 月月批。窗口内照常开发，只在开发实例上验证，不发布。

| 步骤 | 上线 | 内容 | 完成判据 |
|---|---|---|---|
| 1 政策身份与发布瘦身 | 11-26（最晚 11-28） | 第 3 节、第 4 节、2.4；评估集报表 | 一次例行发布完成切换并自动激活新身份；11-27 的自然止损检查与日报正常 |
| 2 agent-runner 切换并删旧 | 12-08 | 2.1–2.3；删除 `Acquisition`、review/repair 往返、`model.py`、`pi_infer.mjs`、`limits.py` 及其测试；契约号加 1；改写文档 | 开发实例上跑通 4 例：1 家正常公司、1 次 `--replay`、招商银行反例（结论须与基线相同，为不适用）、1 次选股比较；评估集报表正常输出 |

### 实施要点

第 1 步：
- 新算法上线时，库内当前激活仍是激活 30（旧算法清单）。宿主的 `freeze_policy` 和新容器 `policy_gate` 的首次 `policy_probe` 都会先读它，所以 `_version` 不再对旧算法清单验签，`_read_state` 读到旧清单时标 `installed=False` 放行，由发布工具按原参数登记新身份并激活。`registry.inspect` 遍历 `policy_evaluations` 时只核对参数，不对历史版本行验签。
- 删除 `code_hash`、`files.code`、`check_installed` 时一并改掉调用点：`deploy.identity_diff()` 里的 `old['files']['code']`、`registry.main()` 输出中的 `code_hash`，以及 `registry.py`、`monitor.py`、`derived.py` 里对 `check_installed` 的调用。

第 2 步：
- **凭据**：`pi-agent-core` 和 `pi-ai` 不读 `~/.pi/agent/auth.json`、`models-store.json`。runner 沿用 `pi_infer.mjs`，从 `pi-coding-agent/dist/core/` 动态加载 `ModelRuntime`、`AuthStorage`、`FileModelsStore`，`streamFn` 用 `runtime.streamSimple.bind(runtime)`，由它解析和刷新 OAuth，退出前 `await runtime.settleCredentialWrites()`。代码里对 `@earendil-works/*` 只写 `import type`，值一律按绝对路径动态 `import`。
- **stdio**（已用最小样例验证，见下文前置检查）：worker 侧用 `subprocess.Popen` 起 runner，把 `proc.stdout`、`proc.stdin` 各包一层 UTF-8 `TextIOWrapper`（写端 `line_buffering=True`），再用 `anyio.wrap_file` 传给 `stdio_server(stdin=, stdout=)`。显式传入流时，SDK 不会接管 worker 自己的 fd 0/1。仓库锁定的 Python `mcp` 是 2.3.0，低层 `Server` 的处理函数通过构造参数 `on_list_tools`、`on_call_tool` 注册，写法同 `tools/remote_mcp.py`，1.x 的装饰器写法已经不能用。runner 侧 `McpClient` 的构造参数是 `{ name, version, requestTimeoutMs: 300_000 }`（默认超时 30 秒，冷缓存的 `industry_trend` 会超过 60 秒）；`PipeTransport` 设 `closeTimeoutMs: 0`，`closeStdin` 里调用 `process.stdout.end()`。`client.close()` 之后，runner 必须 `process.stdin.destroy()`（或直接退出），否则 worker 侧的 `server.run` 会一直等下去。runner 第一行把 `console.log` 指向 `console.error`，防止污染 stdout 帧。
- **镜像与发布**：`Dockerfile` 加 `COPY agent-runner ./agent-runner`；`deploy.py` 发布前的 `git status --porcelain` 检查列表加入 `agent-runner`。
- **删除 `model.py`**：`application_identity()`、`judgment_identity()`（`research.py`、`screening.py`、`stability.py` 使用）和 `preflight`（`worker.py`、`deploy.py` 使用）迁入保留的模块，`preflight` 改为调用 runner 的 `--check`。

### 前置检查（2026-11-21）

样例与输出存放在证据目录的 `stdio-probe/` 下。
- 宿主 pi 为 1.6.0，`pi-agent-core`、`pi-ai`、`pi-mcp`、`pi-coding-agent` 齐全，要求 Node ≥ 22.19。生产 worker 容器的 Node 是 22.23.5，能直接运行 `.ts`，经 `/home/lin/.pi` 挂载可以加载这四个包；容器内 Python `mcp` 为 2.3.0。
- stdio 握手（Python 低层 `Server` ↔ `pi-mcp` 的 `PipeTransport` 加 `McpClient`）在宿主跑通：3 MB 的工具结果完整到达（单条消息上限 16 MB），`structuredContent` 透传，35 秒的工具调用在 300 秒超时下正常返回，关闭耗时 1 ms，worker 侧退出码 0。
- 生产为 `528972c`，政策激活 30，没有排队或运行中的任务；之后的提交都只改文档。owner 凭据是 0600，磁盘剩余 21 GB。第 1 步涉及的测试（政策身份、登记、发布、契约、v0 政策）基线为 959 项全部通过。
- 已知遗留：`tests/test_batch_c.py` 中有一项在 main 上原本就失败，与本方案无关。

观察项（不阻塞）：第 2 步上线后，头 5 份生产报告的复核通过率、失败分类和引文核验结果。

与其他里程碑的关系：
- 12-11 原定删除首次发布路径，改为并入第 1 步。
- 12-18 段 4a 回退到上一个生产镜像。
- 12-23 段 4b 在第 2 步之后的新结构上实现，契约号取下一号。
- 01-01 脚手架删减对照用模型配置档来做。

## 7. 风险

| 风险 | 对策 |
|---|---|
| 宿主 pi 升级后行为变化，或 pi-agent-core 出现破坏性 API 变更 | `calls` 记录 `pi_version`；runner 报错或报告异常时先对照版本，前向修复 |
| 切换后发现缺陷 | 前向修复，或回到上一个提交的镜像 |
| 删掉身份里的代码哈希后，改代码无意中改变了政策输出 | 改 `policy/` 时跑约 20 条真实样本测试，并观察自然评估 |
| 订阅通道 429 | 一次只跑一个 run，与现状相同 |

## 8. 文档与规则对齐

| 文档 | 调整 |
|---|---|
| `AGENTS.md` 授权 | 分层重构的长程授权改为两步的范围 |
| `AGENTS.md` 工程约束 | `agent-runner/` 从宿主 pi 安装加载 pi-ai，不锁版本；第 2 步上线后，模型只经 agent-runner 调用 |
| `AGENTS.md` 验证与交付 | F 类改为“规则、指南、参数”；删除 SQL 语句预算规则，改为评估集报表 |
| `docs/implementation-plan.md` | 工作包与里程碑改为两步 |
| `docs/web-mcp-platform.md` | 4a、4b 的回退目标与实现时机 |
| `docs/judgment-validation.md` | 契约面哈希改为只用契约号，记分加按配置档分组 |
| `README.md` 部署与配置 | 随对应步骤的提交更新：第 1 步删去回退评估（`rollback.assess`、`rollback-probe/`）和 `--complete-held` 两段；第 2 步把 `MODEL_*` 说明改为模型配置档，并改写预检说明 |
| `ARCHITECTURE.md` | 第 1 步把 `policy/` 一行的身份说明改为“规则、指南、参数”；第 2 步按第 5 节整体改写 |
| `docs/implementation-plan.md` 回退段 | 第 1 步删去“读取兼容性”核对，改为“重新发布上一个提交的镜像” |
