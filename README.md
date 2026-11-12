# a-share-research

收盘后的 A 股个股研究助手：输入一家公司和可选问题，AI 自主补读定期报告和临时公告、查询财务与程序计算，通常几分钟后读到带原文出处的研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

本项目生产有出处的公司判断并为判断记分，数据与确定性计算归 `core/`，账户事实归 `account/`，配置与持仓规则归 `policy/`；宿主通过统一 `a-share` Skill 和受控工具访问同一服务，旧账户库只读保留。定时与通知由 Hermes 承担。网页与研究 API 共用任务、原文、计算和追问/纠错；不直接修改账户或投资政策。

- 产品目标与使用流程：[PRODUCT.md](PRODUCT.md)；领域设计与研究方案：[产品方案](docs/proposal.md)；开发约束：[AGENTS.md](AGENTS.md)。
- 三仓合一重构（阶段与日期见[固定里程碑](docs/implementation-plan.md#固定里程碑)）：[方案](docs/consolidation.md)；现行能力与状态归属：[系统架构](ARCHITECTURE.md)。
- AI 能力演进原则与阶段日期：[路线图](docs/roadmap.md)。
- 候选发现与研究实验：[方案](docs/screening-experiment.md)，用法见下文[候选初筛](#候选初筛)。
- 基本面深度、经营序列与投资论证：[升级方案](docs/investment-upgrade.md)。
- 接续研究、检查点与归因复盘：[积累方案](docs/research-accumulation.md)。
- 当前批次与验收：[工作包与状态](docs/implementation-plan.md#工作包与状态)；生产版本与回退：[当前部署](docs/implementation-plan.md#当前部署)。

## 项目目录

| 路径 | 内容 |
|---|---|
| `a_share_research/` | `web.py` 鉴权/参数/渲染，`worker.py` 后台入口，`reading.py` 只读查询，`service.py` 提交；`research.py` 编排研究，`screening.py` / `backtest.py` 候选批次/回测；`contract.py` 判断契约，`report.py` 正文类型与视图，`market.py` TuShare 唯一网关（网页不加载行情依赖），`limits.py` 运行上限与依据；`core/` 并入的 a-stock-lib（Provider 与确定性计算，合同见 `core/PROVIDERS.md`）；`policy/v0/` 并入的旧 Skills 运行时（政策 v0，规则资料在 `policy/v0/guide/`；生产账户经受控工具接入，见[合一重构方案](docs/consolidation.md)） |
| `a_share_research/templates/`、`static/` | Jinja 页面、样式和渐进增强脚本 |
| `tests/`、`tests/data/`、`tests/support/` | 回归测试、真实采集样本、公共测试支撑；组织与验证范围见 [测试说明](tests/README.md)，样本随代码保留 |
| `docs/` | 领域方案、项目契约、路线图、实施与部署状态；产品目标见根目录 `PRODUCT.md`，视觉规范见 `DESIGN.md` |
| `data/` | 本地运行数据库、PDF 与提取缓存（不进 Git）；生产数据另由 `DATA_DIR` 指定 |
| `Dockerfile`、`compose.yaml`、`ops/hermes/` | Web / worker 构建部署；Hermes 定时脚本源与安装工具 |

`.pytest_cache/`、`.ruff_cache/` 和应用、测试目录下的 `__pycache__/` 是可重建缓存；`.playwright-mcp/` 是临时浏览器产物，正式验收证据归档后可清理。`data/`、`.env*`、真实测试样本和仓库外评估证据保留。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）、中文映射包 `poppler-data` 和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

```bash
uv run --env-file .env python -m a_share_research.research 600519 --question '最新财报改变了什么？'
uv run --env-file .env python -m a_share_research.worker            # 后台 worker：执行网页提交的研究与价格参考
uv run --env-file .env.web.dev uvicorn a_share_research.web:app --port 8610  # 网页（本机开发）
```

验证按影响范围选择测试文件（见[测试说明](tests/README.md)）；需要完整离线回归时用 `uv run pytest -q tests`。不以离线测试代替真实研究或浏览器验收。

`.env.web`（本机开发可用 `.env.web.dev`）只放网页配置：`PUBLIC_BASE_URL`、`SESSION_SECRET`（≥32 字符），以及 GitHub OAuth 的 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`；本机开发可用 `DEV_LOGIN=1`（仅限 loopback 地址）代替 OAuth。数据默认在 `./data`，生产用 `DATA_DIR` 指到代码目录之外。

初筛与回测的 TuShare 查询使用 `DATA_DIR/tushare_cache.db`（公司研究原有财务取数仍直连；新增外部条件工具使用同一缓存）：仅缓存 `trade_date` 或 `end_date` 早于上海当天日期的历史行情与财报响应；开放时间范围、当天及未来日期的查询和空结果不缓存。设置 `TUSHARE_CACHE=0` 可完全旁路缓存，直接查询 TuShare（不改写已有缓存）。历史披露若发生更正，需刷新对应缓存后再恢复缓存查询。

worker 重启时，带 `request_id` 的中断任务自动重新排队一次，重新获取干净材料并开始探索、成文和复核；再次中断则标记失败。恢复标记保存在 `runs.error`，`runs.request` 保持原样；命令行独立任务不参与自动恢复。

AI 或脚本做浏览器验收时不走 GitHub。使用当前源码的工具，在能访问数据目录的宿主签发短期浏览器会话；`--storage-state` 独占创建 0600 文件，不把令牌打印到工具输出：

```bash
release_browser_dir=$(mktemp -d /tmp/a-share-browser.XXXXXX)
DATA_DIR=/home/lin/a-share-research-data uv run --env-file .env.web python -m a_share_research.login_token \
  --minutes 15 --storage-state "$release_browser_dir/state.json"
```

本机开发改用开发库及 `.env.web.dev`。Playwright 直接使用 `browser.newContext({storageState: '/tmp/实际目录/state.json'})`；无需动态导入文件模块、打印令牌或手写会话 SQL。验收后通过页面“退出”撤销该会话，再删除本次临时文件。原有令牌输出和 `--agent` 接口仍保留，`--agent` 不与浏览器文件输出混用。

临时实例关闭后再整理证据。SQLite 使用 backup API 保存一致快照，不把使用中的数据库与 WAL/SHM 分别复制当作最终快照；截图、日志与试验输入归档到仓库外证据目录，核对文件哈希后删除本次临时副本。检查宿主开发进程时使用宿主进程视图，不能因沙箱内 `ps` 不可见就判断进程退出。

## 报告阅读

研究员可自主调用 `commodities(exchange)` 查 SHFE/DCE/CZCE/INE/GFEX 品种代码，`commodity(code)` 取主力连续收盘及 20/60/120 个交易日变化，`macro(series, count=12)` 取 `pmi/money/financing/lpr/shibor/cpi/ppi` 最近 1–24 期。原始 `X.*` 与计算 `C.*` 行随报告 table 保存，均可按 `{ref, period}` 引用，API 与网页同源；期货换月未复权，不能当作现货或投资收益。外部条件接口按上海日期缓存当日快照；LPR 上游每小时一次。新闻接口因账号权限暂不可用。

`industry_trend(code, level="l2")` 用当前申万成分合成近 20/60/120 交易日行业趋势（可选 l1/l2/l3）：等权个股复权收益中位数、起点流通市值加权收益（含剔除本公司后的同业口径）、上涨占比、中证800价格指数收益及本公司复权收益，差额通过 calculate 返回 C.*：加权口径相对中证800，本公司相对行业中位数和相对同业加权；中位数不与市值加权的中证800直接比较。正 PE_TTM 中位数及近五年 60 个已完成月份月末分位共用全市场 `daily/adj_factor/daily_basic` 历史缓存。按截至昨日的最新交易日取截面；当前成分回看存在幸存者偏差，缺失行情及非正/缺失估值不填零，覆盖名单、API/参数、交易日和原始输入随 X.* 保存并可复算；中证800价格指数不含分红再投资。冷缓存约 76 次调用，后续同日查询复用缓存；暂不提供行业间轮动排名。

新报告 `stance.action` 只取 现在分批 / 等价格 / 回避；现价与 v2 期望回报门槛价相差 ±5% 以内时页面标“边界立场”，`validation --stances` 单列“·边界”组（同材料重跑可能翻转，不作判断力证据）；现在分批可带 `add_trigger: {metric, period, op, value}`，下一期披露阈值只是加仓参考，不阻止首批。`stance_calc` 冻结该字段。历史 等证据/trigger 不迁移，仍可读、显示和由 `validation --stances` 记分。检查点状态与入场至今相对中证800全收益超额同时展示，完整口径见 [V4](docs/judgment-validation.md#v4-研究立场与等待代价2026-11-04)。

研究员可用 `peers` 发现申万行业内的公司，自主选择是否补取同行证据。通过报告的“财务与走势”章节可展开“已存同行快照对照”（有同行记录时显示）：按最近共同期间读取归母净利润、经营现金流和已有比率，显示口径与历史版本限制。浏览时不取数、补算或排名；详细边界见[步骤 1b](docs/investment-upgrade.md#76-步骤-1b同业发现与快照对照)，部署与验收见[实施计划](docs/implementation-plan.md#当前部署)。

## 投资政策

所有者登录后从导航进入 `/policy`，查看当前登记的政策身份、风险参数、适用账户范围、有效期和账户数据来源。完整参数及代码/规则/指南摘要可展开核对。页面区分尚未启用、旧指针未登记、记录损坏、运行版本变化和参数到期；账户来源单独展示，登记信息不替代监控时的账本与证据核验。

页面只读，不创建 API Key、不触发采集或研究；政策变更仍由所有者的本地入口完成。Bearer Key 不能代替网页所有者会话。功能发布状态见[实施计划](docs/implementation-plan.md)。

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

请求使用 `Content-Type: application/json`；可带 `X-Research-Version: research-v1`，未知版本返回 400。

| 路由 | 输入/结果 |
|---|---|
| `POST /api/research` | `{"code":"600519","question":"最新财报改变了什么？","request_id":"本次唯一编号"}` → 202 与 `run_id` |
| `GET /api/runs/{run_id}` | 排队/研究/成功/失败、步骤、错误和 `report_id`；成功状态为 `done` |
| `GET /api/request?request_id=...` | 只读找回原请求的任务、诊断与调用计数 |
| `GET /api/research/budget` | 模型调用上限（循环保护而非金额预算） |
| `GET /api/schema` | 正文 JSON Schema、判断契约版本与各视图字段清单（Bearer 鉴权） |
| `GET /api/reports?code=600519` | 最近 100 份报告的身份、时间、状态 |
| `GET /api/companies/{code}/card` | 最新通过复核的非纠错论点卡；`current.valuation` 按最新收盘日滚动测算 |
| `GET /api/cards` | `research-v1` 信封：`cards` 为扫描范围（订阅＋最近冻结批次 L5）的卡片列表，每张含 `origin`（`external`/`l5`）；无卡代码列入 `missing: [{code, origin}]` |
| `GET /api/search?q=集采&code=&section=&history=&page=` | 跨公司内容检索：空白分隔最多 5 个词（AND、字面匹配），默认只查每家公司最新一份通过复核的非纠错报告，`history=true` 查全部；章节 `risks/checkpoints/answer/business/financial/stance/prior_status`；返回 `items`（`report_id`、`code`、`name`、`created_at`、`period`、`location`、`text`）、`page`、`more`，每页 20 条。结果是待核查线索，不含结论词和事后表现 |
| `GET /api/reports/{report_id}` | `research-v1` 信封、核验结果、回答、来源页、表格及计算输入；GET 不刷新数据 |
| `GET /api/reports/{report_id}?compact=true` | 精简正文与来源目录（不含整页原文）、失败 `diagnostics`、`budget`、`continuation`；完整读取保持原合同。通过报告可带 `open_questions: [{id, where, problem}]`：复核认定用户问题尚未答完的部分（同时追加到 `gaps`），不能当作已回答 |
| `POST /api/research`（追问） | 上述输入加 `parent_report_id`，必须同公司，创建关联新任务 |
| `POST /api/reports/{id}/feedback` | `{"request_id":"新编号","location":"financial[0]","text":"具体问题与依据"}` → 核查任务 |

request/budget/compact 均沿用研究 API 鉴权，只执行读取，不做 schema 迁移；实际部署状态见[实施记录](docs/implementation-plan.md#当前部署)。正文顶层字段、compact / card / failed 各视图的字段清单与判断契约版本以 `GET /api/schema` 为准（定义在 `a_share_research/report.py`）。
单家与批量卡片共用数据库读取路径，沿用 Bearer 鉴权，不联网取数、不触发研究。订阅与 L5 重叠时 `origin` 为 `external`。
卡片 `current` 包含 `last_close`、`last_close_date`、`current_zone` 与 `valuation`。其中 `valuation` 按收盘日和收盘价重算，返回 `annual_returns`（保守/中性/乐观，比例值）、`support_below`、`stretched_above`、`inverted`、`zone`、`horizon`（年）、`as_of`、`close`；`current_zone` 与其 `zone` 相同。无收盘价时 `valuation` 为 null；未定价、缺字段、测算错误或收盘日到达/超过退出日时，`valuation` 仍是对象，其中各测算字段为 null 并给出 `reason`；区间倒挂时 `zone` 为 null。报告级 `valuation_calc` 保留原报告口径；带情景概率的报告另有 `valuation_calc.v2`（`rule`、`probabilities`、`annual_return`、`zone`、`support_below`），滚动后的 `current.valuation.v2` 另含 `support_below`/`stretched_above`，旧报告为 null。卡片另带 `stance`（`action` 为 现在分批/等价格/等证据/回避，及理由、出处，等证据另有 `trigger`）与程序冻结的 `stance_calc`（等价格的 `price`、`valid_until`；等证据的 `trigger`），旧报告两者为 null。
卡片还投影 `open_questions: [{id, where, problem}]`（无记录时为 `[]`，不代表用户全部要求均已完成）。冻结 `valuation_calc` 保留 `close`、`as_of`、`v2.support_below` 和 `boundary`；`current.valuation.boundary` 按滚动后的价格与门槛另行判定。两处 `boundary` 均复用同一 ±5% 规则，值为 true/false/null，null 表示无法定价或缺少依据。它们只在读取时派生，不修改原报告或冻结立场；false 也不代表已证明稳定。旧服务或旧快照缺字段不能当成无风险。边界与未答问题不改变通过状态或自动触发研究。
滚动测算冻结退出市值与累计分红，假设股本不变；报告日后已发放的分红仍计入，存在约一年派息量级的已知高估。`待复核` 卡片仍保留数值并提示区间暂停解读。
接续报告的 `report.prior_status`（`ref`、`status`、`text`、`cites`）逐项给出旧风险与旧检查点的状态，`ref` 对应 `report.prior.report_id` 那份报告的位置，旧文字见 `continuation.previous_risks`、`previous_checkpoints`；此前的接续报告没有该字段，状态在 `answer` 末尾。精简视图中的 `continuation` 区分生成日、财务覆盖期、行情日、披露与公告检索范围；`baseline_status` 明确区分可用、失败、未就绪、缺失等基线，并携带基线财报期、生成日、行情日和公告范围。表格差值复用网页同一 `reading.comparison` → `since_prior` 入口；`excluded/missing/reason` 保留单位、名称、旧格式和缺失值限制，不同累计期间不相减。精简视图额外要求两份报告均通过核查，否则不给 `diff`。失败时返回已有核查问题和缺口；异常中断尚未保存报告时仍读取已落盘材料中的缺口。全局预算为 `scope=configuration`，`model_calls_recorded=null`；有具体任务时为 `scope=run` 并携带 `run_id` 和该任务调用计数。

相同编号和内容返回原任务；编号相同但内容不同返回 409。不存在返回 404；未就绪或状态不一致返回 409；非法位置/跨公司返回 422；未认证或失效返回 401。`location` 是 `summary` 或 `answer/risks/business/financial/methods/checkpoints/prior_status` 的实际零基下标，例如 `financial[0]`。报告的 `passed` 与任务状态一起判断；失败报告不提供已通过的结论，可保留的事实范围见[产品方案](docs/proposal.md#2-从用户角度的取舍)。

财务表覆盖三表、现金流与债务；字段、单位和缺失值规则见[三表口径](docs/investment-upgrade.md#21-结构化三表扩展)。通过报告的 `report.series` 是只供阅读的经营序列，不属于可引用的 `evidence`。旧报告不回填新增指标，缺失不当作零。

材料与计算可在 `evidence` 中定位；`coverage.periods` 只列本公司财报期间，行情日期另见 `market_date`，`coverage.missing` 包含取证与报告声明的缺口。旧报告缺少的身份不回填。仅覆盖实际读取的披露和已取财务数据；`coverage.announcement_searches` 列出公告标题检索的关键词、日期范围及截断状态，不等于读完全部公告。临时公告沿用 PDF 页码、原文与内容哈希，公告日不冒充财报期间。

Skills 通过 `a-stock-cache research` 接入本 API，先复用旧报告，再按缺口发起追问；配置与使用见 [Skills 接入说明](https://github.com/on195594/a-stock-agent-skills/blob/master/skills/a-stock-research/references/research-service.md)，发布状态见实施计划。

纠错报告的 `methods` 是已核查的方法线索；`evidence.methods_used` 记录检索召回，不证明实际采用或收益贡献。针对 `methods[i]` 的成功核查可替换该检索项，空列表可撤下；失败不生效、原报告不改写。当前公司事实仍须重新取证，方法不会自动修改 Skill、代码、账户或政策；详见[反馈闭环](docs/roadmap.md#4-反馈如何转化为能力)。

### 外部 Agent 受控取证与提交

工具服务显式启用 `ASHARE_TOOLS_EXTERNAL_ENABLED=1` 后，research 与 assistant Key 可通过 `POST /tools/{name}` 调用下表；成功结果位于 `result.data`。入口复用同一服务端材料和核验工具，客户端不上传材料快照、PDF 原文或任意 URL。research Key 无账户权限；assistant 创建的研究始终保留 `account_aware=true`。owner Key 用于所有者操作，不能调用这些外部研究工具；操作和材料只属于创建它的 Key。

| 工具 | 参数与结果 |
|---|---|
| `external.open` | `{request_id,code}`，六位公司代码；首次公司无需历史报告即可排队取证，立即返回操作 `id` |
| `external.operation` | `{operation_id}`，读取 `queued/running/done/failed`、期限、错误类别、可用的 `error_detail` 和结果；成功返回 `external_id`、`material_revision` |
| `external.start` | `{request_id,source_run_id}`，复用已完成 worker 研究的服务端材料，直接创建材料版本 0 |
| `external.tools` | `{}`，读取允许的取证工具及参数 Schema；工具参数严格校验，最多 64 KiB |
| `external.acquire` | `{request_id,external_id,base_revision,tool,arguments}`，在指定材料版本上排队执行一次工具；成功追加材料版本 |
| `external.status` | `{external_id}`，读取当前材料版本、提交版本、未完成操作及历史提交回执 |
| `external.material` | `{external_id,material_revision}`，读取指定不可变材料；省略版本读取最新材料 |
| `external.submit` | `{external_id,expected_version,material_revision,draft}`，机械检查并冻结草稿与指定材料；`expected_version` 是提交版本，不是材料版本 |
| `external.review` | `{submission_id}`，实例另外启用 `ASHARE_TOOLS_EXTERNAL_REVIEW_ENABLED=1` 且不在月批保护期时，将通过机械检查的提交交独立复核 |

`open` 由默认 worker 调用现有 `material.gather`，`acquire` 调用目录中的披露检索、原文阅读、财务及程序计算等工具，两者不调用模型、不伪造研究任务。每次操作在受监督的独立子进程中运行，整次上限 1200 秒；HTTP 只入队和轮询，不等待采集完成。已持有的财务数据和披露目录可复用，原采集时间保留；检索新临时公告用 `announcements`，列出目录不等于读过原文。

同一 Key 的 `start/open/acquire` 共用 `request_id` 命名空间：相同操作与参数重试复用原研究或操作，相同编号改动作或参数返回 409。`start` 返回同一研究 ID 及其当前状态；`open/acquire` 返回同一操作记录，终态回执保持不变。超时、中断、撤销凭据等失败是终态，重试原编号不会重跑；明确决定再次采集时使用新编号。每个外部研究同时只能有一个未完成取证操作，期间新的取证或提交返回 409。取证前和材料入账前均复查 Key；撤销后不发布取证结果。

材料版本只追加，原文页、来源编号和计算行不会被后续取证改写。提交按“草稿哈希＋材料哈希”去重：同草稿绑定新证据产生新提交，指定原材料重试仍返回原回执。为兼容旧调用，省略 `material_revision` 的提交绑定版本 0；新调用应始终显式填写。独立复核只读该提交绑定的材料，后续取证不改变旧复核；`accepted=true` 仅表示机械检查通过，不代表独立复核通过或报告发布。

该链路要求数据库 v22；旧材料与提交迁移为材料版本 0。发布空闲检查包括取证操作，备份收录所有历史材料版本的 PDF。仅支持 v21 的回退镜像会被兼容检查拒绝，须准备支持 v22 的回退基线；这里说明代码接口，不代表生产已切换，部署与真实验收状态见[实施记录](docs/implementation-plan.md)。

## 账户净值工具

在启用账户工具的实例，以 assistant 凭据通过现有 `ashare` / HTTP / MCP 调用，参数结构以 `GET /tools` 返回的 schema 为准。研究凭据和 owner 凭据不能调用这三个账户工具；默认只读实例不开放它们。计算口径和边界见[合一方案的量尺](docs/consolidation.md#4-现有代码与知识的去向)，上线状态见[实施计划](docs/implementation-plan.md)。

1. `account.nav.capture({account_id})`：读取一致快照，返回 `source` 和 `state_sha256`。从中选起止资产观察/已入账快照及每笔有效出入金的流前观察；`kind` 为 `observation` 或 `batch`，每个引用包含 `id` 与选择理由 `reason`。
2. 通过 `account.evidence` 保存完整资金流水原件与指数响应。指数 JSON 为 `{schema_version:1, code:"H00906.CSI", currency:"CNY", return_type:"total_return", source:"tushare.index_daily", fetched_at:"含时区的实际采集时间", source_as_of:"YYYY-MM-DD", status:"ok", rows:[{date:"YYYY-MM-DD",close:"实际收盘值"},...]}`；保留原响应金额与时点，失败响应不能标记成 ok。
3. `account.nav.evaluate({account_id,request})`：`request` 包含 `expected_state_sha256`、`start/end` 估值引用、`flows:[{event_id,before:估值引用}]`、`benchmark_evidence_hash` 和 `coverage`。后者明确 `start/end` 时点、`currency:"CNY"`、`scope:"cash_equity_no_liabilities"`、`complete_external_flows:true`、`no_security_transfers:true`、`evidence_hashes` 与 `explanation`。这些声明必须由原件支持；没有出入金记录不能自动填 true。资料变更返回 409，缺项或口径不符返回 422。
4. 保存返回的 `artifact_sha256`。相同输入重试得到相同哈希；`account.nav.replay({account_id,artifact_sha256})` 重放原输入并核对原结果，即使后来发生更正也不改写历史。结果保存在 `account-evidence/`，随正式备份包保留；工具不修改账户、政策或旧双基准报告，不发送通知、不请求行情。

`account_return` 与 `benchmark_return` 为小数收益率，`excess_percentage_points` 为百分点；所有计算金额和比率用十进制字符串返回，`flows` 留存逐笔份额变化。完整性是带凭证的调用者声明，指数是上传的证据，重放不是券商/供应商认证，也不是当前账户状态或完整历史验收。请保留回执哈希以便检索，当前不提供净值历史列表或自动日报。

## 运行身份与材料重放

每次研究在 `runs.identity` 记录应用文件内容哈希、Python/Lib 版本和材料哈希，在 `calls.identity` 记录模型、pi/Node 版本、调用参数和提示词哈希；未知一律留空，不用当前版本回填。`runs.material` 保存实际送入流程的财务表、行情时点、文档身份和原文页。

方法目录保存在材料快照，`methods_sha256` 标识实际可用的目录；检索使用这一快照。普通研究按开始时间、批次研究按批次创建时间固定方法；`methods_used` 仍只表示检索召回。

`--replay` 复用该快照重新执行成文、检查、复核和修复（包括修正阶段都不开放取证工具、不重新探索、不取数），保存为新任务；快照缺失、版本不支持、哈希或公司不一致时失败。重放报告在网页上标明来源，首页最新报告和“今日已研究”判断排除重放。

报告（纠错除外）带 1–3 条下期检查点，其中至少 2 条带对下一期财务表数值的区间预测（`forecast`），披露后由判断记分命令兑现；以及情景测算输入（`valuation`：未来三年三情景利润、退出倍数、情景概率、派息率与有出处的依据，或 `not_priced`），价格区间与回报由程序算入 `valuation_calc`；研究立场 `stance` 的记分输入由程序冻结为 `stance_calc`，见[顾问闭环](#顾问闭环批次-a)；初筛批次对通过复核的候选统一执行 6 个月相对表现强制排序（`screening.rank`）构建半组模拟组合。主动研究同一家公司且未指定原报告时，自动带入最近一份通过复核、非重放、非纠错报告的风险与检查点（不带结论与摘要），要求在回答末尾说明各自本期状态；批次深挖不接续。规则与验收见[积累方案](docs/research-accumulation.md)。

```bash
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research          # 建基线（评估集）
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research --replay 1 # 按任务 ID 重放
```

完整性巡检（不写数据库内容，可在线运行，不联网，不修复；契约见[方案第 6 节](docs/proposal.md#6-状态与正确性)）。有问题时输出清单并以 1 退出，旧报告没有正文哈希只计数：

```bash
docker exec a-share-research-worker python -m a_share_research.integrity   # 生产（备份前、恢复后）
DATA_DIR=/tmp/research-eval uv run python -m a_share_research.integrity     # 本地
```

数据库与原始证据使用独立备份包保存，包含 SQLite 一致快照、PDF 原件和账户证据；清单记录文件哈希、数据库版本、序列及已提交证据引用，不包含可重建的 PDF 文本缓存或宿主凭据。缺少已引用原件、哈希不符或巡检失败时拒绝完成备份，失败请求中从未入账的缺失材料不计为已提交引用。

```bash
uv run python -m a_share_research.backup create /absolute/data/app.db /absolute/private/new-backup
uv run python -m a_share_research.backup verify /absolute/private/new-backup
uv run python -m a_share_research.backup restore /absolute/private/new-backup /absolute/private/new-recovery
```

默认从数据库同目录的 `pdf/`、`account-evidence/` 读取原件；副本验收可用 `--pdf-directory`、`--evidence-directory` 明确来源。目标目录必须不存在，目录/文件分别为 0700/0600；操作不会覆盖现库。恢复只是生成经校验的独立副本，不启动服务、不自动迁移、不抬升序列，也不代表允许丢弃备份之后的写入。实际数据恢复仍按[数据层回退步骤](docs/data-foundation.md#7-验收与治理规则)处理高水位与引用；合一切换后应用回退保持新账本为唯一事实来源，见[合一回退规则](docs/consolidation.md#7-回退与文档收敛)。

账户切换使用本地 owner 入口，要求 `ASHARE_OWNER_TOKEN` 已通过私有环境提供。按[合一退出条件](docs/consolidation.md)完成验收、停用并排空旧宿主与定时任务后，先运行末次镜像，核对资金/成本零版本期初、全部旧持仓及事件历史、账户周期分析与原始证据，再登记并启用匹配当前代码及账户范围的政策。正式切换日期和阶段状态见[实施计划](docs/implementation-plan.md)；命令只检查技术状态，不证明券商资金完整、五交易日对照通过或允许提前启用宿主。

```bash
DATA_DIR=/absolute/new-data uv run python -m a_share_research.account.mirror --source /absolute/old-state/cache.db
uv run python -m a_share_research.cutover --database /absolute/new-data/app.db --plan /absolute/private/cutover.json prepare \
  --source /absolute/old-state/cache.db --state-directory /absolute/old-state \
  --account-id confirmed-account --account-scope confirmed-scope
uv run python -m a_share_research.cutover --database /absolute/new-data/app.db --plan /absolute/private/cutover.json fence
uv run python -m a_share_research.cutover --database /absolute/new-data/app.db --plan /absolute/private/cutover.json commit
uv run python -m a_share_research.cutover --database /absolute/new-data/app.db --plan /absolute/private/cutover.json transfer-stop-state \
  --destination /absolute/private/new-stop/state.json
```

`prepare` 只读既有数据库，计划以 0600 新建于 0700 私有目录，不覆盖旧计划。`--state-directory` 必须是旧运行时实际提醒状态目录，可与旧库目录不同。若旧应用目录还存放备份、日志或发布记录，逐项追加 `--preserve-entry /absolute/old-state/entry`，明确保留每个顶层旁支；不递归读取或修改其内容和权限，也不证明旁支内写入者已停。未列明的新条目、缺失保留项、符号链接数据库、共享目录和 HOME 均拒绝，不能把账户文件列为保留项。新库和计划必须位于封锁目录之外。

`fence` 再核对镜像指针、源哈希、期初/分析摘要、提醒状态及生效政策，给旧业务表和迁移表安装持久写入拒绝触发器，排空 WAL、转为 DELETE 日志，并将账户文件改为 0400、专用目录改为 0500；不会改旧运行时源码。`commit` 必须证明完整封锁仍在，并在新库事务内比较更新镜像指针为明确的账户/范围绑定。不新增表、不迁移、不自动启用政策或服务；准备后数据、政策或文件身份变化即拒绝。期初或旧分析基线过期时须重新协调和准备，不改写冻结历史。

中途失败保持已有封锁，排空连接或修复原因后可重试；需要新准备时使用新的计划文件名。同一已绑定账户的 `commit` 重试只返回原绑定时间，不覆盖后续入账或政策；不同绑定被拒。重复回执不表示重新验收当前账本。切换成功后镜像入口拒绝运行，应用回退继续使用新账本，不能恢复旧写入口。权限与触发器用于防止旧进程误写，不防御属主主动改权限、重命名目录或删除触发器。

启用新 Hermes 止损任务前，以同一计划执行 `transfer-stop-state`；目标须为新客户端实际使用的状态路径（默认 `~/.local/state/ashare-stop-alert/state.json`，示例路径须相应调整）。命令重验 owner、已完成的相同账本绑定、旧封锁及旧提醒状态哈希，持有客户端锁后原子新建 0600 状态，原文件日期与档位逐字节保留。只有计划记录缺失且封锁后仍缺失时才初始化空对象，回执明确 `source_absent=true`；不补日期或档位。相同内容重试不写文件；目标已有不同内容、已升级格式或待查请求时拒绝覆盖。顾问/月批/记分沿用原状态路径，无需导入；后续安装与任务验收见 [Hermes 交接](ops/hermes/README.md)。命令不安装脚本、启用任务或发送通知。

价格参考支持沪深主板、创业板（300/301）和科创板（688），继续排除 ST、退市风险股及北交所；公式与旧 `price-reference-v2` 记录保持兼容。

## 候选初筛

网页“候选初筛”每月开一个批次（设计见[方案](docs/screening-experiment.md)）。worker 在没有排队的单家任务时推进批次：

1. 全市场确定性初筛（不调模型）：按交易日取股票池、估值、成交额和申万一级行业，粗筛后逐家取近 5 年 `fina_indicator`/`income`/`cashflow`/`fina_audit`，4 线程下旧单组约 20–30 分钟、新双组约 40–60 分钟；初筛期间单家研究排队。
2. 前 20 家（Q20）各自提交一次研究（7 天内已通过的报告直接沿用；网页或 API 新提交的研究排在这些批次研究之前），全部结束后经 pi 做一次跨公司比较，给出 5 家草案；再单独调用一次，把通过复核的候选按 6 个月相对表现强制排序（不影响选股，失败只记录原因）。
3. 批次页展示因子与行业分位、近 60 个交易日涨跌幅、利润跳升且市盈率低的提示、板块覆盖和缺口。模型草案生成即自动冻结，并标明未经人工确认；没有可选公司时才由所有者勾选。冻结后第一个交易日 17:00 后记录开盘入场价，之后更新 L5、Q5、Q20、排序前后两半（有排序时）与两个全收益基准的模拟收益。基准发布较晚时先入场、等待收益；只估值到两个基准共同已发布的交易日，未追到当日则每 30 分钟重试，已有同日收益不重算。

自 2026-12 批次起，新批次使用 `screen-v1.3`，同批增加独立的小市值组：30 亿 ≤ 总市值 <100 亿元、近 20 日日均成交额 ≥3000 万元，沿用相同财务资格与四因子，分池计算行业分位。S10 为前 10 家（单行业最多 2 家），在 Q20 深挖完成后按顺序研究（同样复用 7 天内有效报告），仍处于 `researching` 阶段；两组研究完成后分别比较，冻结 SL3（最多 3 家、单行业最多 1 家）；SQ3 为机械前 3 家（同样单行业最多 1 家，凑不满留现金）。相对 `screen-v1.2` 只对齐 SQ3 的行业约束，历史规则快照不改。大市值 Q20/Q5/L5 规则不变，两组互不挤占；小组无可选草案时可留空按现金计，不阻塞大组冻结。批次页分组展示候选、比较与最终名单；手动冻结表单的 `codes` 仍代表大组，小组使用 `small_codes`（可为空）。

新批次冻结时为 Q20/S10 逐家保存近 90 日卖方覆盖机构数及缓存实际覆盖的交易日数；只读已缓存的按日全市场 `report_rc`，不补查缺失日，页面明确显示无缓存、截断及其他缺口，零机构不能解读为完整覆盖下无人跟踪。SQ3/SL3 与大组同日入场、同费率及期限，另加中证 1000 全收益（`H00852.CSI`）辅助基准，S10 不单独生成模拟组合；旧批次继续原规则与原展示。SL3 先只作模拟，不进入披露扫描或 Skills 换仓候选。

月度自动发起（启用状态与时间/额度记录见[实施计划](docs/implementation-plan.md)）：Bearer API Key 可调用 `POST /api/batches/monthly`（`{"month":"2026-12"}`）入队当月批次，`GET /api/batches/monthly/2026-12` 只读查询当月首批状态。服务端仅接受上海当前自然月自动发起，旧月拒绝；同月已有任意状态批次（含失败）均不另建，网页手动重开不受此限；已入队但跨月仍未开始初筛的自动批次直接失败停止，不使用后来数据补跑。

**额度口径**：自动月批每月最多一批，7 天内已通过报告直接复用，不因研究或比较中断自动重跑付费阶段；按批次关联使用独立的单研究上限，数值与依据见 `a_share_research/limits.py`；取证为写作与复核预留调用，剩余额度不足时不启动新一轮修复（成文、可能的拒绝工具轮及最多两次复核），未解决问题照实保留。核算复用关联研究的 `calls` 与大组比较、小组比较、排序的 `attempts`，单列缺失 usage；usage 中的费用估算不是实际账单。这是应用层调用次数上限，不是美元硬上限或供应商底层请求数上限。

服务重启时，进行中的初筛记为失败（人工可重新开始本月批次），手动批次跨公司比较自动重试，自动月批中断比较则失败停止。只看初筛结果、不建批次：

```bash
uv run --env-file .env python -m a_share_research.screening
```

历史回测（只测确定性规则 Q20/Q5，按月末点时还原数据，接口返回缓存在 `--out` 下，结果为每批一个 JSON；方法与结论见[方案第 5 节](docs/screening-experiment.md#5-历史回测)）：

```bash
uv run --env-file .env python -m a_share_research.backtest --out DIR --start 201601 --end 202510
```

续跑时只跳过已包含当前全部到期收益窗口的批次；缺少到期的 6/12 个月收益时复用接口缓存重算，成功后原子替换结果，失败保留原文件。仅处理已有下月交易日行情的完整月份，当前月自动跳过。接口缓存仍按请求参数复用，不主动刷新已有历史响应；需要全新取数时使用新的输出目录。

### 判断记分

只读打开数据库，不调用模型、不写库；口径见[判断验证方案](docs/judgment-validation.md)。`DATA_DIR` 只用于 TuShare 缓存，指向临时目录即可，不要指向生产数据目录：

```bash
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db [--json OUT]              # V0 事后超额记分
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --forecasts [--json OUT]  # V1 预测记分
uv run python -m a_share_research.validation --database /path/to/app.db --forecasts --frozen --html review.html --json review.json  # 不联网的冻结预测复盘
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --hypotheses [--json OUT] # V3 H1 避坑 / H3 读得快
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --agenda [--json OUT]     # V5 议程状态与各状态组后续表现（没有入样报告时不访问行情）
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --stances [--json OUT]    # V4 研究立场兑现与等待代价
```

V1 可加 `--html review.html` 导出可直接打开的手机友好静态快照，`--json` 保留原字段并追加描述性宽度、误差和模型分组。`--frozen` 不访问行情/模型接口：未结束目标期待披露，已结束但未查询的明确列“未查询兑现”，不冒充已记分。正常 `--forecasts` 仍沿用原兑现查询和评分，基线冻结、不重复记分。快照不是线上实时复盘页，也不计算模拟组合或真实账户收益。

### 顾问闭环（批次 A）

设计与验收见[顾问闭环方案](docs/investment-advisor.md)。

- **情景测算**：通过报告带 `valuation_calc`（规则 `valuation-v1`，默认回报门槛 10%/3%；另有影子规则 `valuation-v2` 按情景概率加权给出期望年化与门槛价，A3 并行记分）：各情景年化回报、测算支持区/中性区/测算透支区、隐含盈利要求、敏感性、价格桥、5 年 PE 分位参照，以及测算完成后才取的卖方预测对照（TuShare `report_rc`，共享 61 秒间隔与默认每日 8 次预算，按覆盖情况标 `partial` / `unavailable`）。
- **卖方覆盖恢复**：worker 空闲时每次补一个缺失交易日，冷却与进度跨重启保存，不新增服务或 cron。成功空表与未抓取分列，空表定期复查；不会改写旧报告或冻结批次。查看最近续补进度及当前预算（不请求上游）：`docker exec a-share-research-worker python -m a_share_research.consensus_backfill`。字段含覆盖/缺失/空结果/可能截断日、最近检查与下次续补时间；首次完整回补须在现有预算内跨日积累。
- **论点卡**：`GET /api/companies/{code}/card`，最新一份通过复核的非纠错研究；有卡片之后的新披露且复核未通过时为 `待复核`，区间暂停解读。
- **订阅与扫描**（迁移 v7）：`PUT/GET /api/subscriptions` 维护外部订阅代码（Research 不存持仓标签），扫描范围另含最近冻结批次的 L5；`POST /api/scans` 排队一次扫描（同一时刻只有一个），`GET /api/scans/{id}` 返回事件、失败与 `notify`，`GET /api/scans/weekly` 返回周报数据。worker 只识别定期报告、业绩预告/快报与重大风险披露，按公告身份去重；公告日早于卡片完成日的标为已覆盖，其余触发接续研究（每个上海自然日最多 5 家，超出积压）。通知投递由 Hermes 脚本 `~/.hermes/scripts/cron/a-share-advisor.py`（`scan`/`weekly`，凭据在其 0600 `runtime.env`）负责。
- **可恢复的扫描提交**（迁移 v21）：`PUT /api/scan-requests/{request_id}` 返回 `{version, request_id, scan_id}`（202）。请求键须为 1–128 个 ASCII 字母、数字或 `._:-`，首字符为字母或数字；客户端须在发送前持久化该键。同一 Bearer 身份和键永久对应原扫描，即使扫描已完成或失败；不同新请求仍复用当前活动扫描。映射与扫描创建在同一事务提交，凭据只保存认证哈希身份。旧 `POST /api/scans` 保持原语义，不支持提交回执丢失后的幂等恢复；新客户端遇到旧服务 404 应保留待查请求，不能回退 POST。Hermes 新版源码已接入独立待查状态与整轮预算，已安装启用，详见[客户端恢复协议](ops/hermes/README.md#顾问请求恢复与执行预算)。
- **区间条件收益诊断**（只读，不调模型）：

```bash
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.zones --database /path/to/app.db [--json OUT]
```

## 部署

v21 增加 `scan_requests` 及扫描引用索引，不改已有扫描或研究记录。后续顾问客户端的回退基线必须同时支持 v21、新请求 API 和 tools 就绪；旧 v20 镜像不满足，应用回退检查会明确拒绝，保留停服/暂停现场。准备期新增接口不代表已满足首次生产迁移的发布准入；须先按下文验证兼容镜像，不能将旧代码能读取新增表当作完整回退证明。

`db.connect()` 以 `PRAGMA user_version` 按版本逐事务迁移：v1 幂等补齐历史列；v2 增加并回填 `runs.replay_of`、`reports.coverage` 及查询索引，v3 建立维护这两列的触发器；v4 `calls.prompt_base` 与 v5 `prompt_blocks` / `calls.prompt_block` 让工具循环后续轮次只存新增消息、研究资料块按内容哈希只存一份；v6 建立 `calls.run_id`、`runs.status` 和 `sessions.expires_at` 高频索引；`db.call_prompt()` 逐字节还原，完整性巡检核对调用时记录的提示哈希。v9 重建 `runs`、`reports`、`calls` 把大字段放到表尾，增加 `company_key` 等生成列，`runs`、`reports` 改为 `AUTOINCREMENT`，设计与门禁见[数据层治理方案](docs/data-foundation.md)。迁移须另获授权并备份，日常发布的准入条件是旧镜像能跑在新结构上，须用最终结构和实际回退镜像的读写实测；做不到的不能使用日常参数，须准备一次性迁移路径，v9→v22 入口见下文。回退旧镜像时保留新增结构，旧应用照常读写原字段并写完整提示，派生列由触发器继续维护（v4 镜像的巡检不认识 `prompt_block`，会误报哈希不符）。

Web/worker 由本仓库 `compose.yaml` 编排，共用 SQLite 数据目录，默认 `/home/lin/a-share-research-data`（可由 `DATA_DIR` 指定）：

- web：`.env.web` 配置，只监听 `127.0.0.1:8620`，经 `nginx-network` 对外服务；无模型和行情凭据。
- tools（账户工具已启用，研究排队开关状态见实施计划）：复用 worker 镜像，Compose `tools` profile；独立 `.env.tools`（模板 `.env.tools.example`，0600），只含行情凭据；宿主仅 127.0.0.1:8621，0.4 CPU，无 Pi 挂载。
- worker：`.env` 配置，挂载 `~/.pi`；`PI_RUNTIME` 留空时使用 pi 托管安装的 `current-version`，无外部端口；每次推理单独启动 Node（冷启动约 1.2 秒），不常驻进程。

主模型使用 `MODEL_PROVIDER`、`MODEL_ID`、`MODEL_REASONING`；独立复核可同时设置 `MODEL_REVIEW_PROVIDER` 与 `MODEL_REVIEW_ID`，仅 `review` 阶段使用该模型，其他阶段仍用主模型。`MODEL_REVIEW_REASONING` 未设置时继承主模型思考强度；未配置独立复核时沿用主模型。当前生产选择只在[实施记录](docs/implementation-plan.md#当前部署)维护。

worker 启动时先离线预检 Node、Pi 安装目录、模型存储、主模型及可选复核模型的本地凭据配置，成功后才连接数据库和领取任务。可在既有 worker 环境单独运行 `python -c 'import json; from a_share_research.model import preflight; print(json.dumps(preflight()))'`；预检使用内存中的凭据与目录副本，不调用模型、不改写宿主存储。扩展注册后等待本地目录恢复完成，再选择模型；目录刷新失败同样阻断。兼容缺省存储（使用内置模型或环境凭据）与带 BOM 的 JSON；文件损坏、模型不存在或没有可用凭据配置仍会阻断。失败时按日志修复 `PI_RUNTIME` / `install/current-version` 指向的安装、存储或凭据环境，再重启 worker；不自动切换模型或安装其他运行时。预检不验证远端凭据有效性与供应商连通性。

预检失败期间，价格任务与批次处理也会暂停，遗留的 `running` 任务保留原状。应先修复运行环境再恢复 worker；这些任务仍受发布空闲检查约束，不能改状态绕过检查。

发布复用以下固定入口。先按改动范围完成一次必要验证；同一应用源码与依赖已有通过结果时直接沿用，不因进入发布阶段再跑全集。纯文档更新只提交推送。当前版本、证据和回退只更新[实施计划](docs/implementation-plan.md#当前部署)，产品与能力文档不重复维护上线状态。

从已提交、已推送的 main 构建两份版本化镜像。Git 归档直接作为 Docker 上下文，无临时解压或权限修补脚本；源码可读权限在 Dockerfile 中固定：

发布工具要求目标提交同时等于 `HEAD` 与 `origin/main`。测试后若又提交了纯文档，以当前 HEAD 构建并发布；确认应用源码与依赖未变即可沿用原测试，不拿较早的应用提交号直接调用发布工具。构建和切换之间不再提交新版本。

```bash
set -o pipefail
release_code=$(git rev-parse --short HEAD)
git archive "$release_code" | docker build --target web -t "a-share-research-web:$release_code" -
git archive "$release_code" | docker build --target worker -t "a-share-research-worker:$release_code" -
```

按 [AGENTS.md 例行发布授权](AGENTS.md#授权)调用仓库内的发布工具（经当次授权有配置变更时追加 `--allow-config`）；不再从历史证据目录复制脚本，也不重新编写临时 SQL：

```bash
uv run python -m a_share_research.deploy "$release_code" \
  "$HOME/.local/share/a-share-research-evaluations/deploy-$release_code-$(date +%Y%m%d-%H%M%S)"
```

工具只处理应用镜像切换：核对提交与镜像源码/读取权限、现有容器及 Compose 配置；以实际运行镜像建立 `rollback-$release_code` 标签；空闲检查（研究任务、批次、扫描与外部取证）→ 暂停受理并复查 → 备份巡检 → 切换 → 本机网页与 worker Pi 离线预检 → 完整性巡检。worker 预检结果保存为 `worker-preflight.json`，失败走同一应用回退流程。成功后只保留最近 3 份发布备份（[K4](docs/research-accumulation.md#k4-存储保留针对-p1-后半)），并删除比当前镜像及最近两级 `rollback-*` 目标更早的 web/worker 镜像标签（较新但未发布的构建不动，删除失败只记入 `manifest.json` 的 `pruned_images`）；失败时仅在旧镜像通过读取兼容检查后恢复原应用，保留数据、证据、全部备份和镜像，不自动恢复数据库。无关的文档未提交改动不阻断发布，应用/部署文件未提交则拒绝。配置、凭据变更和旧镜像跑不了新结构的迁移需要单独安排回退，不使用此工具；旧镜像可在新结构上照常运行的迁移，经当次授权后可走本工具，随新镜像启动执行，回退只换应用镜像（首例 `05d0c3f`）。工具的表指纹比较都在迁移之前，不证明迁移后数据守恒，迁移发布须另外复查。

业务表指纹只在本次纳入的全部服务停止后比较；恢复受理与后台后允许正常任务/估值写入，再以一致读事务做完整性巡检。`MODEL_ID` 与 `MODEL_REASONING` 分别记录，模型与推理强度未变时不追加模型配置检查或研究调用。

发布备份保留原 `backup-pre-<提交>-<时间>.db` 入口，同时生成同名 `.bundle/` 完整证据包；两份数据库独立存储，文件哈希一致。清理旧备份时先核对包的完整性及其与 `.db` 的归属，再成对清理；发现额外文件、损坏或归属不符时保留并报告。发布失败仍保留所有备份，不自动恢复数据库。首次启用 tools 时追加 `--with-tools --allow-config`；已有 tools 容器自动纳入发布。部署先通过私有控制 socket 暂停新请求并排空，再停止服务、备份和切换；新进程读取持久暂停标记，数据库就绪、worker 预检、容器配置和完整性检查通过后才恢复 tools 受理。已被其他操作暂停的服务不会被本次发布接管。恢复受理的回执丢失时，回退前会重新排空，排空失败则不替换进程。切换后失败会先停止写入者，对当时数据库、PDF、账户证据和资产侧文件制作私有副本，再以原 web、worker 镜像分别运行禁网、只读的兼容检查；旧代码必须读懂实际账户来源、政策回执和采集上下文，不能仅凭数据库版本号或 `/ready` 判断。结果保存在发布证据目录的 `rollback-compatibility.json`，副本在 `rollback-probe/`。检查失败、超时或无法完成时保留当前镜像、停服及 tools 暂停状态，不删首次新增的 tools 容器、不恢复受理；须依据报告核对副本并准备兼容应用，不能用恢复旧数据库绕过。通过后才清理本次新增 tools 容器或让已有 tools 随 worker 回退，账本不回滚。此门禁只验证读取，正式账户切换前仍须独立验证前一级合并镜像能够读写新账本。

已启用政策服务时，镜像就绪与政策实装一致是两项检查：应用代码参与政策身份，升级或回退后 `policy.current.installed` 可能为 false，新计算会拒绝，已有请求仍可查回原回执。所有者须按[政策本地入口](a_share_research/tools/README.md#完整政策身份生效指针与固定输入重放影子入口)，用核对后的参数在目标镜像下登记身份并追加生效指针；回退同样适用。发布工具不代替所有者切换政策，也不改写既存回执与旧周期参数。

首次 v9→v22 发布使用同一入口的专用参数；仅适用于当前没有 tools、源库仍为 v9 的部署，不能用于重试已迁移但未完成的现场。v9 漏传此参数会在停服和更改镜像标签前拒绝。目标提交须为已推送的 HEAD，两组镜像都须先按各自提交构建并带 `a-share-research-{web,worker}:<提交>` 标签；恢复提交必须已完成新结构下的读写演练。合一退出条件、配置准备和账户切换日期仍按[实施计划](docs/implementation-plan.md)，命令本身不替代这些条件：

```sh
uv run --offline --no-sync python -m a_share_research.deploy <目标提交> <新的私有证据目录> \
  --with-tools --allow-config --initial-recovery <兼容恢复提交>
```

入口先校验两组镜像源码，固定其不可变 ID，对完整一致副本用目标镜像离线迁移并检查所有旧列（含生成列及序列）守恒、权限默认值和完整性，再用恢复镜像检查该副本；这一步失败不停止生产。准备通过后重新查空闲，暂停受理、停止服务并保留完整备份；目标镜像在没有服务写入者时迁移实际数据库，同样核对守恒，通过后才启动三服务。旧 v9 镜像不作为此次恢复目标。

实际迁移未通过核对时保持停服和暂停标记；已通过迁移但启动或恢复受理失败时，先确认在途请求排空，检查包含最新写入的完整副本，再使用指定兼容镜像恢复全部三服务。新 tools 保留，数据库、原始证据和请求回执不倒退；恢复后再次检查镜像/配置、严格就绪、worker 离线预检和领域完整性，通过后才恢复受理。恢复仍失败则保持暂停；排空无法确认时不替换仍在处理请求的服务。`recovery-result.json` 区分 recovered/held，读取兼容检查另存 `rollback-compatibility.json`；即使兼容恢复成功，本次目标发布仍以失败退出，须核对证据再处理，不能盲重跑首次参数。

首次入口保留全部准备证据、备份和镜像，不做日常清理；兼容镜像作为后续回退标签。成功或核实兼容恢复完成后，后续发布回到日常入口；已有 tools 会自动纳入。此入口不切换账户来源、不启用宿主定时任务、不代替所有者激活政策。

最后用真实浏览器验证受影响的页面和一个正常案例；沿用上文的私有浏览器会话，公共入口不重复用 urllib 探测（Cloudflare 曾返回 1010）。通过后记录版本、结果、备份与回退，结束发布；未通过就定位具体失败，不增加无依据的重试、检查矩阵或多轮审查。

定时脚本的唯一来源是 `ops/hermes/`，用 `python3 ops/hermes/install.py --check` 只读核对。统一脚本已安装；后续从已提交版本用 `python3 ops/hermes/install.py --apply` 同步宿主。发布工具不负责这一步，任务、宿主路径与切换核对见[脚本说明](ops/hermes/README.md)。
