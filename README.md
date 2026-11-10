# a-share-research

收盘后的 A 股个股研究助手：输入一家公司和可选问题，AI 自主补读定期报告和临时公告、查询财务与程序计算，通常几分钟后读到带原文出处的研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

本项目负责公司财报研究与阅读；`a-stock-agent-skills` 负责 Agent 投研、持仓与风险，两者独立运行并共用 `a-stock-lib`。网页与研究 API 共用任务、原文、计算和追问/纠错；不直接修改账户或投资政策。

- 产品目标与使用流程：[PRODUCT.md](PRODUCT.md)；领域设计与研究方案：[产品方案](docs/proposal.md)；开发约束：[AGENTS.md](AGENTS.md)。
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
| `a_share_research/` | 应用源码；`web.py` / `worker.py` 为服务入口，`research.py` 编排单股研究（取材料 `material.py`、机械核对 `checking.py`、成文契约 `prompts.py`、取证工具 `research_tools.py`），`screening.py` / `backtest.py` 为候选批次与历史回测 |
| `a_share_research/templates/`、`static/` | Jinja 页面、样式和渐进增强脚本 |
| `tests/`、`tests/data/`、`tests/support/` | 回归测试、真实采集样本、公共测试支撑；组织与验证范围见 [测试说明](tests/README.md)，样本随代码保留 |
| `docs/` | 领域方案、项目契约、路线图、实施与部署状态；产品目标见根目录 `PRODUCT.md`，视觉规范见 `DESIGN.md` |
| `data/` | 本地运行数据库、PDF 与提取缓存（不进 Git）；生产数据另由 `DATA_DIR` 指定 |
| `Dockerfile`、`compose.yaml` | Web / worker 构建与部署 |

`.pytest_cache/` 和应用、测试目录下的 `__pycache__/` 可重建；`.playwright-mcp/` 是临时浏览器产物，正式验收证据归档后可清理。`data/`、`.env*`、真实测试样本和仓库外评估证据保留。

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
| `GET /api/reports?code=600519` | 最近 100 份报告的身份、时间、状态 |
| `GET /api/companies/{code}/card` | 最新通过复核的非纠错论点卡；`current.valuation` 按最新收盘日滚动测算 |
| `GET /api/cards` | `research-v1` 信封：`cards` 为扫描范围（订阅＋最近冻结批次 L5）的卡片列表，每张含 `origin`（`external`/`l5`）；无卡代码列入 `missing: [{code, origin}]` |
| `GET /api/search?q=集采&code=&section=&history=&page=` | 跨公司内容检索：空白分隔最多 5 个词（AND、字面匹配），默认只查每家公司最新一份通过复核的非纠错报告，`history=true` 查全部；章节 `risks/checkpoints/answer/business/financial/stance/prior_status`；返回 `items`（`report_id`、`code`、`name`、`created_at`、`period`、`location`、`text`）、`page`、`more`，每页 20 条。结果是待核查线索，不含结论词和事后表现 |
| `GET /api/reports/{report_id}` | `research-v1` 信封、核验结果、回答、来源页、表格及计算输入；GET 不刷新数据 |
| `GET /api/reports/{report_id}?compact=true` | 精简正文与来源目录（不含整页原文）、失败 `diagnostics`、`budget`、`continuation`；完整读取保持原合同。通过报告可带 `open_questions: [{id, where, problem}]`：复核认定用户问题尚未答完的部分（同时追加到 `gaps`），不能当作已回答 |
| `POST /api/research`（追问） | 上述输入加 `parent_report_id`，必须同公司，创建关联新任务 |
| `POST /api/reports/{id}/feedback` | `{"request_id":"新编号","location":"financial[0]","text":"具体问题与依据"}` → 核查任务 |

request/budget/compact 均沿用研究 API 鉴权，只执行读取，不做 schema 迁移；实际部署状态见[实施记录](docs/implementation-plan.md#当前部署)。
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

研究库的“研究改进与方法核查”进入 `/improvements`；`GET /api/improvements` 返回最近 30 项，`GET /api/improvements/{run_id}` 返回同源详情，沿用 Bearer 鉴权。内容包括来源缺口、冻结判据、真实对照任务、独立核查、采用／拒绝／暂缓理由及调用用量。诊断任务与报告带 `kind`／`purpose=diagnostic`，不进入正式报告查询与记分。规划与计划核查只能按冻结报告 ID 读取已存证据；案例仍执行原研究与复核流程。后台仅在正式研究、扫描及批次空闲时推进，无新来源不重复调用；完整契约见[研究积累方案](docs/research-accumulation.md#6-受控演进方案让积累服务于赚钱)。

## 运行身份与材料重放

每次研究在 `runs.identity` 记录应用文件内容哈希、Python/Lib 版本和材料哈希，在 `calls.identity` 记录模型、pi/Node 版本、调用参数和提示词哈希；未知一律留空，不用当前版本回填。`runs.material` 保存实际送入流程的财务表、行情时点、文档身份和原文页。

方法目录保存在材料快照，`methods_sha256` 标识实际可用的目录；检索与方法提示都使用这一快照。普通研究按开始时间、批次研究按批次创建时间固定方法，新采用的方法不进入在途批次。实验候选与正式采用使用相同方法提示载体；`methods_used` 仍只表示检索召回，案例中的实际采用由独立核查记录。

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

价格参考支持沪深主板、创业板（300/301）和科创板（688），继续排除 ST、退市风险股及北交所；公式与旧 `price-reference-v2` 记录保持兼容。

## 候选初筛

网页“候选初筛”每月开一个批次（设计见[方案](docs/screening-experiment.md)）。worker 在没有排队的单家任务时推进批次：

1. 全市场确定性初筛（不调模型）：按交易日取股票池、估值、成交额和申万一级行业，粗筛后逐家取近 5 年 `fina_indicator`/`income`/`cashflow`/`fina_audit`，4 线程下旧单组约 20–30 分钟、新双组约 40–60 分钟；初筛期间单家研究排队。
2. 前 20 家（Q20）各自提交一次研究（7 天内已通过的报告直接沿用；网页或 API 新提交的研究排在这些批次研究之前），全部结束后经 pi 做一次跨公司比较，给出 5 家草案；再单独调用一次，把通过复核的候选按 6 个月相对表现强制排序（不影响选股，失败只记录原因）。
3. 批次页展示因子与行业分位、近 60 个交易日涨跌幅、利润跳升且市盈率低的提示、板块覆盖和缺口。模型草案生成即自动冻结，并标明未经人工确认；没有可选公司时才由所有者勾选。冻结后第一个交易日 17:00 后记录开盘入场价，之后更新 L5、Q5、Q20、排序前后两半（有排序时）与两个全收益基准的模拟收益。基准发布较晚时先入场、等待收益；只估值到两个基准共同已发布的交易日，未追到当日则每 30 分钟重试，已有同日收益不重算。

自 2026-12 批次起，新批次使用 `screen-v1.3`，同批增加独立的小市值组：30 亿 ≤ 总市值 <100 亿元、近 20 日日均成交额 ≥3000 万元，沿用相同财务资格与四因子，分池计算行业分位。S10 为前 10 家（单行业最多 2 家），在 Q20 深挖完成后按顺序研究（同样复用 7 天内有效报告），仍处于 `researching` 阶段；两组研究完成后分别比较，冻结 SL3（最多 3 家、单行业最多 1 家）；SQ3 为机械前 3 家（同样单行业最多 1 家，凑不满留现金）。相对 `screen-v1.2` 只对齐 SQ3 的行业约束，历史规则快照不改。大市值 Q20/Q5/L5 规则不变，两组互不挤占；小组无可选草案时可留空按现金计，不阻塞大组冻结。批次页分组展示候选、比较与最终名单；手动冻结表单的 `codes` 仍代表大组，小组使用 `small_codes`（可为空）。

新批次冻结时为 Q20/S10 逐家保存近 90 日卖方覆盖机构数及缓存实际覆盖的交易日数；只读已缓存的按日全市场 `report_rc`，不补查缺失日，页面明确显示无缓存、截断及其他缺口，零机构不能解读为完整覆盖下无人跟踪。SQ3/SL3 与大组同日入场、同费率及期限，另加中证 1000 全收益（`H00852.CSI`）辅助基准，S10 不单独生成模拟组合；旧批次继续原规则与原展示。SL3 先只作模拟，不进入披露扫描或 Skills 换仓候选。

月度自动发起（启用状态与时间/额度记录见[实施计划](docs/implementation-plan.md)）：Bearer API Key 可调用 `POST /api/batches/monthly`（`{"month":"2026-12"}`）入队当月批次，`GET /api/batches/monthly/2026-12` 只读查询当月首批状态。服务端仅接受上海当前自然月自动发起，旧月拒绝；同月已有任意状态批次（含失败）均不另建，网页手动重开不受此限；已入队但跨月仍未开始初筛的自动批次直接失败停止，不使用后来数据补跑。

**额度口径**：自动月批每月最多一批（Q20 + S10 最多 30 家新研究 × 每家最多 21 次调用 + 大/小组比较与排序最多 6 次 = 最多 636 次应用层模型调用；7 天内已通过报告直接复用），且不因研究或比较中断自动重跑付费阶段。自动月批按批次关联使用独立的单研究 21 次上限，其他研究仍为 30 次；取证为写作与复核预留调用，剩余不足 4 次时不启动新一轮修复（成文、可能的拒绝工具轮及最多两次复核），未解决问题照实保留。核算复用关联研究的 `calls` 与大组比较、小组比较、排序的 `attempts`，单列缺失 usage；usage 中的费用估算不是实际账单。这是应用层调用次数上限，不是美元硬上限或供应商底层请求数上限。

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
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --hypotheses [--json OUT] # V3 H1 避坑 / H3 读得快
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --agenda [--json OUT]     # V5 议程状态与各状态组后续表现（没有入样报告时不访问行情）
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.validation --database /path/to/app.db --stances [--json OUT]    # V4 研究立场兑现与等待代价
```

### 顾问闭环（批次 A）

设计与验收见[顾问闭环方案](docs/investment-advisor.md)。

- **情景测算**：通过报告带 `valuation_calc`（规则 `valuation-v1`，默认回报门槛 10%/3%；另有影子规则 `valuation-v2` 按情景概率加权给出期望年化与门槛价，A3 并行记分）：各情景年化回报、测算支持区/中性区/测算透支区、隐含盈利要求、敏感性、价格桥、5 年 PE 分位参照，以及测算完成后才取的卖方预测对照（TuShare `report_rc`，共享 61 秒间隔与默认每日 8 次预算，按覆盖情况标 `partial` / `unavailable`）。
- **卖方覆盖恢复**：worker 空闲时每次补一个缺失交易日，冷却与进度跨重启保存，不新增服务或 cron。成功空表与未抓取分列，空表定期复查；不会改写旧报告或冻结批次。查看最近续补进度及当前预算（不请求上游）：`docker exec a-share-research-worker python -m a_share_research.consensus_backfill`。字段含覆盖/缺失/空结果/可能截断日、最近检查与下次续补时间；首次完整回补须在现有预算内跨日积累。
- **论点卡**：`GET /api/companies/{code}/card`，最新一份通过复核的非纠错研究；有卡片之后的新披露且复核未通过时为 `待复核`，区间暂停解读。
- **订阅与扫描**（迁移 v7）：`PUT/GET /api/subscriptions` 维护外部订阅代码（Research 不存持仓标签），扫描范围另含最近冻结批次的 L5；`POST /api/scans` 排队一次扫描（同一时刻只有一个），`GET /api/scans/{id}` 返回事件、失败与 `notify`，`GET /api/scans/weekly` 返回周报数据。worker 只识别定期报告、业绩预告/快报与重大风险披露，按公告身份去重；公告日早于卡片完成日的标为已覆盖，其余触发接续研究（每个上海自然日最多 5 家，超出积压）。通知投递由 Hermes 脚本 `~/.hermes/scripts/cron/a-share-advisor.py`（`scan`/`weekly`，凭据在其 0600 `runtime.env`）负责。
- **区间条件收益诊断**（只读，不调模型）：

```bash
DATA_DIR=$TMP uv run --env-file .env python -m a_share_research.zones --database /path/to/app.db [--json OUT]
```

## 部署

`db.connect()` 以 `PRAGMA user_version` 按版本逐事务迁移：v1 幂等补齐历史列；v2 增加并回填 `runs.replay_of`、`reports.coverage` 及查询索引，v3 建立维护这两列的触发器；v4 `calls.prompt_base` 与 v5 `prompt_blocks` / `calls.prompt_block` 让工具循环后续轮次只存新增消息、研究资料块按内容哈希只存一份；v6 建立 `calls.run_id`、`runs.status` 和 `sessions.expires_at` 高频索引；`db.call_prompt()` 逐字节还原，完整性巡检核对调用时记录的提示哈希。v9 重建 `runs`、`reports`、`calls` 把大字段放到表尾，增加 `company_key` 等生成列，`runs`、`reports` 改为 `AUTOINCREMENT`，设计与门禁见[数据层治理方案](docs/data-foundation.md)。迁移须另获授权并备份，准入条件是旧镜像能跑在新结构上，须用最终结构和实际回退镜像的读写实测；做不到的不走发布工具，另写一次性步骤。回退旧镜像时保留新增结构，旧应用照常读写原字段并写完整提示，派生列由触发器继续维护（v4 镜像的巡检不认识 `prompt_block`，会误报哈希不符）。

Web/worker 由本仓库 `compose.yaml` 编排，共用 SQLite 数据目录，默认 `/home/lin/a-share-research-data`（可由 `DATA_DIR` 指定）：

- web：`.env.web` 配置，只监听 `127.0.0.1:8620`，经 `nginx-network` 对外服务；无模型和行情凭据。
- worker：`.env` 配置，挂载 `~/.pi`；`PI_RUNTIME` 留空时使用 pi 托管安装的 `current-version`，无外部端口；每次推理单独启动 Node（冷启动约 1.2 秒），不常驻进程。

主模型使用 `MODEL_PROVIDER`、`MODEL_ID`、`MODEL_REASONING`；独立复核可同时设置 `MODEL_REVIEW_PROVIDER` 与 `MODEL_REVIEW_ID`，仅 `review` 阶段使用该模型，其他阶段仍用主模型。`MODEL_REVIEW_REASONING` 未设置时继承主模型思考强度；未配置独立复核时沿用主模型。当前生产选择只在[实施记录](docs/implementation-plan.md#当前部署)维护。

worker 启动时先离线预检 Node、Pi 安装目录、模型存储、主模型及可选复核模型的本地凭据配置，成功后才连接数据库和领取任务。可在既有 worker 环境单独运行 `python -c 'import json; from a_share_research.model import preflight; print(json.dumps(preflight()))'`；预检使用内存中的凭据与目录副本，不调用模型、不改写宿主存储。兼容缺省存储（使用内置模型或环境凭据）与带 BOM 的 JSON；文件损坏、模型不存在或没有可用凭据配置仍会阻断。失败时按日志修复 `PI_RUNTIME` / `install/current-version` 指向的安装、存储或凭据环境，再重启 worker；不自动切换模型或安装其他运行时。预检不验证远端凭据有效性与供应商连通性。

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

工具只处理应用镜像切换：核对提交与镜像源码/读取权限、现有容器及 Compose 配置；以实际运行镜像建立 `rollback-$release_code` 标签；空闲检查（研究任务、批次与扫描）→ 暂停受理并复查 → 备份巡检 → 切换 → 本机网页与 worker Pi 离线预检 → 完整性巡检。worker 预检结果保存为 `worker-preflight.json`，失败走同一应用回退流程。成功后只保留最近 3 份发布备份（[K4](docs/research-accumulation.md#k4-存储保留针对-p1-后半)），并删除比当前镜像及最近两级 `rollback-*` 目标更早的 web/worker 镜像标签（较新但未发布的构建不动，删除失败只记入 `manifest.json` 的 `pruned_images`）；失败恢复原应用镜像，保留数据、证据、全部备份和镜像，不自动恢复数据库。无关的文档未提交改动不阻断发布，应用/部署文件未提交则拒绝。配置、凭据变更和旧镜像跑不了新结构的迁移需要单独安排回退，不使用此工具；旧镜像可在新结构上照常运行的迁移，经当次授权后可走本工具，随新镜像启动执行，回退只换应用镜像（首例 `05d0c3f`）。工具的表指纹比较都在迁移之前，不证明迁移后数据守恒，迁移发布须另外复查。

业务表指纹只在两服务停止的窗口内比较；恢复受理与后台后允许正常任务/估值写入，再以一致读事务做完整性巡检。`MODEL_ID` 与 `MODEL_REASONING` 分别记录，模型与推理强度未变时不追加模型配置检查或研究调用。

最后用真实浏览器验证受影响的页面和一个正常案例；沿用上文的私有浏览器会话，公共入口不重复用 urllib 探测（Cloudflare 曾返回 1010）。通过后记录版本、结果、备份与回退，结束发布；未通过就定位具体失败，不增加无依据的重试、检查矩阵或多轮审查。
