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

| 路径 | 内容 |
|---|---|
| `a_share_research/` | 应用源码；`web.py` / `worker.py` 为服务入口，`research.py` 为单股研究，`screening.py` / `backtest.py` 为候选批次与历史回测，`backtest_experiment.py` 为离线归因 |
| `a_share_research/templates/`、`static/` | Jinja 页面、样式和渐进增强脚本 |
| `tests/`、`tests/data/` | 确定性与状态边界测试、真实采集样本；样本随代码保留 |
| `docs/` | 方案、项目契约、路线图、实施与部署状态；`PRODUCT.md` / `DESIGN.md` 是界面设计入口 |
| `data/` | 本地运行数据库、PDF 与提取缓存（不进 Git）；生产数据另由 `DATA_DIR` 指定 |
| `Dockerfile`、`compose.yaml` | Web / worker 构建与部署 |

`.pytest_cache/`、`.ruff_cache/` 和 `__pycache__/` 可重建；`.playwright-mcp/` 是临时浏览器产物，正式验收证据归档后可清理。`data/`、`.env*`、真实测试样本和仓库外评估证据保留。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

```bash
uv run --env-file .env python -m a_share_research.research 600519 --question '最新财报改变了什么？'
uv run --env-file .env python -m a_share_research.worker            # 后台 worker：执行网页提交的研究与价格参考
uv run --env-file .env.web.dev uvicorn a_share_research.web:app --port 8610  # 网页（本机开发）
uv run pytest -q tests
```

`.env.web`（本机开发可用 `.env.web.dev`）只放网页配置：`PUBLIC_BASE_URL`、`SESSION_SECRET`（≥32 字符），以及 GitHub OAuth 的 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`；本机开发可用 `DEV_LOGIN=1`（仅限 loopback 地址）代替 OAuth。数据默认在 `./data`，生产用 `DATA_DIR` 指到代码目录之外。

AI 或脚本做浏览器验收时不走 GitHub：在能访问数据目录的机器上签发一个短期会话，把输出作为 `session` cookie 注入浏览器，用完点“退出”即作废（不经 HTTP 暴露，能执行它即已有主人权限）：

```bash
uv run python -m a_share_research.login_token --minutes 30                                    # 本机开发实例
docker exec a-share-research-web python -m a_share_research.login_token --minutes 30         # 生产
```

## 报告阅读

当前源码在通过报告的“财务数据”后按需展开“已存同行快照对照”；无同行记录时不显示。仅复用报告已存的归母净利润、经营现金流和可精确映射的已有比率，逐家选择与主体的最近共同期间，不重新取数、补算或排名。

显式期间口径冲突或结构化不可用标记会阻断数值；缺期间元数据只沿用生成时契约。归母净利润非正时保留金额、隐藏比率并解释原因。历史版本未重新核对，顶部链接可查看本报告记录的缺口；不保证已覆盖后续更正。该视图为本地候选，发布状态见[实施计划](docs/implementation-plan.md#1b-只读数据盘点与本地候选)，详细准入与限制见[步骤 1b](docs/investment-upgrade.md#76-步骤-1b同业对照表后置)。

## Agent 研究 API

Skills 等长期调用方使用具名研究 API Key，持续有效直到撤销。owner 在目标实例的数据目录管理（容器内使用同样的 Python 模块）：

```bash
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys create skills-runtime
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys list
DATA_DIR=/path/to/data uv run python -m a_share_research.api_keys revoke <Key编号>
```

`create` 只输出一次明文，存入调用方 0600 的私有 `runtime.env`：`A_STOCK_RESEARCH_URL` 指向服务，`A_STOCK_RESEARCH_TOKEN` 保存 Key；不进入聊天、日志、命令参数或 Git。服务端只存哈希，`list` 只显示编号、名称与创建时间。管理命令仅由 owner 本地执行；Key 只授予研究 API 权限。

轮换：创建新 Key → 更新配置并验证读取 → 按编号撤销旧 Key。同名 Key 可并存；环境变量优先于文件，须排除旧值覆盖。Key 不受服务重启或浏览器登录/退出影响。401 后先修复凭据，再沿用原 `request_id` 重试。

临时验收仍可用 `python -m a_share_research.login_token --agent --minutes 60`（1–480 分钟，不续期）。两种凭据均通过 `Authorization: Bearer ...` 发送，与浏览器 cookie 隔离；`POST /api/session/revoke` 仅撤销当前凭据。API 无需模型或行情凭据。

请求使用 `Content-Type: application/json`；可带 `X-Research-Version: research-v1`，未知版本返回 400。升级只新增 `api_keys` 表；旧服务不识别长期 Key，回退流程见[当前部署](docs/implementation-plan.md#当前部署)。

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

`--replay` 复用该快照重新执行成文、检查、复核和修复（不重新探索、不取数），保存为新任务；快照缺失、版本不支持、哈希或公司不一致时失败。重放报告在网页上标明来源，首页最新报告和“今日已研究”判断排除重放。

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

Web/worker 由本仓库 `compose.yaml` 编排，共用 SQLite 数据目录，默认 `/home/lin/a-share-research-data`（可由 `DATA_DIR` 指定）：

- web：`.env.web` 配置，只监听 `127.0.0.1:8620`，经 `nginx-network` 对外服务；无模型和行情凭据。
- worker：`.env` 配置，挂载 `~/.pi`；`PI_RUNTIME` 留空时使用 pi 托管安装的 `current-version`，无外部端口。

经当次上线授权后，从已提交、已推送的 main 构建并核对镜像。确认研究任务和批次空闲，暂停受理后复查、备份与巡检，再启动候选；验证主流程后记录版本与回退依据。构建和切换分开执行：

```bash
docker compose build
docker compose up -d --no-build --no-deps web worker
```

这两条命令不代替空闲检查、备份或验收；具体版本、证据与回退见[当前部署](docs/implementation-plan.md#当前部署)。开发工作树改动不会自动影响运行中的镜像。
