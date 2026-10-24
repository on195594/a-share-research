# a-share-research

收盘后的 A 股个股研究助手：输入一家公司和可选问题，AI 自主补读定期报告和临时公告、查询财务与程序计算，通常几分钟后读到带原文出处的研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

本项目负责公司财报研究与阅读；`a-stock-agent-skills` 负责 Agent 投研、持仓与风险，两者独立运行并共用 `a-stock-lib`。网页与研究 API 共用任务、原文、计算和追问/纠错；不直接修改账户或投资政策。

- 产品方向与现有流程：[重构方案](docs/proposal.md)；开发约束：[AGENTS.md](AGENTS.md)。
- 三项目职责与协作：[项目契约](docs/project-contract.md)。
- AI 能力演进与进入条件：[路线图](docs/roadmap.md)。
- 候选发现与研究实验：[方案](docs/screening-experiment.md)，用法见下文[候选初筛](#候选初筛)。
- 当前批次与验收：[工作包与状态](docs/implementation-plan.md#工作包与状态)；生产版本与回退：[当前部署](docs/implementation-plan.md#当前部署)。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

```bash
uv run --env-file .env python -m a_share_research.research 600519   # 命令行研究一家（不带代码则跑评估集）
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

## Agent 研究 API

在目标实例的数据目录签发研究专用令牌（默认 60 分钟、最长 480 分钟）：

```bash
DATA_DIR=/path/to/data uv run python -m a_share_research.login_token --agent --minutes 60
```

令牌仅用于 `Authorization: Bearer ...`，不能用作浏览器 cookie；浏览器会话也不能访问 API。`POST /api/session/revoke` 撤销当前令牌。API 不需要模型或行情凭据。下面的请求使用 `Content-Type: application/json`，可带 `X-Research-Version: research-v1`；未知版本返回 400。

| 路由 | 输入/结果 |
|---|---|
| `POST /api/research` | `{"code":"600519","question":"最新财报改变了什么？","request_id":"本次唯一编号"}` → 202 与 `run_id` |
| `GET /api/runs/{run_id}` | 排队/研究/成功/失败、步骤、错误和 `report_id`；成功状态为 `done` |
| `GET /api/reports?code=600519` | 最近 100 份报告的身份、时间、状态 |
| `GET /api/reports/{report_id}` | `research-v1` 信封、核验结果、回答、来源页、表格及计算输入；GET 不刷新数据 |
| `POST /api/research`（追问） | 上述输入加 `parent_report_id`，必须同公司，创建关联新任务 |
| `POST /api/reports/{id}/feedback` | `{"request_id":"新编号","location":"financial[0]","text":"具体问题与依据"}` → 核查任务 |

相同编号和内容返回原任务；编号相同但内容不同返回 409。不存在返回 404；未就绪或状态不一致返回 409；非法位置/跨公司返回 422；未认证或失效返回 401。`location` 是 `summary` 或 `answer/risks/business/financial` 的实际零基下标。报告的 `passed` 与任务状态一起判断，失败只返回核验事实，不能当成已通过的结论。

材料与计算可在 `evidence` 中定位；`coverage.periods` 只列本公司财报期间，行情日期另见 `market_date`，`coverage.missing` 包含取证与报告声明的缺口。旧报告缺少的身份不回填。仅覆盖实际读取的披露和已取财务数据；`coverage.announcement_searches` 列出公告标题检索的关键词、日期范围及截断状态，不等于读完全部公告。临时公告沿用 PDF 页码、原文与内容哈希，公告日不冒充财报期间。

Skills 候选通过 `a-stock-cache research` 接入本 API，先复用旧报告，再按缺口发起追问；配置与使用见 [Skills 接入说明](https://github.com/on195594/a-stock-agent-skills/blob/master/skills/a-stock-research/references/research-service.md)，发布状态见实施计划。

纠错报告的 `methods` 保存有引证、经本轮复核的方法；worker 可按问题关键词跨公司检索，原出处和参考记录随 `evidence.methods_used` 保存。它们只是待验证线索，当前事实仍需重新取证。针对 `methods[i]` 的新核查通过后，其方法结果替代该条历史检索项；返回空列表表示不再保留该方法。原报告不改写，失败核查不影响旧方法，也不自动修改 Skill、代码、账户或投资政策。

CLI 同样支持问题：

```bash
uv run --env-file .env python -m a_share_research.research 600519 --question '最新财报改变了什么，利润与现金流是否一致？'
```

## 运行身份与材料重放

每次研究在 `runs.identity` 记录应用文件内容哈希、Python/Lib 版本和材料哈希，在 `calls.identity` 记录模型、pi/Node 版本、调用参数和提示词哈希；未知一律留空，不用当前版本回填。`runs.material` 保存实际送入流程的财务表、行情时点、文档身份和原文页。

`--replay` 复用该快照重新执行成文、检查、复核和修复（不重新探索、不取数），保存为新任务；快照缺失、版本不支持、哈希或公司不一致时失败。重放报告在网页上标明来源，首页最新报告和“今日已研究”判断排除重放。

```bash
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research          # 建基线（评估集）
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research --replay 1 # 按任务 ID 重放
```

完整性巡检（不写数据库内容，可在线运行，不联网，不修复；契约见[方案第 6 节](docs/proposal.md#6-状态与正确性)）。有问题时输出清单并以 1 退出，旧报告没有正文哈希只计数：

```bash
docker exec a-share-research-worker python -m a_share_research.integrity   # 生产（备份前、恢复后）
DATA_DIR=/tmp/research-eval uv run python -m a_share_research.integrity     # 本地
```

## 候选初筛

网页“候选初筛”每月开一个批次（设计见[方案](docs/screening-experiment.md)）。worker 在没有排队的单家任务时推进批次：

1. 全市场确定性初筛（不调模型）：按交易日取股票池、估值、成交额和申万一级行业，粗筛后逐家取近 5 年 `fina_indicator`/`income`/`cashflow`/`fina_audit`，4 线程约 20–30 分钟；初筛期间单家研究排队。
2. 前 20 家（Q20）各自提交一次研究（7 天内已通过的报告直接沿用；网页或 API 新提交的研究排在这些批次研究之前），全部结束后经 pi 做一次跨公司比较，给出 5 家草案。
3. 批次页展示各因子原值与行业分位、近 60 个交易日涨跌幅（跌幅超过 30% 标出）、板块覆盖和缺口公司；所有者勾选并冻结（草案出来后第一个交易日 09:00 仍未冻结则按草案自动冻结，并标明未经人工确认）；冻结后第一个交易日收盘后（17:00 后）记录开盘入场价，之后每个交易日收盘后更新 L5、机械前五 Q5、全部 Q20 与中证 800/沪深 300 全收益指数的单位净值收益。

服务重启时，进行中的初筛记为失败（重新开始本月批次即可），跨公司比较自动重试。只看初筛结果、不建批次：

```bash
uv run --env-file .env python -m a_share_research.screening
```

## 部署

项目采用 Docker Compose 进行前后端容器化分离部署，全部服务在本项目目录下统一编排：

- **前端（web）**：FastAPI + Jinja 服务端 HTML、OAuth 认证与交互界面。镜像内不含模型与行情凭据，通过 `nginx-network` 接入宿主 Nginx 反向代理，监听 `127.0.0.1:8620`。配置由 `.env.web` 提供。
- **后端（worker）**：后台研究任务执行器，负责 TuShare 行情抓取、PDF 财报抽取与 LLM 研报生成。挂载宿主 `~/.pi`（pi 托管安装与认证），`PI_RUNTIME` 留空时按 `~/.pi/agent/install/current-version` 找运行时。配置由 `.env` 提供。与前端通过共享的 SQLite 数据目录解耦通信，不暴露外部端口。

发布：提交并推送 main 后在本目录执行（worker 有任务在跑时先等它空闲）：
```bash
docker compose up -d --build
```
镜像构建时复制代码，开发改动在重建前不影响线上；不要从有未提交改动的工作树重建。
数据默认保存在 `/home/lin/a-share-research-data`（可通过 `DATA_DIR` 自定义）。
