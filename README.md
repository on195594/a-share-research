# a-share-research

收盘后的 A 股个股研究助手：输入一家公司和可选问题，AI 自主补读定期报告和临时公告、查询财务与程序计算，通常几分钟后读到带原文出处的研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

本项目负责公司财报研究与阅读；`a-stock-agent-skills` 负责 Agent 投研、持仓与风险，两者独立运行并共用 `a-stock-lib`。网页与研究 API 共用任务、原文、计算和追问/纠错；不直接修改账户或投资政策。

- 产品方向与现有流程：[产品方案](docs/proposal.md)；开发约束：[AGENTS.md](AGENTS.md)。
- 三项目职责与协作：[项目契约](docs/project-contract.md)。
- AI 能力演进与进入条件：[路线图](docs/roadmap.md)。
- 候选发现与研究实验：[方案](docs/screening-experiment.md)，用法见下文[候选初筛](#候选初筛)。
- 基本面深度、经营序列与投资论证：[升级方案](docs/investment-upgrade.md)。
- 接续研究、检查点与归因复盘：[积累方案](docs/research-accumulation.md)。
- 当前批次与验收：[工作包与状态](docs/implementation-plan.md#工作包与状态)；生产版本与回退：[当前部署](docs/implementation-plan.md#当前部署)。

## 项目目录

`web.py` 负责鉴权、参数与渲染，页面数据查询集中在只读的 `reading.py`，提交写入由 `service.py` 承接。`companies.py`、`batches.py` 与 `evidence.py` 提供共享资料和纯函数，网页不加载行情供应商依赖。

| 路径 | 内容 |
|---|---|
| `a_share_research/` | 应用源码；`web.py` / `worker.py` 为服务入口，`research.py` 编排单股研究（取材料 `material.py`、机械核对 `checking.py`、成文契约 `prompts.py`、取证工具 `research_tools.py`），`screening.py` / `backtest.py` 为候选批次与历史回测，`backtest_experiment.py` 为离线归因 |
| `a_share_research/templates/`、`static/` | Jinja 页面、样式和渐进增强脚本 |
| `tests/`、`tests/data/`、`tests/support/` | 回归测试、真实采集样本、公共测试支撑；组织与验证范围见 [测试说明](tests/README.md)，样本随代码保留 |
| `docs/` | 方案、项目契约、路线图、实施与部署状态；`PRODUCT.md` / `DESIGN.md` 是界面设计入口 |
| `data/` | 本地运行数据库、PDF 与提取缓存（不进 Git）；生产数据另由 `DATA_DIR` 指定 |
| `Dockerfile`、`compose.yaml` | Web / worker 构建与部署 |

`.pytest_cache/` 和应用、测试目录下的 `__pycache__/` 可重建；`.playwright-mcp/` 是临时浏览器产物，正式验收证据归档后可清理。`data/`、`.env*`、真实测试样本和仓库外评估证据保留。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

```bash
uv run --env-file .env python -m a_share_research.research 600519 --question '最新财报改变了什么？'
uv run --env-file .env python -m a_share_research.worker            # 后台 worker：执行网页提交的研究与价格参考
uv run --env-file .env.web.dev uvicorn a_share_research.web:app --port 8610  # 网页（本机开发）
```

验证按影响范围选择测试文件（见[测试说明](tests/README.md)）；需要完整离线回归时用 `uv run pytest -q tests`。不以离线测试代替真实研究或浏览器验收。

`.env.web`（本机开发可用 `.env.web.dev`）只放网页配置：`PUBLIC_BASE_URL`、`SESSION_SECRET`（≥32 字符），以及 GitHub OAuth 的 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`；本机开发可用 `DEV_LOGIN=1`（仅限 loopback 地址）代替 OAuth。数据默认在 `./data`，生产用 `DATA_DIR` 指到代码目录之外。

初筛与回测的 TuShare 查询使用 `DATA_DIR/tushare_cache.db`（研究取数不经缓存，始终直连）：仅缓存 `trade_date` 或 `end_date` 早于上海当天日期的历史行情与财报响应；开放时间范围、当天及未来日期的查询和空结果不缓存。设置 `TUSHARE_CACHE=0` 可完全旁路缓存，直接查询 TuShare（不改写已有缓存）。历史披露若发生更正，需刷新对应缓存后再恢复缓存查询。

worker 重启时，带 `request_id` 的中断任务自动重新排队一次，重新获取干净材料并开始探索、成文和复核；再次中断则标记失败。恢复标记保存在 `runs.error`，`runs.request` 保持原样；命令行独立任务不参与自动恢复。

AI 或脚本做浏览器验收时不走 GitHub。使用当前源码的工具，在能访问数据目录的宿主签发短期浏览器会话；`--storage-state` 独占创建 0600 文件，不把令牌打印到工具输出：

```bash
release_browser_dir=$(mktemp -d /tmp/a-share-browser.XXXXXX)
DATA_DIR=/home/lin/a-share-research-data uv run --env-file .env.web python -m a_share_research.login_token \
  --minutes 15 --storage-state "$release_browser_dir/state.json"
```

本机开发改用开发库及 `.env.web.dev`。Playwright 直接使用 `browser.newContext({storageState: '/tmp/实际目录/state.json'})`；无需动态导入文件模块、打印令牌或手写会话 SQL。验收后通过页面“退出”撤销该会话，再删除本次临时文件。原有令牌输出和 `--agent` 接口仍保留，`--agent` 不与浏览器文件输出混用。

## 报告阅读

研究员可用 `peers` 发现申万行业内的公司，自主选择是否补取同行证据。通过报告的“财务与走势”章节可展开“已存同行快照对照”（有同行记录时显示）：按最近共同期间读取归母净利润、经营现金流和已有比率，显示口径与历史版本限制。浏览时不取数、补算或排名；详细边界见[步骤 1b](docs/investment-upgrade.md#76-步骤-1b同业发现与快照对照)，部署与验收见[实施计划](docs/implementation-plan.md#当前部署)。

## Agent 研究 API

公司档案提供“这轮研究推进了什么”：对明确关联的已通过报告，显式整理旧认识、增量、判断影响、仍未知与下一步，逐项回到原证据。没有已复核结果时说明缺失，原报告与程序对照继续可读。生成/复核只在 worker 执行，各一次，不取新数据、不自动重试；浏览、刷新和返回不调用模型。

`GET /api/reports/{id}/progress` 读取相同结果；`POST /api/reports/{id}/progress` 显式排队，同一前后报告与输入版本共用一个记录。鉴权沿用下述 API Key。附属记录不回写研究正文，升级会新增 `research_progress` / `progress_calls` 表，生产迁移及部署须另行确认。

本地或隔离库可使用 `uv run --env-file .env python -m a_share_research.progress <本次报告ID>` 只读检查；追加 `--generate` 才执行最多一次生成和一次复核，不重新探索。中断任务保留状态和调用预留记录，须先核对进程/调用结果，不能删除记录重试。真实样本验证范围与当前阻碍见[实施计划](docs/implementation-plan.md#工作包与状态)。

已有授权 API 消费者导出的完整 `research-v1` JSON 可用 `uv run python -m a_share_research.progress_import --database /path/to/new-isolated/app.db report-40.json report-41.json report-42.json report-44.json` 导入新建隔离库。目标已存在则拒绝；保留正文、报告 ID 与导出证据哈希，不伪造原运行材料快照，也不复制访问令牌。普通网页登录会话不能调用 Bearer API。

Skills 等长期调用方使用具名研究 API Key，持续有效直到撤销。owner 在目标实例的数据目录管理（容器内使用同样的 Python 模块）：

```bash
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys create skills-runtime
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys list
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys revoke <Key编号>
```

`create` 只输出一次明文，存入调用方 0600 的私有 `runtime.env`：`A_STOCK_RESEARCH_URL` 指向服务，`A_STOCK_RESEARCH_TOKEN` 保存 Key；不进入聊天、日志、命令参数或 Git。服务端只存哈希，`list` 只显示编号、名称与创建时间。管理命令仅由 owner 本地执行；Key 只授予研究 API 权限。

轮换：创建新 Key → 更新配置并验证读取 → 按编号撤销旧 Key。同名 Key 可并存；环境变量优先于文件，须排除旧值覆盖。Key 不受服务重启或浏览器登录/退出影响。401 后先修复凭据，再沿用原 `request_id` 重试。

临时验收仍可用 `python -m a_share_research.login_token --agent --minutes 60`（1–480 分钟，不续期）。两种凭据均通过 `Authorization: Bearer ...` 发送，与浏览器 cookie 隔离；`POST /api/session/revoke` 仅撤销当前凭据。API 无需模型或行情凭据。

请求使用 `Content-Type: application/json`；可带 `X-Research-Version: research-v1`，未知版本返回 400。

| 路由 | 输入/结果 |
|---|---|
| `POST /api/research` | `{"code":"600519","question":"最新财报改变了什么？","request_id":"本次唯一编号"}` → 202 与 `run_id` |
| `GET /api/runs/{run_id}` | 排队/研究/成功/失败、步骤、错误和 `report_id`；成功状态为 `done` |
| `GET /api/request?request_id=...` | 只读找回原请求的任务、诊断与调用计数；404 不证明原提交失败，不自动重提或重启失败任务 |
| `GET /api/research/budget` | 模型调用上限（循环保护而非金额预算）；费用未知，不按调用次数推算实付 |
| `GET /api/reports?code=600519` | 最近 100 份报告的身份、时间、状态 |
| `GET /api/reports/{report_id}` | `research-v1` 信封、核验结果、回答、来源页、表格及计算输入；GET 不刷新数据 |
| `GET /api/reports/{report_id}?compact=true` | 精简正文与来源目录（不含整页原文）、失败 `diagnostics`、`budget`、`continuation`；完整读取保持原合同 |
| `POST /api/research`（追问） | 上述输入加 `parent_report_id`，必须同公司，创建关联新任务 |
| `POST /api/reports/{id}/feedback` | `{"request_id":"新编号","location":"financial[0]","text":"具体问题与依据"}` → 核查任务 |

request/budget/compact 均沿用研究 API 鉴权，只执行读取，不做 schema 迁移；实际部署状态见[实施记录](docs/implementation-plan.md#当前部署)。
精简视图中的 `continuation` 区分生成日、财务覆盖期、行情日、披露与公告检索范围；
`historical_not_live_checked` 不承诺当前最新。旧检查点是待核查议程，不是当期事实；
`baseline_status` 明确区分可用、失败、未就绪、缺失等基线，并携带基线财报期、生成日、行情日和公告范围。
表格差值复用网页同一 `reading.comparison` → `since_prior` 入口；`excluded/missing/reason` 保留单位、名称、旧格式和缺失值限制，不把不同累计期间相减，也不推断投资理由已改变。精简视图额外要求两份报告均通过核查，否则不给 `diff`；这比网页的程序表格对照更保守。
失败时返回已有核查问题和缺口；异常中断尚未保存报告时仍读取已落盘材料中的缺口，不曝光材料全文或未通过正文；预算提示不等于已确定根因。
`cost=null/cost_status=unavailable` 不是零费用，硬金额上限无法保证时消费者应停止澄清；
读取不触发付费重跑，不写真实账户或 L3。
全局预算为 `scope=configuration`，`model_calls_recorded=null`；有具体任务时为 `scope=run` 并携带 `run_id` 和该任务调用计数。
获得上线授权后须先部署服务端，再激活配套 runtime/Skills；旧服务缺接口不得解释为任务不存在或自动重开研究。

相同编号和内容返回原任务；编号相同但内容不同返回 409。不存在返回 404；未就绪或状态不一致返回 409；非法位置/跨公司返回 422；未认证或失效返回 401。`location` 是 `summary` 或 `answer/risks/business/financial/methods/checkpoints` 的实际零基下标，例如 `financial[0]`。报告的 `passed` 与任务状态一起判断；失败报告不提供已通过的结论，可保留的事实范围见[产品方案](docs/proposal.md#2-从用户角度的取舍)。

财务表覆盖三表、现金流与债务；字段、单位和缺失值规则见[三表口径](docs/investment-upgrade.md#21-结构化三表扩展)。通过报告的 `report.series` 是只供阅读的经营序列，不属于可引用的 `evidence`。旧报告不回填新增指标，缺失不当作零。

材料与计算可在 `evidence` 中定位；`coverage.periods` 只列本公司财报期间，行情日期另见 `market_date`，`coverage.missing` 包含取证与报告声明的缺口。旧报告缺少的身份不回填。仅覆盖实际读取的披露和已取财务数据；`coverage.announcement_searches` 列出公告标题检索的关键词、日期范围及截断状态，不等于读完全部公告。临时公告沿用 PDF 页码、原文与内容哈希，公告日不冒充财报期间。

Skills 通过 `a-stock-cache research` 接入本 API，先复用旧报告，再按缺口发起追问；配置与使用见 [Skills 接入说明](https://github.com/on195594/a-stock-agent-skills/blob/master/skills/a-stock-research/references/research-service.md)，发布状态见实施计划。

纠错报告的 `methods` 是已核查的方法线索，使用记录随 `evidence.methods_used` 保存。针对 `methods[i]` 的成功核查可替换该检索项，空列表可撤下；失败不生效、原报告不改写。当前公司事实仍须重新取证，方法不会自动修改 Skill、代码、账户或政策；详见[反馈闭环](docs/roadmap.md#4-反馈如何转化为能力)。

## 运行身份与材料重放

每次研究在 `runs.identity` 记录应用文件内容哈希、Python/Lib 版本和材料哈希，在 `calls.identity` 记录模型、pi/Node 版本、调用参数和提示词哈希；未知一律留空，不用当前版本回填。`runs.material` 保存实际送入流程的财务表、行情时点、文档身份和原文页。

`--replay` 复用该快照重新执行成文、检查、复核和修复（包括修正阶段都不开放取证工具、不重新探索、不取数），保存为新任务；快照缺失、版本不支持、哈希或公司不一致时失败。重放报告在网页上标明来源，首页最新报告和“今日已研究”判断排除重放。

报告（纠错除外）带 1–3 条下期检查点。主动研究同一家公司且未指定原报告时，自动带入最近一份通过复核、非重放、非纠错报告的风险与检查点（不带结论与摘要），要求在回答末尾说明各自本期状态；批次深挖不接续。规则与验收见[积累方案](docs/research-accumulation.md)。

```bash
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research          # 建基线（评估集）
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research --replay 1 # 按任务 ID 重放
```

完整性巡检（不写数据库内容，可在线运行，不联网，不修复；契约见[方案第 6 节](docs/proposal.md#6-状态与正确性)）。有问题时输出清单并以 1 退出，旧报告没有正文哈希只计数：

```bash
docker exec a-share-research-worker python -m a_share_research.integrity   # 生产（备份前、恢复后）
DATA_DIR=/tmp/research-eval uv run python -m a_share_research.integrity     # 本地
```

价格参考支持沪深主板、创业板（300/301）和科创板（688），继续排除 ST、退市风险股及北交所；公式与旧 `price-reference-v2` 记录保持兼容。

## 候选初筛

网页“候选初筛”每月开一个批次（设计见[方案](docs/screening-experiment.md)）。worker 在没有排队的单家任务时推进批次：

1. 全市场确定性初筛（不调模型）：按交易日取股票池、估值、成交额和申万一级行业，粗筛后逐家取近 5 年 `fina_indicator`/`income`/`cashflow`/`fina_audit`，4 线程约 20–30 分钟；初筛期间单家研究排队。
2. 前 20 家（Q20）各自提交一次研究（7 天内已通过的报告直接沿用；网页或 API 新提交的研究排在这些批次研究之前），全部结束后经 pi 做一次跨公司比较，给出 5 家草案。
3. 批次页展示因子与行业分位、近 60 个交易日涨跌幅、利润跳升且市盈率低的提示、板块覆盖和缺口。模型草案生成即自动冻结，并标明未经人工确认；没有可选公司时才由所有者勾选。冻结后第一个交易日 17:00 后记录开盘入场价，之后更新 L5、Q5、Q20 与两个全收益基准的模拟收益。基准发布较晚时先入场、等待收益；只估值到两个基准共同已发布的交易日，未追到当日则每 30 分钟重试，已有同日收益不重算。

服务重启时，进行中的初筛记为失败（重新开始本月批次即可），跨公司比较自动重试。只看初筛结果、不建批次：

```bash
uv run --env-file .env python -m a_share_research.screening
```

历史回测（只测确定性规则 Q20/Q5，按月末点时还原数据，接口返回缓存在 `--out` 下，结果为每批一个 JSON；方法与结论见[方案第 5 节](docs/screening-experiment.md#5-历史回测)）：

```bash
uv run --env-file .env python -m a_share_research.backtest --out DIR --start 201601 --end 202510
```

续跑时只跳过已包含当前全部到期收益窗口的批次；缺少到期的 6/12 个月收益时复用接口缓存重算，成功后原子替换结果，失败保留原文件。仅处理已有下月交易日行情的完整月份，当前月自动跳过。接口缓存仍按请求参数复用，不主动刷新已有历史响应；需要全新取数时使用新的输出目录。

固定三项离线归因实验（不调用模型、不联网补缓存、不修改线上规则；在 Linux 上使用 `fork`；程序拒绝 `python -O` 或 `PYTHONOPTIMIZE`，确保断言核验生效）：

```bash
uv run python -m a_share_research.backtest_experiment --source BASELINE_DIR --calendar CALENDAR_JSON --out EXPERIMENT_DIR
```

`BASELINE_DIR` 必须是可信私有目录，包含既有 `cache/` 与 `batches/`；`CALENDAR_JSON` 为已采集的交易日历，包含 `end`（缓存财务查询截止日）和 `days`（自 2015 年起的 SSE 开市日升序列表）。输出目录须与基线分离，不能相同或互为祖先目录。程序逐批核对基线名单、分数与收益，缺缓存立即失败；保存来源与代码哈希，同一输入可续跑。`--limit 1` 可先做一个真实批次的检查。结果与限制见[实验方案](docs/screening-experiment.md#5-历史回测)。

## 部署

`db.connect()` 以 `PRAGMA user_version` 按版本逐事务迁移：v1 幂等补齐历史列；v2 增加并回填 `runs.replay_of`、`reports.coverage` 及查询索引，v3 建立维护这两列的触发器；v4 `calls.prompt_base` 与 v5 `prompt_blocks` / `calls.prompt_block` 让工具循环后续轮次只存新增消息、研究资料块按内容哈希只存一份；v6 建立 `calls.run_id`、`runs.status` 和 `sessions.expires_at` 高频索引；`db.call_prompt()` 逐字节还原，完整性巡检核对调用时记录的提示哈希。迁移只做加法，须另获授权并备份；回退旧镜像时保留新增结构，旧应用照常读写原字段并写完整提示，派生列由触发器继续维护（v4 镜像的巡检不认识 `prompt_block`，会误报哈希不符）。旧镜像写下的完整提示可用 `docker exec a-share-research-worker python -m a_share_research.db compact-calls --vacuum` 压缩：逐任务事务、提交前核对还原、可重复执行；改写生产数据，需当次授权。

Web/worker 由本仓库 `compose.yaml` 编排，共用 SQLite 数据目录，默认 `/home/lin/a-share-research-data`（可由 `DATA_DIR` 指定）：

- web：`.env.web` 配置，只监听 `127.0.0.1:8620`，经 `nginx-network` 对外服务；无模型和行情凭据。
- worker：`.env` 配置，挂载 `~/.pi`；`PI_RUNTIME` 留空时使用 pi 托管安装的 `current-version`，无外部端口；推理默认启用进程局部 Node.js 长驻守护（通过临时 Unix Domain Socket 通信，消除冷启动；`PI_DAEMON=0` 可回退为单次 CLI 启动）。

worker 启动时先离线预检 Node、Pi 安装目录、模型存储、所配置模型及本地凭据配置，成功后才连接数据库和领取任务。可在既有 worker 环境单独运行 `node a_share_research/pi_infer.mjs --check`；预检使用内存中的凭据与目录副本，不调用模型、不改写宿主存储。兼容缺省存储（使用内置模型或环境凭据）与带 BOM 的 JSON；文件损坏、模型不存在或没有可用凭据配置仍会阻断。失败时按日志修复 `PI_RUNTIME` / `install/current-version` 指向的安装、存储或凭据环境，再重启 worker；不自动切换模型或安装其他运行时。预检不验证远端凭据有效性与供应商连通性。

预检失败期间，价格任务与批次处理也会暂停，遗留的 `running` 任务保留原状。应先修复运行环境再恢复 worker；这些任务仍受发布空闲检查约束，不能改状态绕过检查。

发布复用以下固定入口。先按改动范围完成一次必要验证；同一应用源码与依赖已有通过结果时直接沿用，不因进入发布阶段再跑全集。纯文档更新只提交推送。当前版本、证据和回退只更新[实施计划](docs/implementation-plan.md#当前部署)，产品与能力文档不重复维护上线状态。

从已提交、已推送的 main 构建两份版本化镜像。Git 归档直接作为 Docker 上下文，无临时解压或权限修补脚本；源码可读权限在 Dockerfile 中固定：

```bash
set -o pipefail
release_code=$(git rev-parse --short HEAD)
git archive "$release_code" | docker build --target web -t "a-share-research-web:$release_code" -
git archive "$release_code" | docker build --target worker -t "a-share-research-worker:$release_code" -
```

按 [AGENTS.md 例行发布授权](AGENTS.md#授权)调用仓库内的发布工具；不再从历史证据目录复制脚本，也不重新编写临时 SQL：

```bash
uv run python -m a_share_research.deploy "$release_code" \
  "$HOME/.local/share/a-share-research-evaluations/deploy-$release_code-$(date +%Y%m%d-%H%M%S)"
```

工具只处理应用镜像切换：核对提交与镜像源码/读取权限、现有容器及 Compose 配置；以实际运行镜像建立 `rollback-$release_code` 标签；空闲检查（研究任务、批次与研究进展）→ 暂停受理并复查 → 备份巡检 → 切换 → 本机网页与 worker Pi 离线预检 → 完整性巡检。worker 预检结果保存为 `worker-preflight.json`，失败走同一应用回退流程。失败恢复原应用镜像，保留数据和证据；不自动恢复数据库或删除备份。无关的文档未提交改动不阻断发布，应用/部署文件未提交则拒绝。配置、凭据变更和非加法迁移需要单独安排回退，不使用此工具；只做加法、旧镜像可在新结构上照常运行的迁移，经当次授权后可走本工具，随新镜像启动执行，回退只换应用镜像（首例 `05d0c3f`）。

业务表指纹只在两服务停止的窗口内比较；恢复受理与后台后允许正常任务/估值写入，再以一致读事务做完整性巡检。`MODEL_ID` 与 `pi_infer.mjs` 的 `reasoning` 分别记录，模型未变时不追加模型配置检查或研究调用。

最后用真实浏览器验证受影响的页面和一个正常案例；沿用上文的私有浏览器会话，公共入口不重复用 urllib 探测（Cloudflare 曾返回 1010）。通过后记录版本、结果、备份与回退，结束发布；未通过就定位具体失败，不增加无依据的重试、检查矩阵或多轮审查。
