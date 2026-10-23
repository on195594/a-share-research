# a-share-research

收盘后的 A 股个股研究助手：输入一家公司和可选问题，AI 自主补读定期披露、查询财务与程序计算，通常几分钟后读到带原文出处的研究报告（结论、最强反证、业务、财务），并可按需查看程序计算的价格参考。

本项目负责公司财报研究与阅读；`a-stock-agent-skills` 负责 Agent 投研、持仓与风险，两者独立运行并共用 `a-stock-lib`。网页与研究 API 共用任务、原文、计算和追问/纠错；不直接修改账户或投资政策。

- 产品方向与现有流程：[重构方案](docs/proposal.md)；开发约束：[AGENTS.md](AGENTS.md)。
- 三项目职责与协作：[项目契约](docs/project-contract.md)。
- AI 能力演进与进入条件：[路线图](docs/roadmap.md)。
- 本批工作与验收：[推进顺序](docs/implementation-plan.md#下一批推进顺序)；生产版本与回退：[当前部署](docs/implementation-plan.md#当前部署)。

## 运行

需要 Python ≥3.13、uv、Node.js、`pdftotext`（poppler-utils）和本机 pi 运行时。按 `.env.example` 建 `.env`（0600，不进 Git），然后 `uv sync`。

```bash
uv run --env-file .env python -m a_share_research.research 600519   # 命令行研究一家（不带代码则跑评估集）
uv run --env-file .env python -m a_share_research.worker            # 后台 worker：执行网页提交的研究与价格参考
uv run --env-file .env.web uvicorn a_share_research.web:app --port 8610  # 网页（本机开发）
uv run pytest -q tests
```

`.env.web` 只放网页配置：`PUBLIC_BASE_URL`、`SESSION_SECRET`（≥32 字符），以及 GitHub OAuth 的 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`GITHUB_USER_ID`；本机开发可用 `DEV_LOGIN=1`（仅限 loopback 地址）代替 OAuth。数据默认在 `./data`，生产用 `DATA_DIR` 指到代码目录之外。

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

材料与计算可在 `evidence` 中定位；`coverage.periods` 只列本公司财报期间，行情日期另见 `market_date`，`coverage.missing` 包含取证与报告声明的缺口。旧报告缺少的身份不回填。仅覆盖列出的官方定期报告和已取财务数据，不覆盖全部临时公告和新闻。追问/纠错保留原报告，新报告给出核查结果；已复核纠错会作为后续研究线索，但仍需核实。

CLI 同样支持问题：

```bash
uv run --env-file .env python -m a_share_research.research 600519 --question '最新财报改变了什么，利润与现金流是否一致？'
```

## 运行身份与材料重放

新研究在 `runs.identity` 保存进程启动时的应用文件内容哈希、Python/Lib 版本及材料哈希，在 `calls.identity` 保存请求与运行时报告的模型、pi/Node 版本、调用参数和完整提示词哈希。每次调用另记桥接脚本哈希、模型来自 catalog 还是 extension；加载扩展时记录其入口路径和文件哈希，不代表已覆盖其全部依赖。失败保留已知身份及子进程诊断；旧记录的空值表示未知，不用当前版本回填。应用内容哈希也适用于没有 `.git` 的容器。

`runs.material` 保存实际送入流程的财务表、行情时点、文档身份及两种顺序的原文页。重放复用该快照，执行成文、检查、复核和修复；新快照包含补证页与计算记录，重放不重新执行探索策略，并保存为新任务；不重新取行情或下载文档。快照缺失、版本不支持、哈希或公司不一致时失败。

在独立目录建立基线，按命令输出的任务 ID 重放，例如：

```bash
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research
DATA_DIR=/tmp/research-eval uv run --env-file .env python -m a_share_research.research --replay 1
```

`--replay` 可接受多个 ID，不能同时指定公司代码。旧报告或范围外公司可能没有完整快照；它们保持原记录，不能用当前资料伪造历史输入。完整评估与已知缺陷见[实施计划](docs/implementation-plan.md)。

重放报告保存来源任务和材料哈希，网页明确提示未刷新财报或行情；公司档案仍可查看它们，首页最新报告和“今日已研究”判断排除重放。此前已保存来源身份、但报告正文还没有标记的重放也适用。

新代码首次连接旧库时只增补 `runs.identity`、`runs.material`、`runs.request`、`calls.identity` 可空文本列，旧身份保持为空。兼容性、生产运行和浏览器验证范围统一记录在[实施计划](docs/implementation-plan.md#3-i1研究运行身份)。

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
